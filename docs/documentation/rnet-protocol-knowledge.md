# Russound RNET Protocol Knowledge Base

Accumulated, verified facts about the Russound RNET RS-232 protocol as used by
this repo (CAA66/CAS44 controllers + KPL keypads), gathered from the official
RNET manual and from live traffic captured with `tool/russound_capture.py`.
Keep this file up to date whenever new protocol behavior is confirmed —
it is the canonical reference for future Copilot sessions.

## Source and confidence

- The attached firmware reverse-engineering notes for CAA66 firmware 2.01.01
  and KPL firmware 1.00.02 are the most specific source for path grammar,
  device classes, event layouts, and controller side effects.
- The official protocol manual and live captures remain useful for framing,
  checksum validation, and observed application behavior. Where older live
  decoder assumptions conflict with firmware-derived details below, prefer the
  firmware-derived interpretation and add a capture before declaring a new
  behavior universal across Russound models.

## Physical / link layer

- 19200 baud, 8N1, no flow control, shared RS-232/RS-485 bus.
- Keypads and controllers share the bus. A third-party/fire-and-forget source
  uses header device/keypad id `0x70`, has no handshake even when Event
  priority is zero, and should use roughly 100 ms spacing between frames.
- CAA66 hardware: RS-232 is MCU P21/P22; keypad RS-485 is P71 RX, P72 TX,
  P75 transmit-enable. STATUS 1-6 and IR are hardware paths, not RNET frames.
- `ser2net` bridges the serial device to TCP; on macOS, USB passthrough into
  Docker isn't possible, so a host-side `socat` process bridges the serial
  device to a TCP port that the containerized `ser2net`/backend connects to
  (see `docker-compose.mac.yml`).

## Frame format

```
F0 [target: cc zz kk] [source: cc zz kk] [type] [body...] [checksum] F7
```

- `F0` = start byte, `F7` = end byte. These values never appear elsewhere in a
  frame unescaped.
- Target/source addresses are each 3 bytes: controller (`cc`), zone (`zz`),
  keypad (`kk`).
- **Checksum**: `(byte_count_before_checksum + sum(all_preceding_bytes)) & 0x7F`,
  i.e. `(len(frame) - 2 + sum(frame[:-2])) & 0x7F`, compared against
  `frame[-2]`.
- **Invert/escape character `0xF1`**: precedes any payload byte after the
  TYPE byte whose real value has bit 7 set. The next byte is XOR'd with
  `0xFF` to recover the real value. Header bytes are never escaped. The
  checksum includes the transmitted `F1` escape pair exactly as sent; only
  unescape the payload for semantic decoding (see
  `_unescape_rnet_frame` / `RussoundTrafficDecoder._unescape_body`).

## Addressing and device classes

Controller byte:
- `0x7F` = all controllers.
- Otherwise `value + 1` = controller number (0-based on the wire).

Zone byte:
- `0x7F` = reserved zone.
- `0x7E` = controller link.
- `0x7D` = peripheral device.
- `0x7C` = trace.
- Otherwise `value + 1` = zone number (0-based on the wire).

Keypad/device-class byte:
- `0x7F` = controller itself / all devices, depending on direction and path.
- `0x7E` = system class in the CAA66 firmware mode used by these captures.
- `0x7D` = source/tuner class; it is also used for source-object publishes.
- `0x70` = third-party/fire-and-forget source; no event handshake.
- `0x60` = keypad class requiring `02 06` handshake for high-priority events.
- `0x00`-`0x05` = physical keypad indices in a zone.
- `0x7C` = keypad ID request in the protocol's special-address context.
- `0x79` = source broadcast display in the display-feedback context.

Older documentation called `0x7E` reserved and `0x7D` all keypads/peripheral
sources. Treat those as generic/manual address descriptions; the firmware RE
gives the more useful CAA66 device-class meanings above. Zone-field special
values remain distinct: `0x7F` reserved, `0x7E` controller link, `0x7D`
peripheral device, and `0x7C` trace.

## Message types (byte 7, the type byte after source address)

- `0x00` = Set Data (controller pushing/updating a value).
- `0x01` = Request Data (asking for a value/path).
- `0x02` = Handshake (ACK only; no paths; body kind `0x06` means event ACK).
- `0x05` = Event (keypad key press / event notification).
- `0x06` = undocumented. Observed volume feedback after every volume change
  (keypad or RS-232): `F0 cc zz 7D 00 00 7F 06 <wire_vol> xx F1 6F 01 10 <CKS> F7`
  (`xx` is `0A` for keypad changes and `1E` for RS-232 changes). Source
  feedback after a source change or power-on is
  `F0 cc zz 7D 00 00 7F 06 00 <source_index> 00 00 05 <CKS> F7`; source index 0
  is reported as `06 41 00 00 00 05`. Other `0x06` shapes are still unknown.
