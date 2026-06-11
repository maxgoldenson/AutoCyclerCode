#include <Arduino.h>

// ── Pin config ─────────────────────────────────────────────────────────────────
#define STEP_PIN    25
#define DIR_PIN     33
#define ENABLE_PIN  32

// ── Serial config ──────────────────────────────────────────────────────────────
#define SERIAL_BAUD 115200

// ── Device identity ────────────────────────────────────────────────────────────
#define DEVICE_ID   "DISPENSER"

// ── Firmware version ───────────────────────────────────────────────────────────
// The launcher flashes the board ONLY when this string changes — so editing comments
// or whitespace never triggers a fleet-wide re-flash. Bump it on any FUNCTIONAL change.
#define FW_VERSION  "2026-06-10.3"

// ── Stepper config ─────────────────────────────────────────────────────────────
#define STEPS_PER_REV     (400 * 48 / 20)   // 1.8° motor + gearing
#define MICROSTEPPING     4
#define STEP_DELAY_MICROS 420               // lower = faster

const float STEPS_PER_DEGREE = (STEPS_PER_REV * MICROSTEPPING) / 360.0f;

// ── Safety limits ──────────────────────────────────────────────────────────────
// Reject absurd moves from a garbled command — a normal dispense is 360 deg.
#define MAX_DISPENSE_DEG 1080.0f   // 3 revolutions

// The ANGLE: ack used to be transmitted in the microseconds after the final step pulse,
// while the driver/coils are still switching — motor EMI right on top of the UART frame
// is the prime suspect for the ~50% corrupted/lost acks seen in the field (the FRONT
// board has no motor and never showed the problem). Wait for the transients to die
// before touching the UART.
#define ACK_SETTLE_MS 75

// ── State ──────────────────────────────────────────────────────────────────────
bool motorEnabled = false;
long lastDispenseSeq = -1;     // seq id of the last executed SET ANGLE (at-most-once)
float lastDispenseDeg = 0.0f;  // degrees of that dispense (reported by GET STATUS)
uint32_t bootId = 0;           // random per boot — lets the host detect a mid-move reset

// ── Helpers ───────────────────────────────────────────────────────────────────

void setMotorEnabled(bool enable) {
    motorEnabled = enable;
    digitalWrite(ENABLE_PIN, enable ? LOW : HIGH);  // driver is active-low
}

// Returns true only if s is a valid signed decimal number (e.g. "360", "-12.5").
// Guards against String::toFloat() silently coercing garbage to 0.0.
bool isNumeric(const String &s) {
    if (s.length() == 0) return false;
    int start = (s[0] == '+' || s[0] == '-') ? 1 : 0;
    if (start >= (int)s.length()) return false;
    bool dot = false, digit = false;
    for (int i = start; i < (int)s.length(); i++) {
        char c = s[i];
        if (c == '.') {
            if (dot) return false;   // more than one decimal point
            dot = true;
        } else if (c >= '0' && c <= '9') {
            digit = true;
        } else {
            return false;
        }
    }
    return digit;
}

void stepDegrees(float degrees) {
    if (degrees == 0.0f) return;

    long steps = abs(degrees) * STEPS_PER_DEGREE;
    digitalWrite(DIR_PIN, degrees > 0 ? HIGH : LOW);

    for (long i = 0; i < steps; i++) {
        digitalWrite(STEP_PIN, HIGH);
        delayMicroseconds(STEP_DELAY_MICROS);
        digitalWrite(STEP_PIN, LOW);
        delayMicroseconds(STEP_DELAY_MICROS);

        // This move can run several seconds — past the ~5s ESP32 task watchdog. Without
        // feeding it, the watchdog fires mid-move and dumps warning text onto the serial
        // line, corrupting the ANGLE: reply (the front board never blocks this long, so
        // it never sees this). yield() lets the idle task run and resets the watchdog;
        // it returns in microseconds when nothing else is pending, so step timing is
        // unaffected. Every 64 steps (~50 ms) is far more often than the watchdog needs.
        if ((i & 0x3F) == 0) yield();
    }
}

// ── Command handlers ───────────────────────────────────────────────────────────

void handleWhoAmI() {
    Serial.print("IAM:"); Serial.println(DEVICE_ID);
}

/**
 * SET ANGLE <degrees> [<seq>]
 * Enables motor, moves, then re-disables unless the motor was already
 * held enabled via SET MOTOR ON.
 *
 * <seq> is an optional monotonic id from the host. SET ANGLE is a RELATIVE move,
 * so executing it twice dispenses twice (an overflow hazard). If a command arrives
 * whose seq matches the last one already executed, it is treated as a duplicate
 * (e.g. the host re-sent because an ack was lost, or a frame was buffered during the
 * blocking move) and is ACKed WITHOUT moving — keeping the dispense at-most-once.
 * Commands without a seq are executed normally (backward compatible).
 *
 * Response: ANGLE:<degrees>
 */
