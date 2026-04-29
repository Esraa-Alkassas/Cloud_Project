#include <Arduino.h>
#include "startup.h" // Your new wrapper

void setup() {
    Serial.begin(115200);
    delay(1000);

    Serial.println(">>> STUDENT APP WAKING UP <<<");
    
    // Arm the bounce-back!
    TEC.begin(); 
    Serial.println(">>> BOUNCE-BACK ARMED! Next reboot goes to Gatekeeper. <<<");

    pinMode(2, OUTPUT);
}

void loop() {
    digitalWrite(2, HIGH); delay(100);
    digitalWrite(2, LOW);  delay(100);
}