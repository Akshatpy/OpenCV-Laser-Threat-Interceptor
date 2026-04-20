from __future__ import annotations

import os
import threading
import time
from dataclasses import asdict, dataclass
from typing import Optional
from urllib.parse import urlparse, urlunparse

import cv2
import numpy as np
import requests
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

load_dotenv()

try:
    import serial
except Exception:  # pragma: no cover
    serial = None


@dataclass
class DetectionState:
    found: bool = False
    label: str = "none"
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    center_x: int = 0
    center_y: int = 0
    area: int = 0
    timestamp: float = 0.0


class ServoBridge:
    def __init__(self) -> None:
        self.enabled = os.getenv("SERVO_ENABLED", "0") == "1"
        self.port = os.getenv("SERVO_SERIAL_PORT", "COM5")
        self.baud = int(os.getenv("SERVO_BAUD", "115200"))
        self.min_angle = int(os.getenv("SERVO_MIN_ANGLE", "20"))
        self.max_angle = int(os.getenv("SERVO_MAX_ANGLE", "160"))
        self.deadband = float(os.getenv("SERVO_DEADBAND", "0.05"))
        self.deadband_px = int(os.getenv("SERVO_DEADBAND_PX", "0"))
        self.kp = float(os.getenv("SERVO_KP", "25.0"))
        self.current_pan = float(os.getenv("SERVO_START_PAN", "90"))
        self.last_sent_angle: Optional[int] = None
        self.last_sent_at = 0.0
        self.min_send_interval = float(os.getenv("SERVO_MIN_SEND_INTERVAL", "0.04"))
        self.threat_output_enabled = os.getenv("THREAT_OUTPUT_ENABLED", "0") == "1"
        self.threat_output_mode = os.getenv("THREAT_OUTPUT_MODE", "LASER").strip().upper()
        if self.threat_output_mode not in {"LASER", "LED"}:
            self.threat_output_mode = "LASER"
        self.threat_output_state: Optional[bool] = None
        self.serial_conn = None

    def connect(self) -> None:
        if not self.enabled:
            print("Servo bridge disabled (SERVO_ENABLED!=1)", flush=True)
            return
        if serial is None:
            print("pyserial not installed, servo bridge disabled", flush=True)
            self.enabled = False
            return
        try:
            self.serial_conn = serial.Serial(self.port, self.baud, timeout=0.05, write_timeout=0)
            time.sleep(1.5)
            print(f"Servo bridge connected on {self.port} @ {self.baud}", flush=True)
            self._write_line("PING")
            self._read_available_lines(prefix="UNO")
            self.set_threat_output(False)
        except Exception as exc:
            self.serial_conn = None
            self.enabled = False
            print(f"Servo bridge failed on {self.port}: {exc}", flush=True)

    def close(self) -> None:
        if self.serial_conn is not None:
            try:
                self.serial_conn.close()
            except Exception:
                pass
            self.serial_conn = None

    def update_from_detection(self, center_x: int, frame_width: int) -> Optional[int]:
        if frame_width <= 0:
            return None
        offset_px = center_x - frame_width / 2.0
        if self.deadband_px > 0:
            if abs(offset_px) < self.deadband_px:
                return None
        else:
            deadband_px = self.deadband * max(frame_width / 2.0, 1.0)
            if abs(offset_px) < deadband_px:
                return None

        error_x = offset_px / max(frame_width / 2.0, 1.0)
        if abs(error_x) < 1e-6:
            return None

        target = self.current_pan + (self.kp * error_x)
        self.current_pan = max(self.min_angle, min(self.max_angle, target))
        angle = int(round(self.current_pan))
        if self._send_angle(angle):
            return angle
        return None

    def _send_angle(self, angle: int) -> bool:
        now = time.time()
        if now - self.last_sent_at < self.min_send_interval:
            return False
        if self.last_sent_angle is not None and abs(self.last_sent_angle - angle) < 1:
            return False

        if not self.enabled or self.serial_conn is None:
            return False

        self.last_sent_angle = angle
        self.last_sent_at = now

        try:
            self._write_line(f"PAN:{angle}")
            return True
        except Exception as exc:
            print(f"Servo write failed: {exc}", flush=True)
            # Fail-open: disable serial writes so vision loop cannot freeze on repeated IO errors.
            self.enabled = False
            self.close()
            return False

    def send_raw(self, command: str) -> None:
        if not self.enabled or self.serial_conn is None:
            return
        self._write_line(command)
        self._read_available_lines(prefix="UNO")

    def set_threat_output(self, active: bool) -> None:
        if not self.threat_output_enabled:
            return
        if self.threat_output_state == active:
            return
        if not self.enabled or self.serial_conn is None:
            return

        try:
            self._write_line(f"{self.threat_output_mode}:{1 if active else 0}")
            self.threat_output_state = active
            state_text = "ON" if active else "OFF"
            print(f"ARDUINO {self.threat_output_mode}={state_text}", flush=True)
        except Exception as exc:
            print(f"Output write failed: {exc}", flush=True)
            self.enabled = False
            self.close()

    def _write_line(self, command: str) -> None:
        if self.serial_conn is None:
            return
        self.serial_conn.write((command.strip() + "\n").encode("ascii"))

    def _read_available_lines(self, prefix: str = "UNO") -> None:
        if self.serial_conn is None:
            return
        deadline = time.time() + 0.25
        while time.time() < deadline:
            raw = self.serial_conn.readline()
            if not raw:
                break
            line = raw.decode("ascii", errors="ignore").strip()
            if line:
                print(f"{prefix}: {line}", flush=True)