void handleSetAngle(const String &args) {
    if (args.length() == 0) {
        Serial.println("ERROR:SET ANGLE requires a value in degrees");
        return;
    }

    // Split an optional trailing sequence id: "<degrees> <seq>".
    String degStr = args;
    long seq = -1;
    int sp = args.indexOf(' ');
    if (sp >= 0) {
        degStr = args.substring(0, sp);
        String seqStr = args.substring(sp + 1);
        seqStr.trim();
        if (seqStr.length() > 0) seq = seqStr.toInt();
    }
    degStr.trim();

    // Reject non-numeric input rather than silently dispensing 0 deg (a dry brew the
    // host would read as success).
    if (!isNumeric(degStr)) {
        Serial.println("ERROR:SET ANGLE value is not numeric");
        return;
    }
    float degrees = degStr.toFloat();

    // Never make an enormous move from a garbled value.
    if (fabs(degrees) > MAX_DISPENSE_DEG) {
        Serial.print("ERROR:SET ANGLE magnitude exceeds limit ");
        Serial.println(MAX_DISPENSE_DEG);
        return;
    }

    // At-most-once: a repeated seq means the host deliberately re-sent because it could
    // not verify execution (lost ack / lost command) — ack WITHOUT moving. Equality only,
    // no time window: the host may legitimately re-send the same seq tens of seconds
    // later (after its STATUS verification probes), and host seqs are session-random +
    // monotonic, so a stale match across sessions is effectively impossible.
    if (seq >= 0 && seq == lastDispenseSeq) {
        Serial.print("ANGLE:"); Serial.println(degrees);
        return;
    }

    bool wasEnabled = motorEnabled;

    setMotorEnabled(true);
    stepDegrees(degrees);
    if (!wasEnabled) setMotorEnabled(false);

    lastDispenseDeg = degrees;
    if (seq >= 0) lastDispenseSeq = seq;

    // Let the motor/driver switching transients die down before transmitting — sending
    // the ack in the same microseconds the coils de-energize is how acks get corrupted.
    delay(ACK_SETTLE_MS);

    Serial.print("ANGLE:"); Serial.println(degrees);
}

/**
 * GET STATUS
 * Idempotent query the host uses to VERIFY a dispense when the ANGLE: ack was lost:
 *   STATUS:<bootId>,<lastSeq>,<lastDeg>
 * - bootId: random per boot. If it changes between two host queries, the board reset
 *   (e.g. brownout mid-move) and any in-flight dispense state is unknowable.
 * - lastSeq: seq of the last executed SET ANGLE (-1 = none since boot).
 * If lastSeq matches what the host sent, the dispense happened and only the ack was
 * lost; if not (same bootId), the command itself was lost and a same-seq re-send is
 * safe (the equality dedup above makes it at-most-once).
 */
void handleGetStatus() {
    Serial.print("STATUS:");
    Serial.print(bootId);
    Serial.print(",");
    Serial.print(lastDispenseSeq);
    Serial.print(",");
    Serial.println(lastDispenseDeg);
}

/**
 * SET MOTOR <ON|OFF>
 * Holds or releases the motor driver enable line independently of moves.
 * Useful for holding position under load or releasing for manual adjustment.
 *
 * Response: MOTOR:ON or MOTOR:OFF
 */
void handleSetMotor(const String &args) {
    if (args.equalsIgnoreCase("ON")) {
        setMotorEnabled(true);
        Serial.println("MOTOR:ON");
    } else if (args.equalsIgnoreCase("OFF")) {
        setMotorEnabled(false);
        Serial.println("MOTOR:OFF");
    } else {
        Serial.println("ERROR:SET MOTOR requires ON or OFF");
    }
}

// ── Command dispatch ───────────────────────────────────────────────────────────

void dispatch(const String &raw) {
    String cmd = raw;
    cmd.trim();
    if (cmd.length() == 0) return;

    int spaceIdx = cmd.indexOf(' ');
    String verb  = (spaceIdx < 0) ? cmd          : cmd.substring(0, spaceIdx);
    String rest  = (spaceIdx < 0) ? String("")    : cmd.substring(spaceIdx + 1);
    rest.trim();
    verb.toUpperCase();

    if (verb == "WHO") {
        String noun = rest;
        noun.toUpperCase();
        if (noun == "AM I") handleWhoAmI();
        else { Serial.print("UNKNOWN:"); Serial.println(cmd); }

    } else if (verb == "GET") {
        String noun = rest;
        noun.toUpperCase();
        if (noun == "STATUS") handleGetStatus();
        else { Serial.print("UNKNOWN:"); Serial.println(cmd); }

    } else if (verb == "SET") {
        int sp2     = rest.indexOf(' ');
        String noun = (sp2 < 0) ? rest            : rest.substring(0, sp2);
        String args = (sp2 < 0) ? String("")       : rest.substring(sp2 + 1);
        noun.toUpperCase(); args.trim();

        if      (noun == "ANGLE") handleSetAngle(args);
        else if (noun == "MOTOR") handleSetMotor(args);
        else { Serial.print("UNKNOWN:"); Serial.println(cmd); }

    } else {
        Serial.print("UNKNOWN:"); Serial.println(cmd);
    }
}

// ── Arduino lifecycle ──────────────────────────────────────────────────────────

void setup() {
    Serial.begin(SERIAL_BAUD);
    while (!Serial) { delay(10); }

    pinMode(STEP_PIN,   OUTPUT);
    pinMode(DIR_PIN,    OUTPUT);
    pinMode(ENABLE_PIN, OUTPUT);

    setMotorEnabled(false);  // driver disabled at startup

    // Random per-boot id (esp_random uses the hardware RNG). 0 is reserved for
    // "unknown", so nudge it if the RNG ever returns 0.
    bootId = (uint32_t)esp_random();
    if (bootId == 0) bootId = 1;

    Serial.print("READY:"); Serial.println(DEVICE_ID);
}

void loop() {
    if (Serial.available()) {
        String line = Serial.readStringUntil('\n');
        dispatch(line);
    }
}