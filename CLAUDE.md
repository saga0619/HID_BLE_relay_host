# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PyQt5 desktop app (macOS-tested) that pairs with the **HID Relay Dongle** (nRF52840) firmware: it shows the HDMI feed from a USB capture card and relays keyboard/mouse input to the dongle over a custom BLE GATT service so the dongle can act as a USB HID for a headless target. The companion firmware lives at https://github.com/saga0619/HID_BLE_relay_dongle.

## Run / Dev

```bash
pip install -r requirements.txt   # PyQt5, bleak
python main.py                    # main entry point
python list_ble.py                # debug: enumerate nearby BLE devices
```

There are no tests, lint config, or build step.

## Architecture

`main.py` is the only entry point. Two cooperating components, joined in `main()`:

1. **`BleManager` (`QObject`)** — owns its own asyncio event loop on a background daemon thread (`_run_loop`). The Qt main thread calls `send_data_sync()`, which uses `asyncio.run_coroutine_threadsafe` to marshal writes onto that loop. This split is deliberate: bleak is async-only, but Qt event handlers must not block. Connection lifecycle: scan by name + service UUID → connect → `start_notify` on TX → cache RX characteristic → idle loop until disconnect cancels all tasks → sleep `BLE_RECONNECT_DELAY_S` → rescan. Writes are chunked to `max_write_without_response_size` and sent with `response=False`. Connection state is published via the `connected_changed` Qt signal so the UI can reflect it without polling.

2. **`VideoApp` (QWidget)** — uses `QCamera` + `QCameraViewfinder`. It picks the camera by matching `TARGET_CAMERA_NAME` against `QCameraInfo.description()`, falling back to camera 0. `setMouseTracking(True)` is set on both the widget and the viewfinder so move events arrive even with no button held. Qt key/mouse/wheel event handlers translate input into the wire protocol below and hand it to `BleManager`. The widget tracks held buttons in `_mouse_buttons` so a drag (move-with-button-held) can be reported with the right HID button bit.

### Wire protocol (host → dongle, ASCII over RX characteristic)

The dongle parser (`src/main.c::received()` in the firmware repo) reads `<device><action>:<payload>`, with `\n` separating multiple commands in one write. Tokens shorter than 4 bytes or missing the `:` at index 2 are rejected.

| Event | Host sends | Dongle action |
|---|---|---|
| Key press | `KP:<hex>` | `get_hid_key()` → `add_key()`/`update_modifiers()` → 8-byte boot keyboard report |
| Key release | `KR:<hex>` | `remove_key()` / clear modifier bit → report |
| Left press | `ML:<x>,<y>` | absolute mouse report, button=1 |
| Right press | `MR:<x>,<y>` | absolute mouse report, button=2 |
| Left release | `MS:<x>,<y>` | absolute mouse report, button=0 |
| Right release | `ME:<x>,<y>` | absolute mouse report, button=0 |
| Move (no button) | `MM:<x>,<y>` | absolute mouse report, button=0 |
| Drag (button held) | `ML:`/`MR:<x>,<y>` | host re-uses press verb so HID button bit stays asserted during drag |
| Wheel | `WW:<int>` | `int8_t` wheel field; reuses last x/y |

Auto-repeated key events are filtered on the host (`event.isAutoRepeat()`); the target OS handles its own key repeat. `x`,`y` are unsigned `0–ABS_COORD_MAX (32767)`, matches the dongle's HID descriptor `Logical Max 0x7FFF`. Coordinate normalization happens in `_normalized_pos()` / `get_video_display_rect()`, which letterboxes the video inside the viewfinder so the cursor maps to actual video pixels rather than widget pixels — and clamps so out-of-frame clicks don't emit out-of-range coords.

The TX characteristic (notify, dongle → host) is wired on both sides but the firmware never calls `bt_hidrelay_send`, so notify traffic is currently dead — host's `handle_rx` only fires if firmware starts using it.

### macOS Cmd/Ctrl swap

