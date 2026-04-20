#include <Servo.h>

// Wire signal to D9, power servo from an external 5V supply.
const int SERVO_PIN = 9;
const int THREAT_OUTPUT_PIN = 7;
const bool THREAT_OUTPUT_ACTIVE_HIGH = true;
const int MIN_ANGLE = 20;
const int MAX_ANGLE = 160;
const int HOME_ANGLE = 90;

Servo panServo;
int currentAngle = HOME_ANGLE;
bool threatOutputState = false;
String inputLine;

void applyAngle(int angle) {
  angle = constrain(angle, MIN_ANGLE, MAX_ANGLE);
  currentAngle = angle;
  panServo.write(currentAngle);
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
  panServo.attach(SERVO_PIN);
  applyAngle(HOME_ANGLE);
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
    applyAngle(angle);
    Serial.print("OK PAN=");
    Serial.println(currentAngle);
    return;
  }

  if (line == "HOME") {
    applyAngle(HOME_ANGLE);
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
