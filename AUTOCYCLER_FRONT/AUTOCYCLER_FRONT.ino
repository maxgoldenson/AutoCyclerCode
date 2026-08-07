/*
 * FRONT ASSEMBLY — Serial Command Reference
 * ──────────────────────────────────────────────────────────────────────────────
 *  WHO AM I               → IAM:FRONT_ASSEMBLY
 *
 *  GET COLOR ERROR        → RGB:<r>,<g>,<b>   (Error sensor, MUX ch 1)
 *  GET COLOR RING         → RGB:<r>,<g>,<b>   (Ring sensor,  MUX ch 0)
 *  GET COLOR ERROR LED    → RGB:...  (keeps LED on after read)
 *  GET COLOR RING  LED    → RGB:...  (keeps LED on after read)
 *  GET COLOR <s> TAG      → RGB:<SENSOR>:<r>,<g>,<b>  (names the sensor it read;
 *                           both sensors share address 0x29, so the tag is the only
 *                           way a host can prove which one answered)
 *
 *  SET SERVO <0-180>      → SERVO:<angle>
 *  SET CAP ON             → CAP:ON
 *  SET CAP OFF            → CAP:OFF
 *
 *  On boot:               READY:FRONT_ASSEMBLY
 *  On error:              ERROR:<message>
 *
 *  The door cover (I2C mux + BOTH color sensors) is HOT-PLUGGABLE — see the
 *  "Door cover" section below. GET COLOR reports which part of the chain is
 *  missing so the host can tell "cover unplugged" from "sensor sees nothing":
 *    ERROR:door cover not detected (no I2C mux)
 *    ERROR:color sensor not responding
 *    ERROR:color sensor init failed
 *    ERROR:zero clear channel      (cover present, but no light reaching it)
 * ──────────────────────────────────────────────────────────────────────────────
 */

#include <Wire.h>
#include <Adafruit_TCS34725.h>
#include <ESP32Servo.h>

// ── Pin config ─────────────────────────────────────────────────────────────────
#define LED_PIN 25
#define SERVO_PIN 32
#define CAP_PIN 33

// ── Serial config ──────────────────────────────────────────────────────────────
#define SERIAL_BAUD 115200

// ── Device identity ────────────────────────────────────────────────────────────
#define DEVICE_ID "FRONT_ASSEMBLY"

// ── Firmware version ───────────────────────────────────────────────────────────
// The launcher flashes the board ONLY when this string changes — so editing comments
// or whitespace never triggers a fleet-wide re-flash. Bump it on any FUNCTIONAL change.
#define FW_VERSION "2026-08-05.1"

// ── MUX config ─────────────────────────────────────────────────────────────────
#define PCA9548A_ADDR 0x70
#define MUX_CH_RING 0   // Channel 0 = Ring sensor
#define MUX_CH_ERROR 1  // Channel 1 = Error sensor
#define MUX_CH_COUNT 2

// ── Door cover (HOT-PLUGGABLE) ─────────────────────────────────────────────────
// The door cover carries the I2C mux AND both color sensors, so the entire I2C chain
// can disappear and come back at any time — including being absent at boot. Nothing
// about the sensors may therefore be assumed from setup(): every color read
// re-establishes the chain before using it.
//
// It does NOT re-run Adafruit's begin() every read, though. begin() powers up the ADC
// and blocks for a full integration period (~55 ms) — and getRawData() already carries
// its own ~51 ms trailing delay — so doing it unconditionally would roughly double every
// read, and the host now polls the error light continuously for the whole cycle. Instead
// each read VERIFIES the chain cheaply (three short register reads, ~2 ms) and pays for
// a full re-init only when the hardware actually needs one:
//
//   1. mux ACKs at 0x70      — else the cover is not plugged in
//   2. sensor ID reads back  — else that channel's sensor is missing
//   3. ENABLE == PON|AEN     — else the chip lost power (a replug) and must be
//                              reconfigured: gain / integration time / enable
//
// Step 3 is what makes a fast unplug→replug safe. After a power cycle the sensor still
// ACKs its address and still reports the right ID, but its configuration is gone and it
// returns zeros — which would look like a DARK indicator rather than a missing cover.
// On this machine "dark error light" means "no fault", so mistaking one for the other
// would silently disarm the error detector. Checking ENABLE closes that hole.
#define TCS34725_I2C_ADDR  0x29
#define TCS_COMMAND_BIT    0x80
#define TCS_REG_ENABLE     0x00
#define TCS_REG_ID         0x12
#define TCS_ENABLE_RUNNING 0x03   // PON | AEN — powered and integrating