class CameraProcessor:
    def __init__(self, source_url: str) -> None:
        self.source_url = source_url
        self.candidate_urls = self._build_candidate_urls(source_url)
        self.capture: Optional[cv2.VideoCapture] = None
        self.stream_response: Optional[requests.Response] = None
        self.stream_mode: Optional[str] = None  # "opencv" or "mjpeg_manual"
        self.stream_buffer = b""
        self.lock = threading.Lock()
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.latest_frame: Optional[np.ndarray] = None
        self.latest_overlay_frame: Optional[np.ndarray] = None
        self.latest_detection = DetectionState()
        self.last_printed_signature: Optional[tuple[int, int, int]] = None
        self.smoothed_center: Optional[tuple[float, float]] = None
        self.center_smooth_alpha = float(os.getenv("CENTER_SMOOTH_ALPHA", "0.5"))
        self.servo = ServoBridge()

    def start(self) -> None:
        if self.running:
            return
        self.servo.connect()
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1.0)
        if self.capture is not None:
            self.capture.release()
            self.capture = None
        if self.stream_response is not None:
            self.stream_response.close()
            self.stream_response = None
        self.stream_buffer = b""
        self.servo.close()

    def _open_capture(self) -> Optional[cv2.VideoCapture]:
        for candidate_url in self.candidate_urls:
            try:
                print(f"Trying stream URL (requests): {candidate_url}", flush=True)
                response = requests.get(
                    candidate_url,
                    stream=True,
                    timeout=(5, 1.5),
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                content_type = response.headers.get("content-type", "").lower()
                if response.status_code == 200 and "multipart" in content_type:
                    print(f"✓ Connected to MJPEG stream {candidate_url}", flush=True)
                    response.raw.decode_content = True
                    self.source_url = candidate_url
                    self.stream_response = response
                    self.stream_buffer = b""
                    self.stream_mode = "mjpeg_manual"
                    return None
                print(
                    f"✗ Candidate {candidate_url} returned status={response.status_code} content-type={content_type}",
                    flush=True,
                )
                response.close()
            except Exception as e:
                print(f"✗ Requests stream failed from {candidate_url}: {e}", flush=True)

        print("Trying OpenCV fallback after requests checks...", flush=True)
        for candidate_url in self.candidate_urls:
            print(f"Trying stream URL (OpenCV): {candidate_url}", flush=True)
            capture = cv2.VideoCapture(candidate_url)
            if capture.isOpened():
                print(f"✓ Successfully connected to {candidate_url}", flush=True)
                self.source_url = candidate_url
                self.stream_mode = "opencv"
                return capture
            print(f"✗ Failed to connect to {candidate_url}", flush=True)
            capture.release()

        print("✗ All candidate URLs and fallbacks failed", flush=True)
        return None

    def _build_candidate_urls(self, source_url: str) -> list[str]:
        candidate_urls = [source_url]

        parsed = urlparse(source_url)
        if parsed.scheme and parsed.netloc:
            if parsed.path in ("", "/"):
                base_netloc = parsed.netloc
                if ":" not in base_netloc:
                    candidate_urls.append(urlunparse((parsed.scheme, f"{base_netloc}:81", "/stream", "", "", "")))
                candidate_urls.append(urlunparse((parsed.scheme, parsed.netloc, "/stream", "", "", "")))

        extra_urls = os.getenv("ESP32_CAM_EXTRA_URLS", "")
        for candidate_url in [item.strip() for item in extra_urls.split(",") if item.strip()]:
            candidate_urls.append(candidate_url)

        deduped: list[str] = []
        for candidate_url in candidate_urls:
            if candidate_url not in deduped:
                deduped.append(candidate_url)
        
        print(f"Candidate stream URLs: {deduped}", flush=True)
        return deduped

    def _loop(self) -> None:
        while self.running:
            frame = None

            # Try OpenCV path if capture exists
            if self.capture is not None and self.capture.isOpened():
                ok, frame = self.capture.read()
                if not ok or frame is None:
                    self.capture.release()
                    self.capture = None
                    time.sleep(0.2)
                    continue

            # Try manual MJPEG path if stream_response exists
            elif self.stream_response is not None:
                try:
                    frame = self._read_mjpeg_frame(self.stream_response)
                    if frame is None:
                        self.stream_response.close()
                        self.stream_response = None
                        self.stream_buffer = b""
                        time.sleep(0.2)
                        continue
                except Exception as e:
                    print(f"✗ MJPEG read error: {e}", flush=True)
                    self.stream_response.close()
                    self.stream_response = None
                    self.stream_buffer = b""
                    time.sleep(0.2)
                    continue

            # No active stream, try to open one
            else:
                result = self._open_capture()
                if self.stream_response is not None:
                    continue
                if result is None:
                    time.sleep(1.0)
                    continue
                else:
                    self.capture = result
                continue

            # Process frame if we have one
            if frame is not None:
                detection = self._detect_red_object(frame)
                overlay = self._draw_overlay(frame, detection)

                with self.lock:
                    self.latest_frame = frame
                    self.latest_overlay_frame = overlay
                    self.latest_detection = detection

                if detection.found:
                    signature = (detection.center_x, detection.center_y, detection.area)
                    if signature != self.last_printed_signature:
                        self.last_printed_signature = signature
                    self.servo.set_threat_output(True)
                    self._mock_servo_target(detection.center_x, detection.center_y, frame.shape[1], frame.shape[0])
                else:
                    self.last_printed_signature = None
                    self.servo.set_threat_output(False)

                time.sleep(0.02)

    def _detect_red_object(self, frame: np.ndarray) -> DetectionState:
        blurred = cv2.GaussianBlur(frame, (5, 5), 0)
        hsv = cv2.cvtColor(blurred, cv2.COLOR_BGR2HSV)

        # Strict bright-red thresholds in HSV.
        lower_red_1 = np.array([0, 170, 120], dtype=np.uint8)
        upper_red_1 = np.array([8, 255, 255], dtype=np.uint8)
        lower_red_2 = np.array([172, 170, 120], dtype=np.uint8)
        upper_red_2 = np.array([180, 255, 255], dtype=np.uint8)

        mask_1 = cv2.inRange(hsv, lower_red_1, upper_red_1)
        mask_2 = cv2.inRange(hsv, lower_red_2, upper_red_2)
        hsv_mask = cv2.bitwise_or(mask_1, mask_2)

        # Extra strictness: require red channel dominance in BGR.
        b, g, r = cv2.split(blurred)
        red_dominance_mask = ((r.astype(np.int16) - np.maximum(g, b).astype(np.int16)) > 55) & (r > 130)
        red_dominance_mask = red_dominance_mask.astype(np.uint8) * 255

        mask = cv2.bitwise_and(hsv_mask, red_dominance_mask)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            self.smoothed_center = None
            return DetectionState(timestamp=time.time())

        best_contour = None
        best_score = -1.0
        min_area = 650

        for contour in contours:
            area = cv2.contourArea(contour)
            if area < min_area:
                continue

            perimeter = cv2.arcLength(contour, True)
            if perimeter <= 0:
                continue

            circularity = (4.0 * np.pi * area) / (perimeter * perimeter)
            x, y, w, h = cv2.boundingRect(contour)
            rect_area = max(w * h, 1)
            extent = area / rect_area

            if circularity < 0.45 or extent < 0.35:
                continue

            score = float(area) * (0.6 + 0.4 * circularity)
            if score > best_score:
                best_score = score
                best_contour = contour

        if best_contour is None:
            self.smoothed_center = None
            return DetectionState(timestamp=time.time())

        area = int(cv2.contourArea(best_contour))
        x, y, w, h = cv2.boundingRect(best_contour)

        moments = cv2.moments(best_contour)
        if moments["m00"] == 0:
            center_x = x + w // 2
            center_y = y + h // 2
        else:
            center_x = int(moments["m10"] / moments["m00"])
            center_y = int(moments["m01"] / moments["m00"])

        if self.smoothed_center is None:
            self.smoothed_center = (float(center_x), float(center_y))
        else:
            alpha = min(max(self.center_smooth_alpha, 0.05), 0.95)
            sx, sy = self.smoothed_center
            self.smoothed_center = (
                sx + alpha * (center_x - sx),
                sy + alpha * (center_y - sy),
            )

        center_x = int(self.smoothed_center[0])
        center_y = int(self.smoothed_center[1])
        return DetectionState(
            found=True,
            label="bright_red_threat",
            x=int(x),
            y=int(y),
            w=int(w),
            h=int(h),
            center_x=int(center_x),
            center_y=int(center_y),
            area=area,
            timestamp=time.time(),
        )

    def _draw_overlay(self, frame: np.ndarray, detection: DetectionState) -> np.ndarray:
        overlay = frame.copy()
        height, width = overlay.shape[:2]

        cv2.rectangle(overlay, (0, 0), (width - 1, height - 1), (255, 255, 255), 1)
        cv2.putText(
            overlay,
            "ESP32-CAM threat demo",
            (12, 28),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )

        if detection.found:
            x1, y1 = detection.x, detection.y
            x2, y2 = detection.x + detection.w, detection.y + detection.h
            cv2.rectangle(overlay, (x1, y1), (x2, y2), (0, 0, 255), 3)
            cv2.circle(overlay, (detection.center_x, detection.center_y), 6, (0, 255, 255), -1)
            cv2.putText(
                overlay,
                f"THREAT x={detection.center_x} y={detection.center_y}",
                (max(10, x1), max(40, y1 - 10)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (0, 0, 255),
                2,
                cv2.LINE_AA,
            )
        else:
            cv2.putText(
                overlay,
                "No threat detected",
                (12, 58),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (0, 255, 0),
                2,
                cv2.LINE_AA,
            )

        return overlay

    def _mock_servo_target(self, center_x: int, center_y: int, frame_width: int, frame_height: int) -> None:
        servo_angle = self.servo.update_from_detection(center_x, frame_width)
        if servo_angle is not None:
            print(f"ARDUINO PAN={servo_angle}", flush=True)

    def _read_mjpeg_frame(self, response: requests.Response) -> Optional[np.ndarray]:
        """Read one JPEG frame from an MJPEG stream response."""
        try:
            while self.running:
                chunk = response.raw.read(4096)
                if not chunk:
                    return None

                self.stream_buffer += chunk
                start_idx = self.stream_buffer.find(b"\xff\xd8")
                if start_idx == -1:
                    continue

                end_idx = self.stream_buffer.find(b"\xff\xd9", start_idx + 2)
                if end_idx == -1:
                    continue

                jpeg_data = self.stream_buffer[start_idx : end_idx + 2]
                self.stream_buffer = self.stream_buffer[end_idx + 2 :]
                frame = cv2.imdecode(np.frombuffer(jpeg_data, dtype=np.uint8), cv2.IMREAD_COLOR)
                if frame is not None:
                    return frame
        except Exception as e:
            print(f"Error reading MJPEG frame: {e}", flush=True)

        return None

    def get_detection(self) -> DetectionState:
        with self.lock:
            return self.latest_detection

    def get_overlay_frame(self) -> Optional[np.ndarray]:
        with self.lock:
            if self.latest_overlay_frame is None:
                return None
            return self.latest_overlay_frame.copy()


ESP32_CAM_URL = os.getenv("ESP32_CAM_URL", "http://10.145.167.57/")
camera = CameraProcessor(ESP32_CAM_URL)
app = FastAPI(title="ESP32-CAM Threat Demo", version="0.1.0")


@app.on_event("startup")
def on_startup() -> None:
    camera.start()


@app.on_event("shutdown")
def on_shutdown() -> None:
    camera.stop()


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return """
    <!doctype html>
    <html>
      <head>
        <meta charset=\"utf-8\" />
        <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
        <title>ESP32-CAM Threat Demo</title>
        <style>
                    body { font-family: Arial, sans-serif; margin: 0; background: #0b1020; color: #e8eefc; }
          header { padding: 16px 20px; background: linear-gradient(90deg, #101a38, #1b2a52); }
                    main { display: grid; grid-template-columns: 2fr 1fr; gap: 16px; padding: 16px; max-width: 1200px; margin: 0 auto; }
          .card { background: #121a33; border: 1px solid #223056; border-radius: 14px; padding: 14px; }
                    .stream-wrap {
                        width: 100%;
                        max-width: 900px;
                        aspect-ratio: 4 / 3;
                        max-height: 72vh;
                        margin: 0 auto;
                        border-radius: 12px;
                        border: 1px solid #334165;
                        background: #000;
                        overflow: hidden;
                        display: flex;
                        align-items: center;
                        justify-content: center;
                    }
                    img {
                        width: 100%;
                        height: 100%;
                        object-fit: contain;
                        background: #000;
                    }
          pre { white-space: pre-wrap; word-break: break-word; background: #0a0f1f; padding: 12px; border-radius: 10px; border: 1px solid #233154; }
          .muted { color: #a9b7d0; }
          @media (max-width: 900px) { main { grid-template-columns: 1fr; } }
        </style>
      </head>
      <body>
        <header>
          <h1 style=\"margin:0 0 4px 0;\">ESP32-CAM Threat Demo</h1>
          <div class=\"muted\">Red object tracking prototype with overlay coordinates</div>
        </header>
        <main>
          <section class=\"card\">
            <h2>Live stream</h2>
                        <div class="stream-wrap">
                            <img src="/video_feed" alt="Processed camera stream" />
                        </div>
          </section>
          <aside class=\"card\">
            <h2>Status</h2>
            <pre id=\"status\">Loading...</pre>
          </aside>
        </main>
        <script>
          async function refresh() {
            try {
              const response = await fetch('/detection');
              const data = await response.json();
              document.getElementById('status').textContent = JSON.stringify(data, null, 2);
            } catch (error) {
              document.getElementById('status').textContent = 'Unable to load status: ' + error;
            }
          }
          refresh();
          setInterval(refresh, 500);
        </script>
      </body>
    </html>
    """


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "camera_url": ESP32_CAM_URL,
            "servo_enabled": camera.servo.enabled,
            "servo_port": camera.servo.port,
            "servo_pan": int(round(camera.servo.current_pan)),
            "threat_output_enabled": camera.servo.threat_output_enabled,
            "threat_output_mode": camera.servo.threat_output_mode,
            "threat_output_active": bool(camera.servo.threat_output_state),
        }
    )


@app.get("/detection")
def detection() -> JSONResponse:
    return JSONResponse(asdict(camera.get_detection()))


@app.get("/coordinates")
def coordinates() -> JSONResponse:
    detection_state = camera.get_detection()
    return JSONResponse(
        {
            "found": detection_state.found,
            "center_x": detection_state.center_x,
            "center_y": detection_state.center_y,
            "area": detection_state.area,
        }
    )


@app.post("/servo/test")
def servo_test(command: str = "PING") -> JSONResponse:
    camera.servo.send_raw(command)
    return JSONResponse(
        {
            "ok": True,
            "servo_enabled": camera.servo.enabled,
            "command_sent": command,
        }
    )


@app.get("/video_feed")
def video_feed() -> StreamingResponse:
    def frame_generator():
        while True:
            frame = camera.get_overlay_frame()
            if frame is None:
                time.sleep(0.1)
                continue

            ok, encoded = cv2.imencode(".jpg", frame)
            if not ok:
                continue

            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + encoded.tobytes() + b"\r\n"
            )
            time.sleep(0.05)

    return StreamingResponse(frame_generator(), media_type="multipart/x-mixed-replace; boundary=frame")
