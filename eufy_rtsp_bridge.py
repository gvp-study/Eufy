#!/usr/bin/env python3
import asyncio
import json
import signal
import sys
import websockets

WS_URI = "ws://127.0.0.1:3000"

CAMERAS = {
    "solocam1": {"serial": "T8170T1125360152", "port": 9001},
    "solocam2": {"serial": "T8170T1125372768", "port": 9002},
    "doorbell": {"serial": "T8214511253325BB", "port": 9003},
}

class CameraBroadcaster:
    def __init__(self, path: str, serial: str, port: int):
        self.path = path
        self.serial = serial
        self.port = port
        self.ws = None
        self.running = True
        self.clients = set()
        self.server = None

    async def client_connected(self, reader, writer):
        self.clients.add(writer)
        try:
            while self.running:
                await asyncio.sleep(1)
        except Exception:
            pass
        finally:
            self.clients.discard(writer)
            writer.close()
            await writer.wait_closed()

    async def run(self):
        print(f"[*] Starting feed '{self.path}' ({self.serial}) on tcp://127.0.0.1:{self.port}")
        self.server = await asyncio.start_server(self.client_connected, "127.0.0.1", self.port)

        try:
            async with websockets.connect(WS_URI, max_size=10 * 1024 * 1024) as ws:
                self.ws = ws
                await ws.send(json.dumps({"messageId": f"schema_{self.path}", "command": "set_api_schema", "schemaVersion": 21}))
                await ws.send(json.dumps({"messageId": f"start_{self.path}", "command": "device.start_livestream", "serialNumber": self.serial}))

                while self.running:
                    try:
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
                        for writer in list(self.clients):
                            try:
                                writer.write(buf)
                                await writer.drain()
                            except Exception:
                                self.clients.discard(writer)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[!] Error on {self.path}: {e}")
        finally:
            await self.stop()

    async def stop(self):
        self.running = False
        print(f"[*] Stopping feed '{self.path}'...")

        if self.server:
            self.server.close()
            await self.server.wait_closed()

        for writer in list(self.clients):
            try:
                writer.close()
            except Exception:
                pass
        self.clients.clear()

        if self.ws:
            try:
                await self.ws.send(json.dumps({"messageId": f"stop_{self.path}", "command": "device.stop_livestream", "serialNumber": self.serial}))
                await asyncio.sleep(0.2)
            except Exception:
                pass

async def main():
    broadcasters = [CameraBroadcaster(p, c["serial"], c["port"]) for p, c in CAMERAS.items()]
    tasks = [asyncio.create_task(b.run()) for b in broadcasters]

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def handle_signal():
        print("\n[!] Exit signal received. Shutting down bridge...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    await stop_event.wait()
    for t in tasks:
        t.cancel()

    await asyncio.wait(tasks, timeout=2.0)
    print("[+] Bridge shutdown complete.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
