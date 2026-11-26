# Headless Live Stream Patch

`zz-headless-live-stream.patch` exposes composited frames from `--headless`
Firefox/Camoufox directly over a Unix domain socket (`AF_UNIX`) and accepts
real widget-level mouse, wheel, and keyboard input back over the same stream,
without needing a virtual display or screen recording permissions.

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

## Video Output Wire Protocol (`CAMF`, Browser -> Client)

Each frame begins with a 32-byte little-endian header followed immediately by
`payload_size` raw pixel bytes:

| Offset | Type | Field | Description |
|---|---|---|---|
| 0 | `u32` | `magic` | `0x464D4143` (`"CAMF"` little-endian) |
| 4 | `u32` | `version` | `1` |
| 8 | `u32` | `width` | Frame width in pixels (even-aligned for `NV12`) |
| 12 | `u32` | `height` | Frame height in pixels (even-aligned for `NV12`) |
| 16 | `u32` | `pixel_format` | `1` = `BGRA` (`w * h * 4`), `2` = `NV12` (`w * h * 3 / 2`) |
| 20 | `u32` | `payload_size` | Exact byte count following this header |
| 24 | `u64` | `pts_us` | Monotonic steady clock timestamp in microseconds |

## Reverse Input Wire Protocol (`CAMI`, Client -> Browser)

The client can write 36-byte little-endian `CAMI` packets (optionally followed
by `text_len` UTF-8 bytes) back onto the connected `SOCK_STREAM`. Events are
dispatched on the parent process main thread against the active `HeadlessWidget`
using `WidgetMouseEvent::eReal`, `WidgetWheelEvent` (with `mWheelTicks*`),
`TextEventDispatcher::BeginNativeInputTransaction`, and
`MaskConfig::InputTimeStamp()`. Coordinates `(x, y)` are in frame device pixels
from the top-left corner of the window, covering both browser chrome (URL bar,
tabs) and web content.

| Offset | Type | Field | Description |
|---|---|---|---|
| 0 | `u32` | `magic` | `0x494D4143` (`"CAMI"` little-endian) |
| 4 | `u32` | `version` | `1` |
| 8 | `u32` | `type` | `1`=MouseMove, `2`=MouseDown, `3`=MouseUp, `4`=Wheel, `5`=KeyDown, `6`=KeyUp, `7`=UTF-8 Text |
| 12 | `u32` | `modifiers` | Bitmask: `1`=Shift, `2`=Control, `4`=Alt, `8`=Meta |
| 16 | `i32` | `x` | Pixel X (Mouse/Wheel), or `isRepeat` (`0`/`1`) for Key |
| 20 | `i32` | `y` | Pixel Y (Mouse/Wheel), or `location` (`0`=auto) for Key |
| 24 | `i32` | `button` | Mouse button `0..4`, or Wheel `deltaX` (px) |
| 28 | `i32` | `data` | Wheel `deltaY` (px), or DOM `keyCode` (`0`=auto) |
| 32 | `u32` | `text_len` | UTF-8 payload byte count (`"key\tcode"` for KeyDown/Up, UTF-8 string for Text) |

## Interactive Web Example

[examples/web_live_control.py](../../../examples/web_live_control.py) is a dependency-free
WebSocket + WebGL2 reference server that bridges the `AF_UNIX` socket to a live
browser viewer (`ws://127.0.0.1:18765/ws`) with mouse, wheel, keyboard, and
paste support:

```bash
python3 examples/web_live_control.py
```
