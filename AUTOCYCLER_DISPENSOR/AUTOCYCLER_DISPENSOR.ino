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

// ── State ──────────────────────────────────────────────────────────────────────
bool motorEnabled = false;

// ── Helpers ───────────────────────────────────────────────────────────────────

void setMotorEnabled(bool enable) {
    motorEnabled = enable;
    digitalWrite(ENABLE_PIN, enable ? LOW : HIGH);  // driver is active-low
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
 * SET ANGLE <degrees>
 * Enables motor, moves, then re-disables unless the motor was already
 * held enabled via SET MOTOR ON.
 *
 * Response: ANGLE:<degrees>
 */
void handleSetAngle(const String &args) {
    if (args.length() == 0) {
        Serial.println("ERROR:SET ANGLE requires a value in degrees");
        return;
    }

    float degrees = args.toFloat();
    bool wasEnabled = motorEnabled;

    setMotorEnabled(true);
    stepDegrees(degrees);
    if (!wasEnabled) setMotorEnabled(false);

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