// ── Sensor config ──────────────────────────────────────────────────────────────
// Single instance used for both sensors — MUX channel is selected before each access.
Adafruit_TCS34725 tcs = Adafruit_TCS34725(
  TCS34725_INTEGRATIONTIME_50MS,
  TCS34725_GAIN_4X);

Servo myServo;

// ── Safety / failsafe config ────────────────────────────────────────────────────
// If the host ever fails to release the brew trigger (crash, USB unplug, killed
// process), auto-release CAP after this long so the machine's start button can never
// be held pressed indefinitely. Set well above the longest legitimate hold — the
// host's 10s reset hold — so a real operation is never cut short.
#define CAP_MAX_ON_MS 15000UL
#define SERVO_REST_DEG 48   // safe "gate closed" position to assume on boot

// ── State ──────────────────────────────────────────────────────────────────────
bool capActive = false;
unsigned long capOnMillis = 0;   // millis() at the moment CAP was last asserted

// Per-MUX-channel: has THIS sensor been configured since it was last seen to be up?
// Cleared whenever the cover goes missing or a chip is found unconfigured, which is what
// makes the next read re-initialize it instead of trusting a stale boot-time setup.
bool sensorConfigured[MUX_CH_COUNT] = { false, false };

// ── Helpers ───────────────────────────────────────────────────────────────────

// One-byte register read. Returns false if the device does not ACK — which is how an
// absent door cover is detected, so callers must never treat a failure as a zero value.
bool i2cRead8(uint8_t addr, uint8_t reg, uint8_t &val) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  if (Wire.endTransmission() != 0) return false;
  if (Wire.requestFrom(addr, (uint8_t)1) != 1) return false;
  val = Wire.read();
  return true;
}

// Read the PCA9548A control register (a bare 1-byte read — no register address).
bool muxReadback(uint8_t &val) {
  if (Wire.requestFrom((uint8_t)PCA9548A_ADDR, (uint8_t)1) != 1) return false;
  val = Wire.read();
  return true;
}

// Point the mux at exactly one channel and PROVE it landed there.
//
// An ACKed write is not proof the switch actually latched, and both sensors are
// TCS34725s at the same address 0x29 — the mux is the ONLY thing that distinguishes
// them. So a mux left on the wrong channel doesn't fail loudly: it silently answers
// with the OTHER sensor's reading, i.e. the error light appearing to show a ring
// color. Reading the control register back makes that impossible to miss.
bool muxSelect(uint8_t ch) {
  const uint8_t want = (uint8_t)(1 << ch);
  Wire.beginTransmission(PCA9548A_ADDR);
  Wire.write(want);
  if (Wire.endTransmission() != 0) return false;
  uint8_t got = 0;
  if (!muxReadback(got)) return false;
  return got == want;   // exactly one channel, and it is the one asked for
}

// Unwedge the bus. A cover pulled mid-transfer can leave a device holding SDA low,
// which jams every later transaction until power-cycled — exactly the kind of failure
// hot-plugging invites. Clock the bus until SDA floats high again, then restart Wire.
// Only ever called on the failure path, so it costs nothing in normal operation.
void i2cRecover() {
  Wire.end();
  pinMode(SDA, INPUT_PULLUP);
  pinMode(SCL, OUTPUT);
  for (uint8_t i = 0; i < 9 && digitalRead(SDA) == LOW; i++) {
    digitalWrite(SCL, LOW);
    delayMicroseconds(5);
    digitalWrite(SCL, HIGH);
    delayMicroseconds(5);
  }
  Wire.begin();
}

// Re-establish the I2C chain for one channel, initializing only what is actually
// missing (see the "Door cover" notes). Returns nullptr on success, else the reason.
const char *ensureSensor(uint8_t ch) {
  if (!muxSelect(ch)) {
    // Cover gone or bus jammed. Either way nothing downstream can be trusted.
    for (uint8_t i = 0; i < MUX_CH_COUNT; i++) sensorConfigured[i] = false;
    i2cRecover();
    if (!muxSelect(ch)) return "door cover not detected (no I2C mux)";
  }

  uint8_t id;
  if (!i2cRead8(TCS34725_I2C_ADDR, TCS_COMMAND_BIT | TCS_REG_ID, id) ||
      (id != 0x44 && id != 0x10 && id != 0x4D)) {
    sensorConfigured[ch] = false;
    return "color sensor not responding";
  }

  uint8_t enable;
  bool running = i2cRead8(TCS34725_I2C_ADDR, TCS_COMMAND_BIT | TCS_REG_ENABLE, enable)
                 && (enable & TCS_ENABLE_RUNNING) == TCS_ENABLE_RUNNING;
  if (sensorConfigured[ch] && running) return nullptr;   // already up — skip the ~55 ms

  // begin() re-reads the ID and reapplies gain + integration time, then enables the ADC.
  // The shared `tcs` object tracks only ONE chip's state, which is why configuration is
  // tracked per channel here rather than relying on the library.
  if (!tcs.begin()) {
    sensorConfigured[ch] = false;
    return "color sensor init failed";
  }
  sensorConfigured[ch] = true;
  return nullptr;
}

