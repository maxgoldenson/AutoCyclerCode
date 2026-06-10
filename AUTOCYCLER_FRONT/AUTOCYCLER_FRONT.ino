/*
 * FRONT ASSEMBLY — Serial Command Reference
 * ──────────────────────────────────────────────────────────────────────────────
 *  WHO AM I               → IAM:FRONT_ASSEMBLY
 *
 *  GET COLOR ERROR        → RGB:<r>,<g>,<b>   (Error sensor, MUX ch 1)
 *  GET COLOR RING         → RGB:<r>,<g>,<b>   (Ring sensor,  MUX ch 0)
 *  GET COLOR ERROR LED    → RGB:...  (keeps LED on after read)
 *  GET COLOR RING  LED    → RGB:...  (keeps LED on after read)
 *
 *  SET SERVO <0-180>      → SERVO:<angle>
 *  SET CAP ON             → CAP:ON
 *  SET CAP OFF            → CAP:OFF
 *
 *  On boot:               READY:FRONT_ASSEMBLY
 *  On error:              ERROR:<message>
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

// ── MUX config ─────────────────────────────────────────────────────────────────
#define PCA9548A_ADDR 0x70
#define MUX_CH_RING 0   // Channel 0 = Ring sensor
#define MUX_CH_ERROR 1  // Channel 1 = Error sensor

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
#define SERVO_REST_DEG 95   // safe "gate closed" position to assume on boot

// ── State ──────────────────────────────────────────────────────────────────────
bool capActive = false;
unsigned long capOnMillis = 0;   // millis() at the moment CAP was last asserted

// ── Helpers ───────────────────────────────────────────────────────────────────

void selectMuxChannel(uint8_t ch) {
  Wire.beginTransmission(PCA9548A_ADDR);
  Wire.write(1 << ch);
  Wire.endTransmission();
}

bool readRGB(uint8_t channel, uint8_t &r, uint8_t &g, uint8_t &b) {
  selectMuxChannel(channel);
  uint16_t raw_r, raw_g, raw_b, raw_c;
  tcs.getRawData(&raw_r, &raw_g, &raw_b, &raw_c);
  if (raw_c == 0) return false;
  r = (uint8_t)constrain((raw_r * 255UL) / raw_c, 0, 255);
  g = (uint8_t)constrain((raw_g * 255UL) / raw_c, 0, 255);
  b = (uint8_t)constrain((raw_b * 255UL) / raw_c, 0, 255);
  return true;
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

void handleGetColor(const String &args) {
  // Syntax: GET COLOR [ERROR|RING] [LED]
  // Sensor defaults to ERROR for backward compatibility.
  String upper = args;
  upper.toUpperCase();
  upper.trim();

  uint8_t channel = MUX_CH_ERROR;
  bool ledArg = false;

  if (upper.startsWith("RING")) {
    channel = MUX_CH_RING;
    String tail = upper.substring(4);
    tail.trim();
    ledArg = (tail == "LED");
  } else if (upper.startsWith("ERROR")) {
    channel = MUX_CH_ERROR;
    String tail = upper.substring(5);
    tail.trim();
    ledArg = (tail == "LED");
  } else {
    ledArg = (upper == "LED");
  }

  digitalWrite(LED_PIN, HIGH);
  delay(60);

  uint8_t r, g, b;
  if (readRGB(channel, r, g, b)) {
    Serial.print("RGB:");
    Serial.print(r);
    Serial.print(",");
    Serial.print(g);
    Serial.print(",");
    Serial.println(b);
  } else {
    Serial.println("ERROR:zero clear channel");
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

  selectMuxChannel(MUX_CH_RING);
  if (!tcs.begin()) {
    Serial.println("ERROR:TCS34725 not found on Ring channel (MUX ch 0). Check wiring.");
    while (1) { delay(1000); }
  }

  selectMuxChannel(MUX_CH_ERROR);
  if (!tcs.begin()) {
    Serial.println("ERROR:TCS34725 not found on Error channel (MUX ch 1). Check wiring.");
    while (1) { delay(1000); }
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