#!/usr/bin/env python3
"""
Reference 2-way WebSocket + WebGL2 viewer for Camoufox `--headless` streaming.

Features demonstrated with zero external dependencies (Python stdlib only):
  * Launching Camoufox `--headless` with CAMOU_HEADLESS_STREAM_SOCKET
  * Shared AF_UNIX hub fanning out raw NV12 `CAMF` frames to multiple tabs
  * GPU WebGL2 YUV (NV12) -> RGB fragment shader at up to 60 FPS
  * Forwarding mouse move/down/up (with drag & right-click), wheel, keyboard,
    and UTF-8 paste text back to Camoufox via `CAMI` v1 packets

Usage:
    python3 browser/patches/headless-stream/examples/web_live_control.py
    python3 browser/patches/headless-stream/examples/web_live_control.py --url https://example.com --port 18765
"""

import argparse
import base64
import hashlib
import http.server
import json
import os
import queue
import shutil
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

WS_MAGIC_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
CAMF_HEADER_FMT = "<IIIIIIQ"
CAMF_HEADER_SIZE = struct.calcsize(CAMF_HEADER_FMT)
CAMF_MAGIC = 0x464D4143
CAMI_HEADER_FMT = "<IIIIiiiiI"
CAMI_MAGIC = 0x494D4143