// Read one sensor as chromaticity (each channel normalized by clear, so brightness is
// divided out). Returns nullptr on success, else the reason the read failed.
const char *readColor(uint8_t ch, uint8_t &r, uint8_t &g, uint8_t &b) {
  const char *err = ensureSensor(ch);
  if (err != nullptr) return err;

  uint16_t raw_r, raw_g, raw_b, raw_c;
  tcs.getRawData(&raw_r, &raw_g, &raw_b, &raw_c);
  if (raw_c == 0) {
    // Present a moment ago but returning nothing: either genuinely pitch dark, or the
    // cover was pulled between the check above and this read. Force a full re-init next
    // time so a replug recovers on its own rather than latching a dead sensor.
    sensorConfigured[ch] = false;
    return "zero clear channel";
  }
  r = (uint8_t)constrain((raw_r * 255UL) / raw_c, 0, 255);
  g = (uint8_t)constrain((raw_g * 255UL) / raw_c, 0, 255);
  b = (uint8_t)constrain((raw_b * 255UL) / raw_c, 0, 255);
  return nullptr;
}

void setCapPin(bool active) {
  capActive = active;
  if (active) {
    capOnMillis = millis();   // start the auto-release watchdog
    pinMode(CAP_PIN, OUTPUT);
    digitalWrite(CAP_PIN, LOW);
  } else {
    pinMode(CAP_PIN, INPUT);
  }
}

// ── Command handlers ───────────────────────────────────────────────────────────

void handleWhoAmI() {
  Serial.print("IAM:");
  Serial.println(DEVICE_ID);
}

// GET VERSION → FW:<FW_VERSION>  — lets the host show which firmware is running.
void handleGetVersion() {
  Serial.print("FW:");
  Serial.println(FW_VERSION);
}

void handleGetColor(const String &args) {
  // Syntax: GET COLOR [ERROR|RING] [LED] [TAG]
  // Sensor defaults to ERROR for backward compatibility.
  //
  // TAG makes the reply name the sensor it came from: RGB:<SENSOR>:<r>,<g>,<b>. Both
  // sensors are TCS34725s at the same address, so without a tag a reading is only as
  // trustworthy as the mux, and a host has no way to tell an error-light reading from a
  // ring reading. Tagging is OPT-IN precisely so the two ends can update independently:
  //   new host + old firmware -> "TAG" falls into the ignored tail, reply is untagged
  //   old host + new firmware -> no TAG asked for, reply is untagged
  // Either way nothing breaks during the window where app and firmware differ.
  String upper = args;
  upper.toUpperCase();
  upper.trim();

  uint8_t channel = MUX_CH_ERROR;
  const char *sensorName = "ERROR";
  String tail;

  if (upper.startsWith("RING")) {
    channel = MUX_CH_RING;
    sensorName = "RING";
    tail = upper.substring(4);
  } else if (upper.startsWith("ERROR")) {
    channel = MUX_CH_ERROR;
    sensorName = "ERROR";
    tail = upper.substring(5);
  } else {
    tail = upper;
  }
  tail.trim();
  bool ledArg = (tail.indexOf("LED") >= 0);
  bool tagArg = (tail.indexOf("TAG") >= 0);

  digitalWrite(LED_PIN, HIGH);
  delay(60);

  uint8_t r, g, b;
  const char *err = readColor(channel, r, g, b);
  if (err == nullptr) {
    Serial.print("RGB:");
    if (tagArg) {
      Serial.print(sensorName);   // RGB:ERROR:... / RGB:RING:...
      Serial.print(":");
    }
    Serial.print(r);
    Serial.print(",");
    Serial.print(g);
    Serial.print(",");
    Serial.println(b);
  } else {
    // Say WHICH link in the chain is missing — the host surfaces this verbatim, so
    // "reseat the door cover" and "the sensor sees nothing" stay distinguishable.
    Serial.print("ERROR:");
    Serial.println(err);
  }

  if (!ledArg) digitalWrite(LED_PIN, LOW);
}

