# Eufy Local RTSP & WebRTC Streaming Bridge

A low-latency, local-network video bridge running on a Raspberry Pi (`utilitypi`) to expose battery-powered and wired Eufy cameras (SoloCams and Doorbell) as standard R TSP and WebRTC feeds over Tailscale and local LAN.

---

># Architecture Overview

```text
Eufy Cloud / P2P
        │
        ▼
[eufy-security-ws] (Docker :3000)
        │  (WebSocket raw elementary H.264 / HEVC NAL units)
        ▼
[eufy_rtsp_bridge.py]
        │  (TCP loopback servers: 127.0.0.1:9001 - 9003)
        ▼
   (go2rtc] (Binary orchestrator)
        │  (exec:ffmpeg pulls loopback, packetizes, and restreams)
       ┌─────────────────────────────┌─────────────────────────────┑
       ▼                              ▼                              ▼
  RTSP (:8554)                   WebRTC (:8555)                 Web UI (:894)
  (VLC / ffplay)                 (Low-latency Web)              (Dashboard)
```

### Why This Design?
- **RTSP Push Ingestion Issues Avoided:** Directly pushing raw elementary NAL units from FFmpeg to an RTSP server triggers DTS/PTS timestamp discontinuities (`End of file` / `Broken pipe`).
- **Zero-Block TCP Loopback:** The Python bridge hosts local TCP streaming servers (`:9001` - `:9003`). `go2rtc` spawns `ffmpeg` as an on-demand `exec` consumer only when an active viewer connects.
- **Tailscale Accessible:** Services bind explicitly to `0.0.0.0` so they are reachable over Tailscale IP (`100.68.193.42`) and LAN (`10.0.0.66`).

---

## Directory Structure

```text
~/Eufy/
├──eufy_rtsp_bridge.py     # Python bridge (WebScocket -> TCP loopback)
├──go2rtc                 # go2rtc arm64 binary
├──go2rtc.yaml             # go2rtc streams & network configuration
␼� 8� start_eufy_stack.sh     # Orchestrator script with cleanup handlers
┼── README.md
 ```

---

## Configuration Files

### 1. `go2rtc.yaml`
```yaml
api:
  listen: "0.0.0.0:1984"

rtsp:
  listen: "0.0.0.0:8554"

webrtc:
  listen: "0.0.0.0:8555"
  candidates:
    - "100.68.193.42:8555"

streams:
  solocam1:
    - exec:ffmpeg -hide_banner -loglevel warning -re -fflags +genpts+nobuffer -f hevc -r 15 -i tcp://127.0.0.1:9001 -c:v copy -an -f rtsp {output}
  solocam2:
    - exec:ffmpeg -hide_banner -loglevel warning -re -fflags +genpts+nobuffer -f hevc -r 15 -i tcp://127.0.0.1:9002 -c:v copy -an -f rtsp {output}
  doorbell:
    - exec:ffmpeg -hide_banner -loglevel warning -re -fflags +genpts+nobuffer -f h264 -r 15 -i tcp://127.0.0.1:9003 -c:v copy -an -f rtsp {output}
```

### 2. Stream Mapping
|size Feeds | Model | Codec | Local Ingest Port | RTSP URL |
| :--- | :--- | :--- | :--- | :--- |
| `solocam1` | T8170 (SoloCam) | HEVC (H.265) | `127.0.0.1:9001` | `rtsp://<host>:8554/solocam1` |
| `solocam2` | T8170 (SoloCam) | HEVC (H.265) | `127.0.0.1:9002` | `rtsp://<host>:8554/solocam2` |
| `doorbell` | T8214 (Dual Doorbell) | H.264 | `127.0.0.1:9003` | `rtsp://<host>:8554/doorbell` |

---

## Operating Instructions

### Launching the Stack (tmux)
Run the stack inside a persistent `tmux` session to ensure uninterrupted streaming:

```bash
tmux new -s eufy -d
tmux send-keys -t eufy "~/Eufy/start_eufy_stack.sh" C-m
```

To attach and monitor logs:
```bash
tmux attach -t eufy
```
To detach without stopping the feeds: press `Ctrl + b`, then `d`.

### Viewing Streams
- **Web Dashboard / WebRTC:** http://100.68.193.42:1984
- **Low-Latency CLI Playback:**
  ```bash
  ffplay -rtsp_transport tcp -flags low_delay "rtsp://100.68.193.42:8554/solocam1"
  ffplay -rtsp_transport tcp -flags low_delay "rtsp://100.68.193.42:8554/doorbell"
  ```

---

## Key Troubleshooting Takeaways

1. **Systemd Port Collision:**
   A background `go2rtc.service` daemon may automatically resurrect and bind `:8554` and `:1984` to `127.0.0.1`, ignoring stack config changes and causing `Connection refused` on external interfaces.
   - Fix: `sudo systemctl stop go2rtc && sudo systemctl disable go2rtc`
2. **WebSockets v14+ Compatibility:**
   `ClientConnection` objects in modern Python `websockets` do not expose `.closed`. Avoid checking `ws.closed` directly; rely on connection context managers or `.close_code`.
3. **FFmpeg Timestamps on Raw Elementary Streams:**
   Piping raw HEVC/H.264 NAL units without container timestamps requires `-fflags +genpts` and `-re` to pace frames at real-time speeds without saturating the `go2rtc` buffer.
