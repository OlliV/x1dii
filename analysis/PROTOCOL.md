# X1D II USB protocol (CFiProtocol) — framing notes

Recovered from `Phocus.app/Contents/Frameworks/HBTethered.framework` (Phocus 4.1.1,
x86_64 + arm64, symbolicated). No debugging of the camera was involved.

The same `CFiProtocol` serves USB, Wi-Fi and Bluetooth, so this framing applies to all
three transports.

## Packet layout (from `CFiProtocol::SendUserCommand`, x86_64 `0x44149`)

```
off  size  field
 0    1    0x55 magic
 1    2    total length in bits[9:0], packet "type" in bits[15:10]
 3    1    CRC8 over bytes [0..2], seed 0x77, reflected poly 0x8C
 4    2    bitfield: bits[4:0]=0x02, [7:5] and [12:8] and [15:13] from request header
 6    2    command id high (u16)          <- see parse note below
 8    1    flags: bits[7:5] and bit 7 from request header
 9    1    command byte
 10   1    sub-command byte
 11   ...  payload
 len-2 2    CRC16 over [0 .. len-3], seed 0x3692, stored low byte then high
```

CRC8 table verified: extracted at `0x892e0` and byte-identical to the standard
reflected table for poly `0x8C`. CRC16 uses the table at `0x893e0` with seed `0x3692`
(reflected poly `0x8408`).

Both tables and seeds match the DJI DUML parameters recorded in the X2D II research —
so this is a compact DUML variant, not a proprietary Hasselblad format.

## Command id as parsed

`CFiProtocol::ParseCommand` (`0x418f0`) builds the lookup key as:

```c
cmd = ((u16)buf[6] << 16) | (buf[9] << 8) | buf[10];
```

then `CFiProtocol::FindInList(cmd)` dispatches. So bytes 6-7 are the high half of the
command id, and bytes 9/10 are the low halves — consistent with the builder above.

## Transport

| transport | evidence |
|---|---|
| USB | `LibUsb.framework` bundled; `CCameraDeviceUSBx::ControlParameter/WriteParameter/ReadData` |
| Wi-Fi | `CFiProtocol::SendRequest(wifi_cmd_req_t*)`, `WiFiInfoPoll` |
| Bluetooth | `CFiProtocol::BluetoothParse/BluetoothRequest`, `BluetoothSession` |

On USB the frames go to the vendor-specific interface (intf 3): OUT `0x03`/`0x04`,
IN `0x84`/`0x85`.

## What this enables

A host-side encoder is now a direct port of the builder above: fill the header, append
payload, compute CRC8 over 3 bytes, CRC16 over the rest, write to `0x03`, read `0x84`.

Next step: send one benign query and confirm a reply. The safest first frames are the
ones Phocus sends on connect — `SendHeartBeat` and the parameter poll. Their exact
command bytes still need to be read out of `CFiProtocol`'s tables (`FindInList` maps
command ids to handlers).

## Parameter addressing (from `CCameraDeviceUSB::WriteParameter`, x86_64 `0x6707c`)

Phocus does not send parameter *names* on the wire. The function resolves a name to a
16-bit code first:

```
code = CCameraDeviceParameter::DeviceEnum(name)     // HBUtil 0x5c808
payload.append(code & 0xFF)
payload.append((code >> 8) & 0xFF)
payload.append(value bytes)                          // built from sCameraParameter
send(request, timeout = 2000 ms)
```

`DeviceEnum` (defined in `HBUtil.framework`, `0x5c808`) strips a `Device:` prefix and
looks the remainder up in a table — that table is the parameter dictionary and is the
last missing piece for constructing real requests.

So a request is: `[u16 code][value]`, where the code comes from that table. This also
explains why the earlier device-type frame went unanswered: it was addressed as a
command, whereas the camera's live API is parameter/action based.

## Parameter dictionary (extracted)

`DeviceEnum` (HBUtil `0x5c808`) walks a static table at `__DATA_CONST 0xA14F0`:
**170 entries × 16 bytes**, laid out as

```
+0x00  u32  parameter code
+0x04  u32  padding
+0x08  u64  chained-fixup pointer to a libc++ std::string (the name)
```

The loop walks the table backwards (`r12 = 0xAA0; r12 -= 0x10`), comparing the
`Device:`-stripped name and returning `entry[0]` on a match.

Full dump: [`parameter-table.txt`](parameter-table.txt).

Video / capture related entries:

| code | name |
|---|---|
| `0x0023` | TetheredPowerMode |
| `0x0033` | TetheredCaptureEnabled |
| `0x0035` | LiveVideo |
| `0x0044` | VideoType |
| `0x0051` | LiveVideoMode |
| `0x007a` | TetheredMode |
| `0x0080` | ImageFormat |
| `0x0085` | LiveVideoZoom |
| `0x0087` | LiveVideoZoomFactor |
| `0x009c` | ImageFormatOption |

A parameter request therefore carries `[u16 code][value]` in its payload. The remaining
unknown is the outer opcode that distinguishes read from write, which is set by
`CCameraDeviceUSB::ReadParameter` / `WriteParameter` in the request header.
