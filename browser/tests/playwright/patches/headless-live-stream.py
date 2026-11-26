"""
Verify HeadlessStreamServer streams live compositor frames from `--headless`
over CAMOU_HEADLESS_STREAM_SOCKET and accepts 2-way `CAMI` input back into
HeadlessWidget with anti-detect parity (`isTrusted`, double-click detail,
native wheel ticks, keyboard typing).

    python3 browser/tests/playwright/patches/headless-live-stream.py --binary ...
"""

import hashlib
import http.server
import json
import os
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

HEADER_FMT = "<IIIIIIQ"
HEADER_SIZE = struct.calcsize(HEADER_FMT)
MAGIC = 0x464D4143
CAMI_FMT = "<IIIIiiiiI"
CAMI_MAGIC = 0x494D4143
FMT_NV12 = 2
LOAD_TIMEOUT_S = 30.0
STREAM_WINDOW_S = 2.0

PAGE = b"""<!doctype html><meta charset=utf-8><title>headless-stream</title>
<body style="margin:0;background:#0f172a;overflow:hidden">
<input id="inp" style="position:fixed;inset:0;width:100vw;height:100vh;opacity:0.01;z-index:2" />
<canvas id="c" width="800" height="500"></canvas>
<script>
const ctx = document.getElementById('c').getContext('2d');
const inp = document.getElementById('inp');
let n = 0;
const report = (kind, payload) =>
  fetch('/event', {method: 'POST', body: JSON.stringify({kind, ...payload})}).catch(() => {});

inp.addEventListener('dblclick', (e) => {
  report('dblclick', {trusted: e.isTrusted, detail: e.detail});
});
window.addEventListener('wheel', (e) => {
  report('wheel', {trusted: e.isTrusted, wheelDelta: e.wheelDelta, deltaY: e.deltaY});
});
inp.addEventListener('input', (e) => {
  report('input', {trusted: e.isTrusted, value: e.target.value});
});

function tick() {
  n++;
  ctx.fillStyle = '#0f172a';
  ctx.fillRect(0, 0, 800, 500);
  const x = (n * 19) % 720;
  ctx.fillStyle = n % 2 === 0 ? '#38bdf8' : '#f43f5e';
  ctx.fillRect(x, 80, 120, 260);
  ctx.fillStyle = '#f8fafc';
  ctx.font = 'bold 48px sans-serif';
  ctx.fillText('FRAME #' + n, 40, 60);
  requestAnimationFrame(tick);
}
requestAnimationFrame(tick);
</script></body>"""


def serve_page():
    events = []
    ev_lock = threading.Lock()
    ev_signal = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(PAGE)))
            self.end_headers()
            self.wfile.write(PAGE)

        def do_POST(self):
            ln = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(ln)
            with ev_lock:
                events.append(json.loads(body))
            ev_signal.set()
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    socketserver.TCPServer.allow_reuse_address = True
    srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1], events, ev_lock, ev_signal


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("stream socket closed")
        buf.extend(chunk)
    return bytes(buf)


def read_frame(sock: socket.socket):
    hdr = recv_exact(sock, HEADER_SIZE)
    magic, version, width, height, fmt, payload_size, pts_us = struct.unpack(
        HEADER_FMT, hdr
    )
    if magic != MAGIC or version != 1:
        raise AssertionError(f"invalid header: magic={magic:#x} version={version}")
    payload = recv_exact(sock, payload_size)
    return width, height, fmt, payload, pts_us


def send_input(sock: socket.socket, typ: int, mods: int = 0, x: int = 0, y: int = 0,
               button: int = 0, data: int = 0, text: str = "") -> None:
    tb = text.encode("utf-8")
    pkt = struct.pack(CAMI_FMT, CAMI_MAGIC, 1, typ, mods, x, y, button, data, len(tb))
    sock.sendall(pkt + tb)


def nv12_to_png(width: int, height: int, payload: bytes, out_path: Path) -> None:
    """Save an NV12 frame as PNG (only used when --dump-png is requested)."""
    import zlib

    y_size = width * height
    y_plane = payload[:y_size]
    uv_plane = payload[y_size:]
    rows = []
    for j in range(height):
        row = bytearray([0])
        y_row = j * width
        uv_row = (j // 2) * width
        for i in range(width):
            y = y_plane[y_row + i]
            uv_idx = uv_row + (i & ~1)
            u = uv_plane[uv_idx] - 128
            v = uv_plane[uv_idx + 1] - 128
            c = y - 16
            r = max(0, min(255, (298 * c + 409 * v + 128) >> 8))
            g = max(0, min(255, (298 * c - 100 * u - 208 * v + 128) >> 8))
            b = max(0, min(255, (298 * c + 516 * u + 128) >> 8))
            row.extend((r, g, b))
        rows.append(bytes(row))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"".join(rows), level=1))
        + chunk(b"IEND", b"")
    )
    out_path.write_bytes(png)


