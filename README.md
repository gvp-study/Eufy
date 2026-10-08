# Eufy Local RTSP Streaming Stack

Low-latency local RTSP bridge pipeline for battery-powered Eufy SoloCams and Video Doorbell on Linux.

## Architecture
- **eufy-security-ws**: Docker container handling cloud auth and P2P wake/handshakes.
- **eufy_rtsp_bridge.py**: Asynchronous WebSocket client that ingests H.264/H.265 NAL streams and pipes them via FFmpeg with generated monotonic wallclock timestamps into RTSP endpoints.
- **go2rtc**: Local RTSP and WebRTC media server (`:8554` RTSP, `:1984` Web UI).
- **start_eufy_stack.sh**: Process orchestrator with automatic `SIGINT`/`SIGTERM` traps to return cameras to sleep.

## Prerequisites
- Docker (`bropat/eufy-security-ws`)
- `ffmpeg`
- Python 3 with `websockets`
- `go2rtc` binary placed in this directory

## Endpoints
- `rtsp://127.0.0.1:8554/solocam1` (HEVC)
- `rtsp://127.0.0.1:8554/solocam2` (HEVC)
- `rtsp://127.0.0.1:8554/doorbell` (AVC)
