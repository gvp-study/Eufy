#!/usr/bin/env bash
set -e

DIR="/home/gvp/Eufy"
PYTHON_ENV="$HOME/onvif-env/bin/python3"
GO2RTC_BIN="$DIR/go2rtc"
GO2RTC_CONFIG="$DIR/go2rtc.yaml"
BRIDGE_SCRIPT="$DIR/eufy_rtsp_bridge.py"

cleanup() {
    echo ""
    echo "[!] Exit signal received. Tearing down Eufy streaming stack..."

    # Signal Python bridge first to sleep camera sensors and radios
    if [ -n "$BRIDGE_PID" ] && kill -0 "$BRIDGE_PID" 2>/dev/null; then
        echo "[*] Signaling bridge to sleep cameras (PID: $BRIDGE_PID)..."
        kill -INT "$BRIDGE_PID" 2>/dev/null || true
        wait "$BRIDGE_PID" 2>/dev/null || true
    fi

    # Terminate go2rtc media server
    if [ -n "$GO2RTC_PID" ] && kill -0 "$GO2RTC_PID" 2>/dev/null; then
        echo "[*] Stopping go2rtc (PID: $GO2RTC_PID)..."
        kill -TERM "$GO2RTC_PID" 2>/dev/null || true
        wait "$GO2RTC_PID" 2>/dev/null || true
    fi

    echo "[+] Stack stopped cleanly. All cameras sleeping."
    exit 0
}

trap cleanup INT TERM

echo "=================================================="
echo "          Eufy Local RTSP Streaming Stack         "
echo "=================================================="

# 1. Verify eufy-ws container status
echo "[1/3] Checking eufy-ws Docker container..."
if ! docker ps --format '{{.Names}}' | grep -q "^eufy-ws$"; then
    echo "[*] Starting eufy-ws container..."
    docker start eufy-ws >/dev/null || {
        echo "[ERROR] Failed to start container eufy-ws."
        exit 1
    }
fi

# Wait for WebSocket on port 3000
timeout=10
while ! nc -z 127.0.0.1 3000; do
    sleep 0.5
    timeout=$((timeout - 1))
    if [ "$timeout" -le 0 ]; then
        echo "[ERROR] Port 3000 (eufy-ws) did not become ready."
        exit 1
    fi
done
echo "[+] eufy-ws WebSocket ready on port 3000."

# 2. Launch go2rtc
echo "[2/3] Starting go2rtc media server..."
if pgrep -f "$GO2RTC_BIN" >/dev/null 2>&1; then
    echo "[*] Terminating existing go2rtc instance..."
    pkill -f "$GO2RTC_BIN" || true
    sleep 1
fi

"$GO2RTC_BIN" -config "$GO2RTC_CONFIG" >/dev/null 2>&1 &
GO2RTC_PID=$!

# Wait for RTSP port 8554
timeout=10
while ! nc -z 127.0.0.1 8554; do
    sleep 0.5
    timeout=$((timeout - 1))
    if [ "$timeout" -le 0 ]; then
        echo "[ERROR] go2rtc failed to bind to port 8554."
        exit 1
    fi
done
echo "[+] go2rtc ready on RTSP :8554 (Web dashboard: http://localhost:1984)."

# 3. Launch Python RTSP pipeline
echo "[3/3] Launching Python RTSP pipeline..."
"$PYTHON_ENV" "$BRIDGE_SCRIPT" &
BRIDGE_PID=$!

echo "=================================================="
echo " Streams active:"
echo "   - rtsp://127.0.0.1:8554/solocam1"
echo "   - rtsp://127.0.0.1:8554/solocam2"
echo "   - rtsp://127.0.0.1:8554/doorbell"
echo " Dashboard: http://localhost:1984"
echo " Press Ctrl+C in this terminal to stop all streams."
echo "=================================================="

wait "$BRIDGE_PID"