def main() -> int:
    if "--binary" not in sys.argv:
        if "CAMOUFOX_EXECUTABLE_PATH" not in os.environ and "CAMOUFOX_BINARY" not in os.environ:
            root = Path(__file__).resolve().parents[3]
            mac_bins = sorted(
                root.glob("camoufox-*/obj-*/dist/Camoufox.app/Contents/MacOS/camoufox")
            )
            if mac_bins:
                os.environ["CAMOUFOX_EXECUTABLE_PATH"] = str(mac_bins[-1])

    dump_png = None
    if "--dump-png" in sys.argv:
        dump_png = Path(sys.argv[sys.argv.index("--dump-png") + 1])

    binary = resolve_binary()
    srv, port, events, ev_lock, ev_signal = serve_page()
    profile = tempfile.mkdtemp(prefix="headless-stream-prof-")
    sock_dir = tempfile.mkdtemp(prefix="hs-", dir="/tmp")
    sock_path = os.path.join(sock_dir, "s.sock")

    Path(profile, "user.js").write_text(
        'user_pref("browser.shell.checkDefaultBrowser", false);\n'
        'user_pref("datareporting.policy.dataSubmissionPolicyBypassNotification", true);\n'
    )

    env = dict(os.environ)
    env["CAMOU_HEADLESS_STREAM_SOCKET"] = sock_path
    env["CAMOU_HEADLESS_STREAM_FPS"] = "60"
    env["CAMOU_HEADLESS_STREAM_FORMAT"] = "nv12"

    proc = subprocess.Popen(
        [
            str(binary),
            "-headless",
            "-no-remote",
            "-profile",
            profile,
            "--window-size=1280,720",
            f"http://127.0.0.1:{port}/",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    try:
        deadline = time.monotonic() + LOAD_TIMEOUT_S
        while not os.path.exists(sock_path):
            if proc.poll() is not None:
                print(f"FAIL: browser exited early with {proc.returncode}")
                return 1
            if time.monotonic() > deadline:
                print("FAIL: stream socket was not created before timeout")
                return 1
            time.sleep(0.05)

        frames = []
        hashes = set()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(10.0)
            client.connect(sock_path)

            first_w, first_h, first_fmt, first_payload, first_pts = read_frame(client)
            assert first_fmt == FMT_NV12, f"unexpected pixel format: {first_fmt}"
            assert first_w >= 200 and first_h >= 200, f"tiny size {first_w}x{first_h}"
            assert len(first_payload) == first_w * first_h * 3 // 2

            # Sample live video stream.
            t_start = time.monotonic()
            prev_pts = first_pts
            while time.monotonic() - t_start < STREAM_WINDOW_S:
                w, h, fmt, payload, pts_us = read_frame(client)
                assert fmt == FMT_NV12
                assert len(payload) == w * h * 3 // 2
                assert pts_us > prev_pts, f"non-monotonic pts: {pts_us} <= {prev_pts}"
                prev_pts = pts_us
                frames.append((w, h, pts_us, payload))
                hashes.add(hashlib.blake2b(payload, digest_size=8).digest())

            # Exercise 2-way interactive input: double-click center, wheel down, type "ok".
            cx, cy = first_w // 2, first_h // 2
            send_input(client, 1, x=cx, y=cy)
            for _ in range(2):
                send_input(client, 2, x=cx, y=cy, button=0)
                send_input(client, 3, x=cx, y=cy, button=0)
            send_input(client, 4, x=cx, y=cy, button=0, data=120)
            for ch, code in (("o", "KeyO"), ("k", "KeyK")):
                send_input(client, 5, text=f"{ch}\t{code}")
                send_input(client, 6, text=f"{ch}\t{code}")

            # Wait briefly while pumping frames so input round-trips through Firefox.
            input_deadline = time.monotonic() + 5.0
            while time.monotonic() < input_deadline:
                read_frame(client)
                with ev_lock:
                    kinds = {e["kind"] for e in events}
                if {"dblclick", "wheel", "input"}.issubset(kinds):
                    break

        with ev_lock:
            observed = list(events)

        dbl = [e for e in observed if e["kind"] == "dblclick"]
        whl = [e for e in observed if e["kind"] == "wheel"]
        inp = [e for e in observed if e["kind"] == "input"]
        if not dbl or not dbl[-1]["trusted"] or dbl[-1]["detail"] != 2:
            print(f"FAIL: bad dblclick events: {dbl}")
            return 1
        if not whl or not whl[-1]["trusted"] or whl[-1]["wheelDelta"] != -120:
            print(f"FAIL: bad wheel events (expected wheelDelta=-120): {whl}")
            return 1
        if not inp or not inp[-1]["trusted"] or inp[-1]["value"] != "ok":
            print(f"FAIL: bad keyboard input events (expected 'ok'): {inp}")
            return 1

        elapsed = max(1e-3, (frames[-1][2] - first_pts) / 1_000_000.0) if frames else 0.0
        fps = len(frames) / elapsed if elapsed > 0 else 0.0

        if len(hashes) < 5:
            print(f"FAIL: expected animated distinct frames, got {len(hashes)} unique hashes")
            return 1

        if dump_png is not None and frames:
            dump_w, dump_h, _, dump_payload = frames[-1]
            nv12_to_png(dump_w, dump_h, dump_payload, dump_png)

        # Verify clean reconnect after disconnecting viewer.
        time.sleep(0.2)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client2:
            client2.settimeout(10.0)
            client2.connect(sock_path)
            w2, h2, fmt2, payload2, pts2 = read_frame(client2)
            assert fmt2 == FMT_NV12 and w2 >= 200 and h2 >= 200
            assert len(payload2) == w2 * h2 * 3 // 2 and pts2 > prev_pts

        print(
            f"PASS: headless NV12 stream {frames[-1][0]}x{frames[-1][1]}, "
            f"{len(frames)} frames in {elapsed:.2f}s ({fps:.1f} FPS), "
            f"{len(hashes)} distinct frames, 2-way input (dblclick detail=2, "
            f"wheelDelta=-120, input='ok', all isTrusted=True), reconnect OK"
        )
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        srv.shutdown()
        shutil.rmtree(profile, ignore_errors=True)
        shutil.rmtree(sock_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