TARGET_PAGE_HTML = b"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Camoufox Interactive Headless</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: radial-gradient(circle at 20% 20%, #0f172a, #020617);
    color: #f8fafc;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    min-height: 100vh;
    padding: 20px;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  .top {
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: rgba(30, 41, 59, 0.8);
    border: 1px solid rgba(148, 163, 184, 0.25);
    border-radius: 12px;
    padding: 14px 20px;
  }
  .clock { font-variant-numeric: tabular-nums; font-size: 26px; font-weight: 700; color: #38bdf8; }
  .badge { background: #10b981; color: #022c22; font-weight: 800; padding: 6px 12px; border-radius: 999px; font-size: 13px; }
  .grid { display: grid; grid-template-columns: 1.3fr 1fr; gap: 16px; flex: 1; }
  .card {
    background: rgba(15, 23, 42, 0.85);
    border: 1px solid rgba(56, 189, 248, 0.25);
    border-radius: 14px;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .stage { position: relative; flex: 1; min-height: 280px; border-radius: 10px; overflow: hidden; background: #090d16; }
  canvas { width: 100%; height: 100%; display: block; }
  .controls { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
  button {
    background: #2563eb; color: #fff; border: none; border-radius: 8px;
    padding: 10px 16px; font-weight: 600; cursor: pointer; font-size: 14px;
  }
  button:hover { background: #3b82f6; }
  input {
    flex: 1; min-width: 180px; background: #020617; color: #f8fafc;
    border: 1px solid #334155; border-radius: 8px; padding: 10px 12px; font-size: 14px;
  }
  .scrollbox {
    height: 180px; overflow-y: auto; border: 1px solid #1e293b; border-radius: 8px;
    padding: 10px; background: #020617; font-size: 13px; line-height: 1.8;
  }
  .log { font-family: monospace; font-size: 12px; color: #94a3b8; }
</style>
</head>
<body>
  <div class="top">
    <div>
      <div style="font-size:18px;font-weight:700">Camoufox --headless Interactive Test Target</div>
      <div style="font-size:12px;color:#94a3b8">Click URL bar above, open tabs (+), click/drag/scroll/type below -- 100% inside --headless</div>
    </div>
    <div id="clock" class="clock">00:00:00.000</div>
    <div class="badge">LIVE 2-WAY</div>
  </div>
  <div class="grid">
    <div class="card">
      <div class="stage"><canvas id="c"></canvas></div>
    </div>
    <div class="card">
      <div class="controls">
        <button id="btn">Click me (0)</button>
        <input id="inp" placeholder="Click & type here..." />
      </div>
      <div id="ev" class="log">Last trusted input: (waiting...)</div>
      <div class="scrollbox" id="sb"></div>
    </div>
  </div>
<script>
const sb = document.getElementById('sb');
for (let i = 1; i <= 50; i++) {
  const row = document.createElement('div');
  row.textContent = `Scroll row #${i} - try mouse wheel or scrollbar drag`;
  sb.appendChild(row);
}
let clicks = 0;
const btn = document.getElementById('btn');
const evLog = document.getElementById('ev');
btn.addEventListener('click', (e) => {
  clicks++;
  btn.textContent = `Click me (${clicks})`;
  evLog.textContent = `click button: isTrusted=${e.isTrusted} detail=${e.detail}`;
});
document.getElementById('inp').addEventListener('input', (e) => {
  evLog.textContent = `input value="${e.target.value}" isTrusted=${e.isTrusted}`;
});
window.addEventListener('wheel', (e) => {
  evLog.textContent = `wheel deltaY=${Math.round(e.deltaY)} wheelDelta=${e.wheelDelta} isTrusted=${e.isTrusted}`;
});

const canvas = document.getElementById('c');
const ctx = canvas.getContext('2d');
const clockEl = document.getElementById('clock');
let frame = 0, lastSec = performance.now(), rafCount = 0, rafFps = 0;
let box = { x: 180, y: 120, w: 96, h: 64, dragging: false, dx: 0, dy: 0 };

function resize() { canvas.width = canvas.clientWidth; canvas.height = canvas.clientHeight; }
window.addEventListener('resize', resize);
resize();

canvas.addEventListener('mousedown', (e) => {
  const r = canvas.getBoundingClientRect();
  const mx = e.clientX - r.left, my = e.clientY - r.top;
  if (mx >= box.x && mx <= box.x + box.w && my >= box.y && my <= box.y + box.h) {
    box.dragging = true; box.dx = mx - box.x; box.dy = my - box.y;
  }
  evLog.textContent = `mousedown (${Math.round(mx)},${Math.round(my)}) isTrusted=${e.isTrusted} detail=${e.detail}`;
});
window.addEventListener('mousemove', (e) => {
  if (!box.dragging) return;
  const r = canvas.getBoundingClientRect();
  box.x = Math.max(0, Math.min(canvas.width - box.w, e.clientX - r.left - box.dx));
  box.y = Math.max(0, Math.min(canvas.height - box.h, e.clientY - r.top - box.dy));
});
window.addEventListener('mouseup', () => { box.dragging = false; });

function draw(now) {
  frame++; rafCount++;
  if (now - lastSec >= 500) {
    rafFps = Math.round((rafCount * 1000) / (now - lastSec));
    rafCount = 0; lastSec = now;
  }
  const d = new Date();
  clockEl.textContent = d.toTimeString().slice(0, 8) + '.' + String(d.getMilliseconds()).padStart(3, '0');
  const w = canvas.width, h = canvas.height;
  ctx.fillStyle = '#090d16'; ctx.fillRect(0, 0, w, h);

  ctx.beginPath(); ctx.lineWidth = 3; ctx.strokeStyle = '#38bdf8';
  for (let x = 0; x <= w; x += 10) {
    const y = h * 0.55 + Math.sin(x * 0.015 + now * 0.005) * 65;
    if (x === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.stroke();

  ctx.fillStyle = box.dragging ? '#f43f5e' : '#22c55e';
  ctx.fillRect(box.x, box.y, box.w, box.h);
  ctx.fillStyle = '#020617'; ctx.font = 'bold 13px sans-serif';
  ctx.fillText('DRAG ME', box.x + 16, box.y + 37);

  ctx.fillStyle = '#f8fafc'; ctx.font = '700 22px monospace';
  ctx.fillText(`FRAME #${frame} | rAF ${rafFps} FPS`, 20, 38);
  requestAnimationFrame(draw);
}
requestAnimationFrame(draw);
</script>
</body>
</html>"""

VIEWER_HTML = b"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Camoufox Interactive --headless Live Stream</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #030712; color: #e2e8f0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    min-height: 100vh; display: flex; flex-direction: column; align-items: center;
    padding: 16px; gap: 12px;
  }
  .bar {
    width: 100%; max-width: 1366px; display: flex; flex-wrap: wrap;
    align-items: center; justify-content: space-between; gap: 12px;
    background: #0f172a; border: 1px solid #1e293b; border-radius: 12px; padding: 10px 16px;
  }
  .stats { display: flex; gap: 16px; font-variant-numeric: tabular-nums; font-size: 13px; }
  .stat strong { color: #38bdf8; }
  button { background: #2563eb; color: #fff; border: none; border-radius: 8px; padding: 7px 14px; font-weight: 600; cursor: pointer; }
  button.off { background: #dc2626; }
  .frame-wrap {
    width: 100%; max-width: 1366px; background: #000;
    border: 2px solid #1e293b; border-radius: 10px; overflow: hidden;
  }
  .frame-wrap.active { border-color: #38bdf8; }
  canvas { width: 100%; height: auto; display: block; outline: none; cursor: default; }
</style>
</head>
<body>
  <div class="bar">
    <div style="font-weight:700;font-size:15px">Camoufox --headless 2-Way Control (Click canvas to focus & interact)</div>
    <div class="stats">
      <div class="stat">Status: <strong id="st">Connecting...</strong></div>
      <div class="stat">FPS: <strong id="fps">0</strong></div>
      <div class="stat">Size: <strong id="sz">-</strong></div>
      <div class="stat">Frames: <strong id="cnt">0</strong></div>
    </div>
    <button id="toggle">Disconnect</button>
  </div>
  <div id="wrap" class="frame-wrap"><canvas id="gl" width="1366" height="768" tabindex="0"></canvas></div>
<script>
const canvas = document.getElementById('gl');
const wrap = document.getElementById('wrap');
const stEl = document.getElementById('st');
const fpsEl = document.getElementById('fps');
const szEl = document.getElementById('sz');
const cntEl = document.getElementById('cnt');
const btn = document.getElementById('toggle');

const gl = canvas.getContext('webgl2');
const vsSrc = `#version 300 es
in vec2 aPos;
out vec2 vUv;
void main() {
  vUv = vec2((aPos.x + 1.0) * 0.5, (1.0 - aPos.y) * 0.5);
  gl_Position = vec4(aPos, 0.0, 1.0);
}`;
const fsSrc = `#version 300 es
precision mediump float;
uniform sampler2D uY;
uniform sampler2D uUV;
in vec2 vUv;
out vec4 fragColor;
void main() {
  float y = (texture(uY, vUv).r * 255.0 - 16.0) / 219.0;
  vec2 uv = (texture(uUV, vUv).rg * 255.0 - 128.0) / 224.0;
  float r = y + 1.402 * uv.y;
  float g = y - 0.344136 * uv.x - 0.714136 * uv.y;
  float b = y + 1.772 * uv.x;
  fragColor = vec4(clamp(vec3(r, g, b), 0.0, 1.0), 1.0);
}`;

function compile(type, src) {
  const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); return s;
}
const prog = gl.createProgram();
gl.attachShader(prog, compile(gl.VERTEX_SHADER, vsSrc));
gl.attachShader(prog, compile(gl.FRAGMENT_SHADER, fsSrc));
gl.linkProgram(prog); gl.useProgram(prog);

const buf = gl.createBuffer();
gl.bindBuffer(gl.ARRAY_BUFFER, buf);
gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1, 1,-1, -1,1, 1,1]), gl.STATIC_DRAW);
const loc = gl.getAttribLocation(prog, 'aPos');
gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);

function mkTex(unit, name) {
  const t = gl.createTexture();
  gl.activeTexture(gl.TEXTURE0 + unit); gl.bindTexture(gl.TEXTURE_2D, t);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  gl.uniform1i(gl.getUniformLocation(prog, name), unit);
  return t;
}
const yTex = mkTex(0, 'uY'), uvTex = mkTex(1, 'uUV');
gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);

let ws = null, total = 0, winFrames = 0, winStart = performance.now();
let frameW = 1366, frameH = 768;

function modsBitmask(e) {
  return (e.shiftKey ? 1 : 0) | (e.ctrlKey ? 2 : 0) | (e.altKey ? 4 : 0) | (e.metaKey ? 8 : 0);
}
function toFrameXY(e) {
  const r = canvas.getBoundingClientRect();
  return [
    Math.max(0, Math.min(frameW - 1, Math.round((e.clientX - r.left) * (frameW / r.width)))),
    Math.max(0, Math.min(frameH - 1, Math.round((e.clientY - r.top) * (frameH / r.height)))),
  ];
}
function sendInput(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(obj));
  }
}

canvas.addEventListener('focus', () => wrap.classList.add('active'));
canvas.addEventListener('blur', () => wrap.classList.remove('active'));
canvas.addEventListener('contextmenu', (e) => e.preventDefault());
canvas.addEventListener('mousemove', (e) => {
  const [x, y] = toFrameXY(e);
  sendInput({ t: 1, m: modsBitmask(e), x, y, b: 0, d: 0 });
});
canvas.addEventListener('mousedown', (e) => {
  canvas.focus({ preventScroll: true });
  e.preventDefault();
  const [x, y] = toFrameXY(e);
  sendInput({ t: 2, m: modsBitmask(e), x, y, b: e.button, d: 0 });
});
canvas.addEventListener('mouseup', (e) => {
  e.preventDefault();
  const [x, y] = toFrameXY(e);
  sendInput({ t: 3, m: modsBitmask(e), x, y, b: e.button, d: 0 });
});
canvas.addEventListener('wheel', (e) => {
  e.preventDefault();
  const [x, y] = toFrameXY(e);
  sendInput({ t: 4, m: modsBitmask(e), x, y, b: Math.round(e.deltaX), d: Math.round(e.deltaY) });
}, { passive: false });
canvas.addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'v') {
    return;  // Allow browser paste event to deliver host clipboard text via Text packet.
  }
  e.preventDefault();
  sendInput({
    t: 5, m: modsBitmask(e), x: e.repeat ? 1 : 0, y: e.location || 0,
    b: 0, d: e.keyCode || 0, s: `${e.key}\t${e.code || ''}`
  });
});
canvas.addEventListener('keyup', (e) => {
  e.preventDefault();
  sendInput({
    t: 6, m: modsBitmask(e), x: 0, y: e.location || 0,
    b: 0, d: e.keyCode || 0, s: `${e.key}\t${e.code || ''}`
  });
});
canvas.addEventListener('paste', (e) => {
  const text = e.clipboardData ? e.clipboardData.getData('text/plain') : '';
  if (!text) return;
  e.preventDefault();
  sendInput({ t: 7, m: 0, x: 0, y: 0, b: 0, d: 0, s: text });
});

function connect() {
  if (ws) return;
  stEl.textContent = 'Connecting...';
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => {
    stEl.textContent = 'Streaming'; btn.textContent = 'Disconnect'; btn.classList.remove('off');
    winFrames = 0; winStart = performance.now();
  };
  ws.onmessage = (ev) => {
    const dv = new DataView(ev.data);
    if (dv.getUint32(0, true) !== 0x464D4143) return;
    frameW = dv.getUint32(8, true); frameH = dv.getUint32(12, true);
    const yLen = frameW * frameH, bytes = new Uint8Array(ev.data, 32);
    if (canvas.width !== frameW || canvas.height !== frameH) {
      canvas.width = frameW; canvas.height = frameH; gl.viewport(0, 0, frameW, frameH);
    }
    gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, yTex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.R8, frameW, frameH, 0, gl.RED, gl.UNSIGNED_BYTE, bytes.subarray(0, yLen));
    gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, uvTex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RG8, frameW >> 1, frameH >> 1, 0, gl.RG, gl.UNSIGNED_BYTE, bytes.subarray(yLen));
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);

    total++; winFrames++; cntEl.textContent = total; szEl.textContent = `${frameW}x${frameH}`;
    const now = performance.now();
    if (now - winStart >= 500) {
      fpsEl.textContent = Math.round((winFrames * 1000) / (now - winStart));
      winFrames = 0; winStart = now;
    }
  };
  ws.onclose = () => {
    ws = null; stEl.textContent = 'Paused (0% CPU)'; btn.textContent = 'Reconnect'; btn.classList.add('off');
  };
}
btn.onclick = () => { if (ws) ws.close(); else connect(); };
connect();
</script>
</body>
</html>"""


def resolve_camoufox_binary(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    for var in ("CAMOUFOX_EXECUTABLE_PATH", "CAMOUFOX_BINARY"):
        if os.environ.get(var):
            return Path(os.environ[var]).expanduser().resolve()
    browser_dir = Path(__file__).resolve().parents[3]
    candidates = sorted(
        list(browser_dir.glob("camoufox-*/obj-*/dist/Camoufox.app/Contents/MacOS/camoufox"))
        + list(browser_dir.glob("camoufox-*/obj-*/dist/bin/camoufox-bin"))
    )
    if not candidates:
        raise SystemExit("No built Camoufox binary found; pass --binary or set CAMOUFOX_EXECUTABLE_PATH")
    return candidates[-1]


def send_ws_binary(sock: socket.socket, payload: bytes) -> None:
    n = len(payload)
    if n < 126:
        hdr = struct.pack("!BB", 0x82, n)
    elif n <= 0xFFFF:
        hdr = struct.pack("!BBH", 0x82, 126, n)
    else:
        hdr = struct.pack("!BBQ", 0x82, 127, n)
    sock.sendall(hdr + payload)


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("closed")
        buf.extend(chunk)
    return bytes(buf)


def read_ws_client_message(sock: socket.socket):
    hdr = recv_exact(sock, 2)
    opcode = hdr[0] & 0x0F
    masked = (hdr[1] & 0x80) != 0
    length = hdr[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", recv_exact(sock, 8))[0]
    mask = recv_exact(sock, 4) if masked else b""
    data = bytearray(recv_exact(sock, length)) if length > 0 else bytearray()
    if masked:
        for i in range(length):
            data[i] ^= mask[i % 4]
    return opcode, bytes(data)


class StreamHub:
    """Broadcasts `CAMF` frames from one AF_UNIX connection and forwards `CAMI` input back."""

    def __init__(self, sock_path: str):
        self.sock_path = sock_path
        self.lock = threading.Lock()
        self.subscribers: set[queue.Queue[bytes]] = set()
        self.latest_packet: bytes | None = None
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.active_unix_sock: socket.socket | None = None
        self.send_lock = threading.Lock()
        threading.Thread(target=self._pump_loop, daemon=True).start()

    def subscribe(self) -> queue.Queue[bytes]:
        q: queue.Queue[bytes] = queue.Queue(maxsize=2)
        with self.lock:
            self.subscribers.add(q)
            if self.latest_packet is not None:
                q.put_nowait(self.latest_packet)
        self.wake_event.set()
        return q

    def unsubscribe(self, q: queue.Queue[bytes]) -> None:
        with self.lock:
            self.subscribers.discard(q)

    def send_input_packet(self, msg: dict) -> None:
        s_bytes = msg.get("s", "").encode("utf-8") if msg.get("s") else b""
        hdr = struct.pack(
            CAMI_HEADER_FMT,
            CAMI_MAGIC,
            1,
            int(msg["t"]),
            int(msg.get("m", 0)),
            int(msg.get("x", 0)),
            int(msg.get("y", 0)),
            int(msg.get("b", 0)),
            int(msg.get("d", 0)),
            len(s_bytes),
        )
        with self.send_lock:
            s = self.active_unix_sock
            if s is not None:
                try:
                    s.sendall(hdr + s_bytes)
                except OSError:
                    pass

    def _broadcast(self, packet: bytes) -> int:
        with self.lock:
            self.latest_packet = packet
            subs = list(self.subscribers)
        for q in subs:
            while True:
                try:
                    q.put_nowait(packet)
                    break
                except queue.Full:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        break
        return len(subs)

    def _pump_loop(self) -> None:
        while not self.stop_event.is_set():
            with self.lock:
                has_subs = bool(self.subscribers)
            if not has_subs:
                self.wake_event.wait(0.2)
                self.wake_event.clear()
                continue

            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as unix_sock:
                    unix_sock.settimeout(5.0)
                    unix_sock.connect(self.sock_path)
                    with self.send_lock:
                        self.active_unix_sock = unix_sock
                    try:
                        while not self.stop_event.is_set():
                            with self.lock:
                                if not self.subscribers:
                                    break
                            hdr = recv_exact(unix_sock, CAMF_HEADER_SIZE)
                            magic, ver, _w, _h, _fmt, plen, _pts = struct.unpack(
                                CAMF_HEADER_FMT, hdr
                            )
                            if magic != CAMF_MAGIC or ver != 1:
                                break
                            payload = recv_exact(unix_sock, plen)
                            if self._broadcast(hdr + payload) == 0:
                                break
                    finally:
                        with self.send_lock:
                            if self.active_unix_sock is unix_sock:
                                self.active_unix_sock = None
            except (EOFError, OSError):
                time.sleep(0.1)


def make_handler(hub: StreamHub):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/target":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(TARGET_PAGE_HTML)))
                self.end_headers()
                self.wfile.write(TARGET_PAGE_HTML)
                return

            if self.path == "/ws" and self.headers.get("Upgrade", "").lower() == "websocket":
                key = self.headers.get("Sec-WebSocket-Key", "")
                accept = base64.b64encode(
                    hashlib.sha1((key + WS_MAGIC_GUID).encode("ascii")).digest()
                ).decode("ascii")
                self.send_response(101, "Switching Protocols")
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept)
                self.end_headers()

                sub_q = hub.subscribe()
                closed = threading.Event()
                ws_lock = threading.Lock()

                def read_client_loop():
                    self.connection.settimeout(None)
                    try:
                        while not closed.is_set():
                            op, data = read_ws_client_message(self.connection)
                            if op == 8:
                                break
                            if op == 1 and data:
                                try:
                                    hub.send_input_packet(json.loads(data.decode("utf-8")))
                                except Exception:
                                    pass
                            elif op == 9:
                                with ws_lock:
                                    self.connection.sendall(b"\x8A\x00")
                    except (EOFError, OSError):
                        pass
                    finally:
                        closed.set()

                threading.Thread(target=read_client_loop, daemon=True).start()
                try:
                    while not closed.is_set():
                        try:
                            pkt = sub_q.get(timeout=0.5)
                        except queue.Empty:
                            with ws_lock:
                                self.connection.sendall(b"\x89\x00")
                            continue
                        with ws_lock:
                            send_ws_binary(self.connection, pkt)
                except (EOFError, OSError, BrokenPipeError):
                    pass
                finally:
                    closed.set()
                    hub.unsubscribe(sub_q)
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(VIEWER_HTML)))
            self.end_headers()
            self.wfile.write(VIEWER_HTML)

        def log_message(self, *args):
            pass

    return Handler


class ReusableThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    parser = argparse.ArgumentParser(description="2-way WebGL2 viewer for --headless Camoufox")
    parser.add_argument("--binary", help="Path to built camoufox / camoufox-bin")
    parser.add_argument("--url", help="Initial URL to load in headless Camoufox")
    parser.add_argument("--port", type=int, default=18765, help="HTTP/WS viewer port (default 18765)")
    parser.add_argument("--fps", type=int, default=60, help="Target stream FPS (1..120, default 60)")
    parser.add_argument("--size", default="1280,720", help="Window size W,H (default 1280,720)")
    args = parser.parse_args()

    binary = resolve_camoufox_binary(args.binary)
    profile = tempfile.mkdtemp(prefix="camou-live-prof-")
    sock_dir = tempfile.mkdtemp(prefix="clive-", dir="/tmp")
    sock_path = os.path.join(sock_dir, "stream.sock")
    Path(profile, "user.js").write_text(
        'user_pref("browser.shell.checkDefaultBrowser", false);\n'
        'user_pref("datareporting.policy.dataSubmissionPolicyBypassNotification", true);\n'
    )

    hub = StreamHub(sock_path)
    httpd = ReusableThreadingHTTPServer(("127.0.0.1", args.port), make_handler(hub))
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    target_url = args.url or f"http://127.0.0.1:{port}/target"
    env = dict(os.environ)
    env["CAMOU_HEADLESS_STREAM_SOCKET"] = sock_path
    env["CAMOU_HEADLESS_STREAM_FPS"] = str(args.fps)
    env["CAMOU_HEADLESS_STREAM_FORMAT"] = "nv12"

    proc = subprocess.Popen(
        [
            str(binary),
            "-headless",
            "-no-remote",
            "-profile",
            profile,
            f"--window-size={args.size}",
            target_url,
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        deadline = time.monotonic() + 20.0
        while not os.path.exists(sock_path):
            if proc.poll() is not None:
                return 1
            if time.monotonic() > deadline:
                print("Timed out waiting for stream socket", file=sys.stderr)
                return 1
            time.sleep(0.05)

        print(f"Viewer URL:  http://127.0.0.1:{port}/", flush=True)
        print(f"WebSocket:   ws://127.0.0.1:{port}/ws", flush=True)
        print(f"Unix socket: {sock_path} (pid={proc.pid})", flush=True)
        proc.wait()
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        hub.stop_event.set()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        httpd.shutdown()
        shutil.rmtree(profile, ignore_errors=True)
        shutil.rmtree(sock_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