PyQt5 on macOS swaps Cmd↔Ctrl by default, which breaks Ctrl+C/V/etc. on the headless target (the host's physical Ctrl arrives as `Qt::Key_Meta`, which the dongle maps to ⌘). `main()` calls `QCoreApplication.setAttribute(Qt.AA_MacDontSwapCtrlAndMeta)` **before** `QApplication(...)` to disable the swap — this attribute must be set before the QApplication is constructed or it has no effect.

### Configuration

The constants at the top of `main.py` are the things most likely to need editing per setup:

- `TARGET_CAMERA_NAME` (default `"UGREEN-25854"`) — substring-matched against camera descriptions
- `TARGET_CAPTURE_WIDTH/HEIGHT/FPS` — requested viewfinder settings (logged at startup; check console output to see what was actually negotiated)
- `TARGET_BLE_NAME` (default `"HID BLE Relay"`) — substring-matched against advertised name; also gated by `HID_SERVICE_UUID`
- `BLE_RECONNECT_DELAY_S` — backoff between scan retries when the dongle isn't found / disconnects
- `ABS_COORD_MAX` — must match the dongle's mouse HID descriptor

The three custom UUIDs (`HID_SERVICE_UUID`, `HID_RX_CHAR_UUID`, `HID_TX_CHAR_UUID`) are a contract with the dongle firmware — do not change them in isolation.

`qtkeystring.py` is a Qt-key → human-readable name table kept around as a debugging reference for what key codes the wire protocol carries; `main.py` doesn't import it.

## Companion firmware (`~/HID_BLE_relay_dongle`)

The other half of this system — the Zephyr / nRF Connect SDK 2.8.0 firmware that runs on the nRF52840 Dongle — lives at `~/HID_BLE_relay_dongle`. When the wire protocol or BLE service shape changes, both repos must change in lockstep.

**Build (firmware side):**
```bash
cd ~/HID_BLE_relay_dongle
source env.sh                                # exposes west + Zephyr toolchain at /opt/nordic/ncs/v2.8.0
west build -b nrf52840dongle_nrf52840
# flash with nRF Connect Programmer
```

**Firmware structure:**
- `src/main.c` — Zephyr entry. Sets up BT advertising, USB HID, GPIO/PWM LEDs, CDC-ACM. Owns the `received()` callback that parses the ASCII wire protocol and the main thread that pumps an `app_evt_t` FIFO + drives LED feedback.
- `src/ble_hidrelay.{c,h}` — static-GATT custom service exposing the three UUIDs. RX char is write/write-without-response, TX char is notify+read with a CCC descriptor. **Not** Zephyr's built-in NUS (`CONFIG_BT_ZEPHYR_NUS=n` in `prj.conf`); it's a from-scratch reimplementation with NUS-shaped UUIDs.
- `src/hid_km.{c,h}` — registers two USB HID interfaces (`CONFIG_USB_HID_DEVICE_COUNT=2`):
  - `HID_0` = standard 8-byte boot keyboard (`HID_KEYBOARD_REPORT_DESC()`)
  - `HID_1` = custom absolute mouse: 6-byte report `[buttons | x_lo | x_hi | y_lo | y_hi | wheel]`, X/Y are 16-bit `0–0x7FFF`
  - Holds the giant `qt_hid_map[]` translating Qt key codes → USB HID usage codes + modifier mask. Modifier vs. regular key is decided by whether `modifier_mask != 0`.
- `src/usb_hid_keys.h` — standard USB HID keyboard usage table (`KEY_A=0x04`, `KEY_MOD_LSHIFT=0x02`, etc.).

**Cross-repo gotchas:**
- The Qt → HID translation table only lives on the dongle. The host blindly forwards `event.key()` as hex; if a key isn't in `qt_hid_map[]`, the dongle blinks a red error LED and types `"ERROR"` on the USB keyboard as a tell.
- Keyboard state (which keys are currently pressed) is kept on the **dongle** in `pressed_keys[6]` + `current_modifiers`, not the host. A host crash mid-press can leave keys "stuck"; pressing the dongle's user button (SW0 / `GPIO_BUTTON_0`) sends a clear report.
- Coordinate range `32767` is duplicated in three places: host `ABS_COORD_MAX`, dongle parser, and dongle HID descriptor `Logical Max 0xFF, 0x7F`. All three must agree.
- Service UUID `597f1290-...` and the two char UUIDs are duplicated in `main.py` and `src/ble_hidrelay.h` — keep in sync.
- LEDs as debug aid: blue PWM fades while the central is unsubscribed and goes solid when notify is enabled; the small GPIO LED blinks per relayed input; red blinks on a key the dongle couldn't translate or a malformed protocol token.
