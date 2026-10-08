#!/usr/bin/env python3
"""On-demand bridge: Eufy livestreams (via eufy-security-ws) -> raw video on local TCP ports.

go2rtc's exec:ffmpeg sources connect to tcp://127.0.0.1:900x only while someone is
watching. A camera's livestream is started when the first client connects and stopped
IDLE_STOP_SEC after the last one leaves, so the battery cameras sleep otherwise.

Also:
- Late joiners first get the stream header (VPS/SPS/PPS) plus the frames since the last
  keyframe, so H.265 decoders never start blind (that is what made the green garbage).
- One shared eufy-ws connection, reconnected with backoff if it drops. A problem with
  one camera never stops the others.
- Hard cap MAX_LIVESTREAM_SEC per livestream, as a backstop for the batteries.
"""
import asyncio
import json
import signal
import time
import websockets

WS_URI = "ws://127.0.0.1:3000"
SCHEMA_VERSION = 21
IDLE_STOP_SEC = 10          # stop the camera this long after the last viewer leaves
MAX_LIVESTREAM_SEC = 600    # never keep a camera awake longer than this in one go
COOLDOWN_SEC = 60           # after a forced stop, refuse viewers this long (go2rtc retries instantly)
CLIENT_QUEUE_CHUNKS = 600   # a client this far behind is dropped instead of stalling everyone
GOP_CACHE_MAX = 6 * 1024 * 1024

CAMERAS = {
    "solocam1": {"serial": "T8170T1125360152", "port": 9001, "codec": "h265"},
    "solocam2": {"serial": "T8170T1125372768", "port": 9002, "codec": "h265"},
    "doorbell": {"serial": "T8214511253325BB", "port": 9003, "codec": "h264"},
}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def nal_units(buf: bytes, codec: str):
    """Yield (nal_type, bytes incl. start code) for each NAL unit in an Annex-B chunk."""
    starts = []
    i = buf.find(b"\x00\x00\x01")
    while i != -1:
        starts.append(i - 1 if i > 0 and buf[i - 1] == 0 else i)
        i = buf.find(b"\x00\x00\x01", i + 3)
    for k, st in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else len(buf)
        hdr = st + (4 if buf[st:st + 4] == b"\x00\x00\x00\x01" else 3)
        if hdr < len(buf):
            b = buf[hdr]
            yield ((b >> 1) & 0x3F if codec == "h265" else b & 0x1F), buf[st:end]


PARAM_TYPES = {"h265": (32, 33, 34), "h264": (7, 8)}          # VPS/SPS/PPS, SPS/PPS
KEYFRAME_TYPES = {"h265": range(16, 22), "h264": (5,)}         # IRAP / IDR slices


class Camera:
    def __init__(self, bridge, name, serial, port, codec):
        self.bridge, self.name, self.serial, self.port, self.codec = bridge, name, serial, port, codec
        self.clients = set()          # asyncio.Queue per connected client
        self.streaming = False        # livestream requested from eufy-ws
        self.started_at = 0.0
        self.params = {}              # latest VPS/SPS/PPS by NAL type (sent once, at stream start)
        self.gop = bytearray()        # frames since the last keyframe
        self.idle_task = None
        self.blocked_until = 0.0      # no new livestream before this time (after a forced stop)

    async def serve(self):
        server = await asyncio.start_server(self.client, "127.0.0.1", self.port)
        log(f"[*] {self.name} ({self.serial}) waiting for viewers on tcp://127.0.0.1:{self.port}")
        async with server:
            await server.serve_forever()

    async def client(self, reader, writer):
        if self.bridge.closing or self.bridge.ws is None or time.time() < self.blocked_until:
            writer.close()            # shutting down, eufy-ws not connected, or cooling down
            return
        q = asyncio.Queue(CLIENT_QUEUE_CHUNKS)
        if self.params and self.gop:           # catch a newcomer up: header + frames since the keyframe
            q.put_nowait(b"".join(self.params[t] for t in sorted(self.params)) + bytes(self.gop))
        self.clients.add(q)
        log(f"[+] {self.name}: viewer connected ({len(self.clients)} now)")
        if self.idle_task:
            self.idle_task.cancel()
            self.idle_task = None
        if not self.streaming:
            await self.start()

        async def pump():
            while True:
                chunk = await q.get()
                if chunk is None:
                    return
                writer.write(chunk)
                await writer.drain()

        send = asyncio.create_task(pump())
        eof = asyncio.create_task(reader.read(1))      # ffmpeg never sends; b"" means it hung up
        try:
            await asyncio.wait({send, eof}, return_when=asyncio.FIRST_COMPLETED)
        except Exception:
            pass
        finally:
            for t in (send, eof):
                t.cancel()
            self.clients.discard(q)
            try:
                writer.close()
            except Exception:
                pass
            log(f"[-] {self.name}: viewer left ({len(self.clients)} now)")
            if not self.clients and self.streaming and not self.idle_task:
                self.idle_task = asyncio.create_task(self.stop_when_idle())

    async def stop_when_idle(self):
        try:
            await asyncio.sleep(IDLE_STOP_SEC)
        except asyncio.CancelledError:
            return
        self.idle_task = None
        if not self.clients:
            await self.stop("no viewers")

    async def start(self):
        self.streaming, self.started_at = True, time.time()
        self.params.clear()
        self.gop.clear()
        log(f"[>] {self.name}: starting livestream (waking camera)")
        await self.bridge.command("device.start_livestream", self.serial)

    async def stop(self, why, cooldown=False):
        if cooldown:
            self.blocked_until = time.time() + COOLDOWN_SEC
        if self.streaming:
            log(f"[<] {self.name}: stopping livestream ({why})")
            await self.bridge.command("device.stop_livestream", self.serial)
        self.streaming = False
        self.params.clear()
        self.gop.clear()
        self.drop_clients()

    def drop_clients(self):
        for q in list(self.clients):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass
        self.clients.clear()

    def on_video(self, buf: bytes):
        if not self.streaming:
            return
        keyframe = False
        for t, nal in nal_units(buf, self.codec):
            if t in PARAM_TYPES[self.codec]:
                self.params[t] = nal
            elif t in KEYFRAME_TYPES[self.codec]:
                keyframe = True
        if keyframe:
            self.gop = bytearray(buf)
        elif self.gop:
            self.gop += buf
            if len(self.gop) > GOP_CACHE_MAX:     # keyframes very rare: stop caching, keep streaming
                self.gop.clear()
        for q in list(self.clients):
            try:
                q.put_nowait(buf)
            except asyncio.QueueFull:
                log(f"[!] {self.name}: viewer too slow, dropping it")
                self.clients.discard(q)
                q._queue.clear()
                q.put_nowait(None)

    def on_stopped(self):
        if self.streaming:
            log(f"[!] {self.name}: camera ended the livestream")
            self.streaming = False
            self.blocked_until = time.time() + COOLDOWN_SEC
            self.drop_clients()     # viewers see the stream end and can reconnect


