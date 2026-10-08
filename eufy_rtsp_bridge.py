#!/usr/bin/env python3
import asyncio
import json
import signal
import subprocess
import sys
import websockets

WS_URI = "ws://127.0.0.1:3000"
RTSP_BASE = "rtsp://127.0.0.1:8554"

CAMERAS = {
    "solocam1": {"serial": "T8170T1125360152", "codec": "hevc", "fps": "15"},
    "solocam2": {"serial": "T8170T1125372768", "codec": "hevc", "fps": "15"},
    "doorbell": {"serial": "T8214511253325BB", "codec": "h264", "fps": "15"},
}

class CameraStreamer:
    def __init__(self, path: str, config: dict):
        self.path = path
        self.serial = config["serial"]
        self.codec = config["codec"]
        self.fps = config["fps"]
        self.rtsp_url = f"{RTSP_BASE}/{self.path}"
        self.process = None
        self.ws = None
        self.running = True

    async def run(self):
        print(f"[*] Starting feed '{self.path}' ({self.serial}) -> {self.rtsp_url}")

        ffmpeg_cmd = [
            "ffmpeg",
            "-use_wallclock_as_timestamps", "1",
            "-fflags", "+genpts",
            "-f", self.codec,
            "-r", self.fps,
            "-i", "-",
            "-c:v", "copy",
            "-an",
            "-f", "rtsp",
            "-rtsp_transport", "tcp",
            self.rtsp_url,
        ]

        self.process = subprocess.Popen(
            ffmpeg_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        try:
            async with websockets.connect(WS_URI, max_size=10 * 1024 * 1024) as ws:
                self.ws = ws
                await ws.send(json.dumps({"messageId": f"schema_{self.path}", "command": "set_api_schema", "schemaVersion": 21}))
                await ws.send(json.dumps({"messageId": f"start_{self.path}", "command": "device.start_livestream", "serialNumber": self.serial}))

                while self.running:
                    try:
                        # 1-second timeout allows task cancellation to trigger promptly on Ctrl+C
                        msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue

                    data = json.loads(msg)
                    if (
                        data.get("type") == "event"
                        and data.get("event", {}).get("event") == "livestream video data"
                        and data.get("event", {}).get("serialNumber") == self.serial
                    ):
                        buf = bytes(data["event"]["buffer"]["data"])
                        if self.process and self.process.stdin:
                            try:
                                self.process.stdin.write(buf)
                                self.process.stdin.flush()
                            except (BrokenPipeError, OSError):
                                break

        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[!] Error on {self.path}: {e}")
        finally:
            await self.stop()

    async def stop(self):
        self.running = False
        print(f"[*] Stopping feed '{self.path}'...")

        # Kill FFmpeg immediately so standard streams close
        if self.process:
            try:
                self.process.terminate()
                self.process.kill()
            except Exception:
                pass
            self.process = None

        # Send stop command to sleep camera
        if self.ws and not self.ws.closed:
            try:
                await self.ws.send(json.dumps({"messageId": f"stop_{self.path}", "command": "device.stop_livestream", "serialNumber": self.serial}))
                await asyncio.sleep(0.2)
            except Exception:
                pass

async def main():
    streamers = [CameraStreamer(path, cfg) for path, cfg in CAMERAS.items()]
    tasks = [asyncio.create_task(s.run()) for s in streamers]

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def handle_signal():
        print("\n[!] Ctrl+C detected. Cancelling camera feeds...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    await stop_event.wait()
    for t in tasks:
        t.cancel()

    # Wait for tasks to clean up with a 2-second hard timeout
    await asyncio.wait(tasks, timeout=2.0)
    print("[+] Bridge shutdown complete.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
