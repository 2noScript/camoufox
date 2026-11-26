# Headless Live Stream Patch

`zz-headless-live-stream.patch` exposes composited frames from `--headless`
Firefox/Camoufox directly over a Unix domain socket (`AF_UNIX`), bypassing
Juggler's 25 FPS timer, CPU JPEG compression, Base64 encoding, and main-thread
roundtrips while requiring no virtual display or screen recording permissions.

This patch lives in its own directory under `browser/patches/headless-stream/`
so no core Camoufox or Playwright patch files are modified.

## Activation & Environment Variables

Read once in the parent/GPU process (`HeadlessStreamServer::Get()`):

- `CAMOU_HEADLESS_STREAM_SOCKET`: Path to the Unix domain socket that the
  headless browser binds and listens on. When unset or empty, the server stays
  disabled (zero threads, zero sockets, zero memory overhead).
- `CAMOU_HEADLESS_STREAM_FPS`: Target maximum frame rate (`1..120`, default
  `60`). Frame emission is driven directly by WebRender SWGL's
  `EndRemoteDrawingInRegion` commit hook, throttled to this cap with a trailing
  snapshot timer so the final painted frame is never dropped.
- `CAMOU_HEADLESS_STREAM_FORMAT`: `"nv12"` (default, converted via
  `libyuv::ARGBToNV12`) or `"bgra"`.

While no viewer client is connected to the socket, zero frame readbacks or
conversions occur. When a client disconnects, staging buffers are freed.

## Wire Protocol

Each frame sent over the `SOCK_STREAM` socket begins with a 32-byte
little-endian header followed immediately by `payload_size` raw pixel bytes:

| Offset | Type | Field | Description |
|---|---|---|---|
| 0 | `u32` | `magic` | `0x464D4143` (`"CAMF"` little-endian) |
| 4 | `u32` | `version` | `1` |
| 8 | `u32` | `width` | Frame width in pixels (even-aligned for `NV12`) |
| 12 | `u32` | `height` | Frame height in pixels (even-aligned for `NV12`) |
| 16 | `u32` | `pixel_format` | `1` = `BGRA` (`w * h * 4`), `2` = `NV12` (`w * h * 3 / 2`) |
| 20 | `u32` | `payload_size` | Exact byte count following this header |
| 24 | `u64` | `pts_us` | Monotonic steady clock timestamp in microseconds |
