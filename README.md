# ESP32-CAM Threat Demo Server

This project provides a FastAPI server for a simple prototype defense demo:

- reads frames from an ESP32-CAM stream
- detects a red object as the mock threat
- draws a danger rectangle and center point
- prints threat coordinates to the console
- exposes a live MJPEG feed and JSON status endpoints

<img src="images/circuit.png" width="700"/>
<img src="images/workingimg.png" width="700" height="500"/>
## Setup

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Run the server:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## Laser or LED Threat Output (optional)

You can mount a small LED or laser module on top of the servo horn so it physically moves with the camera direction. The software now turns this output ON while a threat is detected and OFF when no threat is detected.

### Arduino pin used

- Output control pin: Uno D7
- Command from FastAPI: `LASER:1` / `LASER:0` (or `LED:1` / `LED:0`)

### Wiring for a normal LED

- Uno D7 -> 220 ohm resistor -> LED anode (+)
- LED cathode (-) -> GND

### Wiring for a laser module

Most laser modules should not be driven directly from a Uno pin. Use a transistor switch:

- Uno D7 -> 1k resistor -> NPN base (2N2222)
- NPN emitter -> GND
- NPN collector -> laser module negative (-)
- Laser module positive (+) -> +5V external supply
- Uno GND -> external supply GND (common ground)

This is a prototype detector, not a weapon system.