class Bridge:
    def __init__(self):
        self.ws = None
        self.msg_id = 0
        self.closing = False
        self.cams = {c["serial"]: Camera(self, n, c["serial"], c["port"], c["codec"]) for n, c in CAMERAS.items()}

    async def command(self, cmd, serial):
        if not self.ws:
            return
        self.msg_id += 1
        try:
            await self.ws.send(json.dumps({"messageId": f"{cmd}-{self.msg_id}", "command": cmd, "serialNumber": serial}))
        except Exception as e:
            log(f"[!] could not send {cmd}: {e}")

    async def eufy_link(self):
        backoff = 2
        while True:
            try:
                async with websockets.connect(WS_URI, max_size=16 * 1024 * 1024) as ws:
                    self.ws, backoff = ws, 2
                    await ws.send(json.dumps({"messageId": "schema", "command": "set_api_schema", "schemaVersion": SCHEMA_VERSION}))
                    await ws.send(json.dumps({"messageId": "listen", "command": "start_listening"}))
                    log("[+] connected to eufy-ws")
                    for cam in self.cams.values():        # make sure nothing was left streaming
                        await self.command("device.stop_livestream", cam.serial)
                    async for msg in ws:
                        try:
                            self.handle(json.loads(msg))
                        except Exception as e:
                            log(f"[!] bad message from eufy-ws: {e}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log(f"[!] eufy-ws connection lost: {e}; retrying in {backoff}s")
            self.ws = None
            for cam in self.cams.values():
                cam.streaming = False
                cam.drop_clients()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def handle(self, data):
        if data.get("type") == "result":
            if data.get("success") is False and not str(data.get("messageId", "")).startswith("device.stop_livestream"):
                log(f"[!] eufy-ws refused {data.get('messageId')}: {data.get('errorCode')}")
            return
        ev = data.get("event") or {}
        cam = self.cams.get(ev.get("serialNumber"))
        if data.get("type") != "event" or cam is None:
            return
        name = ev.get("event")
        if name == "livestream video data":
            cam.on_video(bytes(ev["buffer"]["data"]))
        elif name == "livestream started":
            log(f"[+] {cam.name}: camera awake, video flowing")
        elif name == "livestream stopped":
            cam.on_stopped()

    async def watchdog(self):
        while True:
            await asyncio.sleep(5)
            for cam in self.cams.values():
                if cam.streaming and time.time() - cam.started_at > MAX_LIVESTREAM_SEC:
                    await cam.stop(f"{MAX_LIVESTREAM_SEC // 60} min limit", cooldown=True)

    async def shutdown(self):
        self.closing = True           # refuse reconnects from here on
        for cam in self.cams.values():
            if cam.streaming:
                await cam.stop("bridge shutting down")
        await asyncio.sleep(0.3)


async def main():
    bridge = Bridge()
    tasks = [asyncio.create_task(bridge.eufy_link()), asyncio.create_task(bridge.watchdog())]
    tasks += [asyncio.create_task(c.serve()) for c in bridge.cams.values()]
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    log("[!] exit signal: putting cameras to sleep")
    await bridge.shutdown()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    log("[+] bridge stopped")


if __name__ == "__main__":
    asyncio.run(main())