- Zone power changes (keypad or RS-232) are forwarded to the source class as
  Set State `0xDC`: `F0 cc zz 7D 00 00 7F 05 02 01 00 02 01 00 F1 23 00 <pwr> 00 <zz> 00 01 <CKS> F7`.
  Observed to match the polled zone power on every keypad on/off in the
  2026-10-10 capture; the web server still re-reads the zone to confirm.

Keypad Events from zone keypads are visible on RS-232, e.g.
`F0 00 00 7F 00 00 00 05 03 02 00 00 02 04 03 <EventID> ...` (source
controller/zone/keypad in header bytes 4-6). The web server uses them as a
hint to re-read that zone immediately.

## Event messages (`type == 0x05`)

After the header, non-handshake messages use path grammar:
`target_length, target_path, source_length, source_path, body`.
The target path identifies the object; the source path supplies context. Get
replies copy the requested node into the source path and use an empty target
path. Unsolicited source-object publishes fill both paths.

For keypad Events, path `2.0` is encoded as target path `[02, 00]`, with no
source path. The common wire prefix after TYPE is therefore `02 02 00 00`.
The event body is `EventID, timestamp, data..., zone, priority`; exact data
length depends on the event family.

- `event_id` may be escaped with `0xF1` if it would be > `0x7F`.
- Known keypad event IDs (`KEYPAD_EVENTS` in `tool/russound_capture.py`):
  - `0x64` setup, `0x67` previous, `0x68` next, `0x69` plus, `0x6A` minus,
    `0x6B` source_toggle, `0x6C` power, `0x6D` stop, `0x6E` pause,
    `0x6F` favorite_1, `0x70` favorite_2, `0x73` play,
    `0x7F` volume_up, `0x80` volume_down (both volume events use the
    invert-escape since `0x80 > 0x7F`).
  - Mute is triggered by source-control event key `0x0D` (often appears as
    part of the `0xBF` family in raw captures); triggers `* Muted *` display
    text, unmute restores the previous source/status text.
- `event_data` = little-endian 16-bit value at offset `event_index+4..+5`;
  `priority` byte follows at `event_index+6`.

Important event families:
- `0xDC` (wire `F1 23`) = discrete Set State; for zone power, data `00` is
  OFF and `01` is ON. A typical command targets zone `zz`, uses source device
  `0x70`, and is not a state echo.
- `0xBF`/`0xC0` = source-control family; mute uses data/key `0x0D`.
- System path `2.1.0` uses target path `[01, 00]` and source path `[01, 00]`.
  `0xDC` there is Set State forwarded to source class `0x7D`; `0x2F` is a
  tuner Tune Up event.
- `0xC8` (wire `F1 37`) on system path is a periodic zone-activity
  advertisement, not an ACK for a power command. Its six low mask bits map to
  zones 1-6 that are ON with a valid source; the mask can be one timer tick
  stale.

## Handshake

- Only a source/device class other than `0x70` must ACK a high-priority Event
  (priority `0`) with a pathless frame addressed back to the original source:
  `F0 <original-source-address> 02 06 <CKS> F7`.
- Failure to ACK can cause approximately a 2.5 second retry stall. Prefer
  `0x70` plus spacing for diagnostic command injection.

## Paths and Request Data (`type == 0x01`)

- The CAA66 all-zone-info request uses target path `4.2.0.zz.07`, encoded on
  the wire as target length `04` followed by `02 00 zz 07`; it does **not**
  have a `05 02` body prefix. The quick-identification pattern is
  `01 04 02 00 zz PP` after the header.
- A normal zone parameter path is `4.2.0.zz.PP` and a source object path is
  `4.4.L`. Public tone/feature paths are `5.2.0.zz.00.YY`, where `YY=00`
  bass, `01` treble, `02` loudness, `03` balance, `05` party, and `06` DnD.
- All-zone-info response is Set Data with an empty target path and the node in
  the source path. Its 12-byte payload decodes as:
  - `power` (bool), `source_index`, `volume` (wire units doubled for
    app-facing value: `wire * 2`), `bass`/`treble`/`balance` (wire value
    `- 10`, i.e. offset-10 signed range), `loudness` (bool),
    `system_power` (bool), `shared_source` (bool), party mode (CAA66 `0`),
    and Do Not Disturb (CAA66 `0`).
  - Volume wire range is `0..50`; app-facing range is `0..100`.
  - Mute state is **not** publicly readable via this path.

