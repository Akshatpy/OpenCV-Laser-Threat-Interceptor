#include <Servo.h>

// Pan on D9 and optional tilt on D10. Power servos from an external 5V supply.
const int PAN_SERVO_PIN = 9;
const int TILT_SERVO_PIN = 10;
const int THREAT_OUTPUT_PIN = 7;
const bool THREAT_OUTPUT_ACTIVE_HIGH = true;
const int PAN_MIN_ANGLE = 20;
const int PAN_MAX_ANGLE = 160;
const int PAN_HOME_ANGLE = 90;
const int TILT_MIN_ANGLE = 20;
const int TILT_MAX_ANGLE = 160;
const int TILT_HOME_ANGLE = 90;

Servo panServo;
Servo tiltServo;
int currentPanAngle = PAN_HOME_ANGLE;
int currentTiltAngle = TILT_HOME_ANGLE;
bool threatOutputState = false;
String inputLine;

void applyPanAngle(int angle) {
  angle = constrain(angle, PAN_MIN_ANGLE, PAN_MAX_ANGLE);
  currentPanAngle = angle;
  panServo.write(currentPanAngle);
}

void applyTiltAngle(int angle) {
  angle = constrain(angle, TILT_MIN_ANGLE, TILT_MAX_ANGLE);
  currentTiltAngle = angle;
  tiltServo.write(currentTiltAngle);
}

void applyThreatOutput(bool active) {
  threatOutputState = active;
  int level = ((active && THREAT_OUTPUT_ACTIVE_HIGH) || (!active && !THREAT_OUTPUT_ACTIVE_HIGH)) ? HIGH : LOW;
  digitalWrite(THREAT_OUTPUT_PIN, level);
}

void setup() {
  Serial.begin(115200);
  pinMode(THREAT_OUTPUT_PIN, OUTPUT);
  applyThreatOutput(false);
  panServo.attach(PAN_SERVO_PIN);
  tiltServo.attach(TILT_SERVO_PIN);
  applyPanAngle(PAN_HOME_ANGLE);
  applyTiltAngle(TILT_HOME_ANGLE);
  Serial.println("UNO servo tracker ready");
}

void handleLine(const String &line) {
  if (line.startsWith("LASER:") || line.startsWith("LED:")) {
    int separator = line.indexOf(':');
    int value = line.substring(separator + 1).toInt();
    bool active = value != 0;
    applyThreatOutput(active);
    Serial.print("OK OUTPUT=");
    Serial.println(threatOutputState ? "ON" : "OFF");
    return;
  }

  if (line == "OUTPUT?") {
    Serial.print("OUTPUT=");
    Serial.println(threatOutputState ? "ON" : "OFF");
    return;
  }

  if (line.startsWith("PAN:")) {
    int angle = line.substring(4).toInt();
    applyPanAngle(angle);
    Serial.print("OK PAN=");
    Serial.println(currentPanAngle);
    return;
  }

  if (line.startsWith("TILT:")) {
    int angle = line.substring(5).toInt();
    applyTiltAngle(angle);
    Serial.print("OK TILT=");
    Serial.println(currentTiltAngle);
    return;
  }

  if (line == "HOME") {
    applyPanAngle(PAN_HOME_ANGLE);
    applyTiltAngle(TILT_HOME_ANGLE);
    Serial.println("OK HOME");
    return;
  }

  if (line == "PING") {
    Serial.println("PONG");
    return;
  }

  Serial.print("ERR UNKNOWN: ");
  Serial.println(line);
}

void loop() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\n') {
      inputLine.trim();
      if (inputLine.length() > 0) {
        handleLine(inputLine);
      }
      inputLine = "";
    } else {
      inputLine += c;
      if (inputLine.length() > 64) {
        inputLine = "";
      }
    }
  }
}