void handleSetServo(const String &args) {
  if (args.length() == 0) {
    Serial.println("ERROR:SET SERVO requires an angle");
    return;
  }
  int angle = constrain(args.toInt(), 0, 180);
  myServo.write(angle);
  Serial.print("SERVO:");
  Serial.println(angle);
}

void handleSetCap(const String &args) {
  if (args.equalsIgnoreCase("ON")) {
    setCapPin(true);
    Serial.println("CAP:ON");
  } else if (args.equalsIgnoreCase("OFF")) {
    setCapPin(false);
    Serial.println("CAP:OFF");
  } else {
    Serial.println("ERROR:SET CAP requires ON or OFF");
  }
}

// ── Command dispatch ───────────────────────────────────────────────────────────

void dispatch(const String &raw) {
  String cmd = raw;
  cmd.trim();
  if (cmd.length() == 0) return;

  int spaceIdx = cmd.indexOf(' ');
  String verb = (spaceIdx < 0) ? cmd : cmd.substring(0, spaceIdx);
  String rest = (spaceIdx < 0) ? String("") : cmd.substring(spaceIdx + 1);
  rest.trim();
  verb.toUpperCase();

  if (verb == "WHO") {
    String noun = rest;
    noun.toUpperCase();
    if (noun == "AM I") handleWhoAmI();
    else {
      Serial.print("UNKNOWN:");
      Serial.println(cmd);
    }

  } else if (verb == "GET") {
    int sp2 = rest.indexOf(' ');
    String noun = (sp2 < 0) ? rest : rest.substring(0, sp2);
    String args = (sp2 < 0) ? String("") : rest.substring(sp2 + 1);
    noun.toUpperCase();
    args.trim();

    if (noun == "COLOR") handleGetColor(args);
    else if (noun == "VERSION") handleGetVersion();
    else {
      Serial.print("UNKNOWN:");
      Serial.println(cmd);
    }

  } else if (verb == "SET") {
    int sp2 = rest.indexOf(' ');
    String noun = (sp2 < 0) ? rest : rest.substring(0, sp2);
    String args = (sp2 < 0) ? String("") : rest.substring(sp2 + 1);
    noun.toUpperCase();
    args.trim();

    if (noun == "SERVO") handleSetServo(args);
    else if (noun == "CAP") handleSetCap(args);
    else {
      Serial.print("UNKNOWN:");
      Serial.println(cmd);
    }

  } else {
    Serial.print("UNKNOWN:");
    Serial.println(cmd);
  }
}

// ── Arduino lifecycle ──────────────────────────────────────────────────────────

void setup() {
  Serial.begin(SERIAL_BAUD);
  while (!Serial) { delay(10); }

  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  myServo.setPeriodHertz(50);
  myServo.attach(SERVO_PIN, 500, 2400);
  myServo.write(SERVO_REST_DEG);   // boot to a known safe (gate closed) position

  pinMode(CAP_PIN, INPUT);

  Wire.begin();

  // Warm the sensors up if the cover happens to be attached, purely so the first color
  // read isn't the one that pays for initialization. This is an OPTIMIZATION, not a
  // requirement: the cover is hot-pluggable, so being absent right now is a perfectly
  // normal state and must not be reported as a fault. Every read re-establishes the
  // chain on its own (ensureSensor), so a cover attached later just starts working.
  //
  // Never halt on a sensor problem either way: a halted board never sends READY and
  // never answers WHO AM I, so the host couldn't identify it to connect OR to flash new
  // firmware. Stay alive in a degraded state — GET COLOR returns a reason, while
  // SET SERVO / SET CAP / WHO AM I keep working.
  const char *ringErr  = ensureSensor(MUX_CH_RING);
  const char *errorErr = ensureSensor(MUX_CH_ERROR);
  if (ringErr != nullptr || errorErr != nullptr) {
    // EVENT:, not ERROR: — this is an expected state for a hot-pluggable part.
    Serial.println("EVENT:DOOR_COVER_ABSENT");
  }

  Serial.print("READY:");
  Serial.println(DEVICE_ID);
}

void loop() {
  // Failsafe watchdog: never let the brew trigger stay asserted indefinitely. If the
  // host fails to send SET CAP OFF (crash / unplug / killed process), release it here.
  if (capActive && (millis() - capOnMillis > CAP_MAX_ON_MS)) {
    setCapPin(false);
    Serial.println("EVENT:CAP_AUTORELEASE");
  }

  if (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    dispatch(line);
  }
}