## Zone user parameter response

- Body path prefix `05 02 00`, parameter id at offset 5, value at offset 12.
- Known parameter ids: `0`=bass, `1`=treble, `2`=loudness, `3`=balance,
  `4`=turn_on_volume.
  - bass/treble/balance: value `- 10`.
  - turn_on_volume: value `* 2` (same doubling as normal volume).
  - loudness: value coerced to bool.

## Direct display feedback (Set Data, path `2.1.1`)

- This is the live keypad text mechanism, sent on both the RS-232 and
  RS-485 buses.
- Wire target path is length `02`, path `01 01`; quick identification is
  `00 02 01 01` after the header. The data begins with alignment (`00`
  center, `01` left), then flash time as little-endian 10 ms units, followed
  by NUL-terminated ASCII text. Display bytes may be escaped individually.
- There is also a **broadcast** display variant using target `F0 7F ...`
  that updates the global display fields and all known keypad overrides,
  vs. a **targeted** variant `F0 @cc @zz @tk ...` that updates only the
  addressed controller/zone/keypad override
  (see `tool/dummy_backend/dummy_backend.py` display-frame handling).

## Set Data — controller-originated updates and source objects

- The connector currently recognizes controller-originated All Zone Info
  pushes and applies power/volume updates immediately, in addition to regular
  polling. Firmware RE also shows unsolicited source-object publishes on
  `4.4.L` to device class `0x7D` after zone power changes:
  - `L=00` publishes `0x48` when a zone is OFF. `0x48` is an idle/released
    protocol sentinel, not a source index or source type.
  - `L=02`, `04`, `05`, and `06` publish `0` when the zone is OFF.
  - When ON, `4.4.0` contains a variable value.

## Zone activity and display side effects

- A zone power command can produce source-object Set Data publishes, a `0xDC`
  Event forwarded to source class `0x7D`, and an unrelated periodic `0xC8`
  zone-activity Event. Do not interpret those side effects as a direct power
  response; confirm power by polling `4.2.0.zz.06` or All Zone Info.
- Mute has no public Get path. Mute writes `* Muted *` to both RS-232 and
  keypad display buses; unmute restores source/status text through the normal
  display mechanism. Source names live in controller NVRAM and are not
  publicly readable or renameable through documented RNET.

## Known protocol gaps / unknowns (not yet reverse-engineered)

- Exact source-name ASCII offset/record format (source names live in
  controller NVRAM and aren't accessible over documented RNET).
- Complete list of parameter "leaf" ids beyond the documented
  bass/treble/loudness/balance/turn-on-volume and the CAA66 party/DnD leaves.
- Private SCS-C5 command paths.
- Full meaning of all undocumented event IDs outside `KEYPAD_EVENTS`.
- Exact volume-to-bargraph scaling on keypad displays.
- No public path to read mute state or commit arbitrary setup/NVRAM changes.

## Decoder maintenance warning

- The current capture decoder was written before the firmware-derived path
  grammar was available. In particular, review `_decode_display_feedback`,
  `_decode_request_data`, `_decode_response`, and `_decode_event` in
  `tool/russound_capture.py` before extending them: some routines still match
  older fixed-offset/prefix assumptions. Add captured-frame regression tests
  when correcting those parsers; do not silently change production behavior
  based only on one model's firmware notes.

## Where this is implemented in the repo

- `tool/russound_capture.py` — `RussoundTrafficDecoder`: canonical decode
  logic for all frame types above, plus frame builders
  (`build_rnet_frame`, `build_volume_event_frame`, `build_set_volume_frame`,
  `build_get_zone_info_frame`, `build_mute_frame`, `build_power_frame`) and
  an interactive TUI (`run_tui`) for sending test frames over serial or TCP
  (ser2net) and watching decoded traffic live.
- `web/russound_connector.py` — production `Russound` client: connection
  handling, checksum/escape logic, `_parse_zone_updates`, background
  unsolicited-update listener.
- `tool/dummy_backend/dummy_backend.py` — hardware-free TCP stand-in server
  implementing the same wire behavior for tests (`DummyRussoundState`),
  including both display-frame variants.
- `tests/test_russound_capture.py` — regression tests for the decoder,
  checksum/escape handling, and frame builders.
