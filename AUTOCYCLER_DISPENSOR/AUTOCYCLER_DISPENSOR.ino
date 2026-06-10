#include <Arduino.h>

// ── Pin config ─────────────────────────────────────────────────────────────────
#define STEP_PIN    25
#define DIR_PIN     33
#define ENABLE_PIN  32

// ── Serial config ──────────────────────────────────────────────────────────────
#define SERIAL_BAUD 115200

// ── Device identity ────────────────────────────────────────────────────────────
#define DEVICE_ID   "DISPENSER"

// ── Stepper config ─────────────────────────────────────────────────────────────
#define STEPS_PER_REV     (400 * 48 / 20)   // 1.8° motor + gearing
#define MICROSTEPPING     4
#define STEP_DELAY_MICROS 420               // lower = faster

const float STEPS_PER_DEGREE = (STEPS_PER_REV * MICROSTEPPING) / 360.0f;

// ── Safety limits ──────────────────────────────────────────────────────────────
// Reject absurd moves from a garbled command — a normal dispense is 360 deg.
#define MAX_DISPENSE_DEG 1080.0f   // 3 revolutions

// A re-sent / buffered-duplicate dispense arrives within a couple of seconds of the
// original; the next legitimate cycle's dispense is always tens of seconds later. So
// we only dedup a repeated seq seen inside this short window — this catches retries
// and RX-buffered duplicates without ever suppressing a genuine new dispense (even
// one that reuses a seq value after the host process restarts).
#define DISPENSE_DEDUP_WINDOW_MS 5000UL

// ── State ──────────────────────────────────────────────────────────────────────
bool motorEnabled = false;
long lastDispenseSeq = -1;            // seq id of the last executed SET ANGLE
unsigned long lastDispenseMillis = 0; // millis() when that dispense completed

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

    // At-most-once: a duplicate seq arriving within the dedup window means the host
    // re-sent (or a frame was buffered during the blocking move). Ack without moving.
    if (seq >= 0 && seq == lastDispenseSeq &&
        (millis() - lastDispenseMillis) < DISPENSE_DEDUP_WINDOW_MS) {
        Serial.print("ANGLE:"); Serial.println(degrees);
        return;
    }

    bool wasEnabled = motorEnabled;

    setMotorEnabled(true);
    stepDegrees(degrees);
    if (!wasEnabled) setMotorEnabled(false);

    if (seq >= 0) lastDispenseSeq = seq;
    lastDispenseMillis = millis();

    Serial.print("ANGLE:"); Serial.println(degrees);
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

    Serial.print("READY:"); Serial.println(DEVICE_ID);
}

void loop() {
    if (Serial.available()) {
        String line = Serial.readStringUntil('\n');
        dispatch(line);
    }
}