# Signing audit — poking the gate (2026-11-05)

This is the follow-up to [`UPGRADE-AND-SIGNING.md`](UPGRADE-AND-SIGNING.md). That note
established that the Android boot bundle is DJI-signed and verified by `dji_fw_verify`;
this one asks what that check actually covers, whether the visible keys are weak, and
where the next weak link is.

**Bottom line:** the visible PKI is not weak in the "short key" sense. The OTA signer is
RSA-2048 with `e = 65537`; the system `testkey` is a different RSA-2048 key; no shared
primes. But the check is narrower than "the firmware is signed":

1. the SD-card upgrade script verifies **only `normal.img`**, the boot bundle — not the
   system/vendor payloads, the recovery image, the bootloader area, or the updater script;
2. the CPLD, FPGA, SPC and SUC firmware is flashed with **no signature check in the host
   upgrade path** (whether the MCUs self-verify is not established);
3. the OTA signature is **SHA-1**, and the signed manifest's SHA-256 fields do not match
   the files; the SHA-1 lines are the only consistent ones;
4. the system ships a **different** `testkey` certificate from the one that signed the
   OTA, and the recovery trust anchor (`/res/keys`) is inside the encrypted recovery
   image and was not read.

None of these is a demonstrated arbitrary-code-execution exploit yet. The first two are
real coverage gaps; the rest are where to look next.

No camera was modified and nothing was flashed. Everything below is offline analysis of
the 1.5.2 image.

---

## 0. What was checked

| check | result |
|---|---|
| OTA signer key size / exponent | RSA-2048, `e = 65537` |
| OTA signer signature algorithm | `sha1WithRSAEncryption`; PKCS#7 digest `sha1` |
| System `testkey` key size | RSA-2048, `sha256WithRSAEncryption` |
| Shared prime between OTA key, `testkey`, AOSP `testkey` | none (GCD = 1 for all pairs) |
| OTA JAR signature | valid over `META-INF/CERT.SF` with the 9D:76 cert |
| Per-file SHA-1 in `MANIFEST.MF` | 15/15 match inside `ota.zip` |
| Per-file SHA-256 in `MANIFEST.MF` | 6/14 mismatch (1 entry has no SHA-256 line) |
| `dji_fw_verify` | userland is a thin, fail-closed TEE client; TA is in TZOS |
| `upgrade.sh` verification scope | `normal.img` only |
| Peripheral upgrade scripts | no signature/verify call at all |
| CIM container | AES-encrypted with a hard-coded key; no RSA-sized signature in it |
| Recovery verifier | `/system/bin/update_engine` is the recovery binary; loads `/res/keys` |

---

## 1. F1 — The peripheral upgrade path has no host-side signature check (confirmed, high if the MCU does not self-verify)

`upgrade.sh` is the SD-card entry point. Its list is:

```sh
upgrade_list=("cpld" "fpga" "spc" "suc" "eagle")
```

For each non-eagle module it runs `upgrade_$1.sh $device_fw`, where `$device_fw` is
whatever the card contains matching `cpld-*.bin`, `fpga-*.bin`, `spc-*.bin`,
`suc-*.bin`. Those four scripts contain **no signature verification**. The actual writes:

| module | command | host-side integrity check |
|---|---|---|
| CPLD | `cat $firmware > /dev/mtd/mtd0` | md5 of the read-back vs. the file just written |
| FPGA | `send_fw -l $firmware -r upgrade://farmus` | none in the host script |
| SPC | `send_fw -l $firmware -r upgrade://pwrctrl` | none in the host script |
| SUC | `stm32flash -f -v -w $firmware -b 115200 /dev/ttyS1` | STM32 flash verify only |

The CPLD check is the clearest example of a check that looks like authentication but is
not: it compares the md5 of what was written with the md5 of what was read back. Both
sides are the attacker's file, so it only detects a bad flash, not a bad image.

`send_fw` / `upgrade_fw` import no RSA/verify code and contain no `dji_fw_verify`; the only
hash-looking string in them is `.hash`. A hash sent alongside the image is not a
signature. This establishes the **host-side** gap; whether the FPGA/SPC/SUC bootloader
independently verifies an embedded signature was not established (the remote firmware was
not extracted).

Impact is peripheral-dependent, but this is not a harmless display-only path:

* the CPLD bitstream is written to the SPI NOR (`/dev/mtd/mtd0`, `dji-spinor`), i.e. the
  same class of persistent storage the board boots around;
* the SUC is the system-control MCU, reached over UART (`/dev/ttyS1`); the script uses it
  to hold/release reset and set `autostart`;
* the FPGA is an RT node (`farmus`) loaded at runtime over a custom transport.

If the peripheral does not self-verify, this is persistent unsigned firmware on the
peripheral processors, with an SD-card insertion as the vector. Whether any of them can
reach the Android SoC is not established here; the point is that the host-side upgrade
path does not enforce a signature, so the earlier note's "no unsigned firmware" claim is
at best unproven.

**Reproduction.** Extract the scripts from `system.img` and read them:

```sh
python3 - <<'PY'
from dissect.extfs import ExtFS
fs = ExtFS(open('/tmp/fw152/system.img', 'rb'))
for p in ('/bin/upgrade.sh', '/bin/upgrade_cpld.sh', '/bin/upgrade_fpga.sh',
          '/bin/upgrade_spc.sh', '/bin/upgrade_suc.sh'):
    open('/tmp/' + p.rsplit('/', 1)[-1], 'wb').write(fs.get(p).open().read())
PY
grep -nE 'dji_fw_verify|verify|sign|md5|stm32flash|send_fw|cat .*mtd' /tmp/upgrade*.sh
```

---

## 2. F2 — The pre-flash gate covers only `normal.img` (confirmed scope, impact conditional)

The script that gates the Android update is short:

```sh
busybox unzip /cache/ota.zip normal.img -d /tmp
dji_fw_verify -n normal /tmp/normal.img
if [ $? != 0 ]; then ... exit 1; fi
echo "--update_package=/cache/ota.zip" > /cache/recovery/command
reboot recovery
```

Only `normal.img` is passed to the verifier. `normal.img` is not the Android system; it is
the boot bundle. Its cleartext IM*H table (1.5.2) is:

| item | offset | size | id |
|---|---:|---:|---:|
| TZOS | 0x000000 | 0x04f800 | 0x007d |
| KERN | 0x04f800 | 0x5be510 | 0x0005 |
| LRFS | 0x60dd20 | 0x0bf2ec | 0x0006 |
| LDTB | 0x6cd020 | 0x00aa28 | 0x0008 |
| DTB2–DTB6 | 0x6d7a60 … | ~0xad00 each | 0x0208… |
| DTB2–DTB6 (second set) | 0x70dbe0 … | ~0xad00 each | 0x8208… |

`recovery.img` is the same bundle with a much larger LRFS (the recovery ramdisk,
0xfab4c2 bytes). `system.new.dat`, `vendor.new.dat`, `recovery.img`, `bootarea.img` and
`META-INF/com/google/android/updater-script` are **not** passed to `dji_fw_verify`.

That means the script-level gate is exactly: "the boot bundle is genuine." Everything else
in the zip depends on recovery's whole-file JAR verification (and on the bootloader's own
checks for `recovery.img`/`bootarea.img`). If recovery verifies the zip, the system payload
is covered by a second, different check. If it does not — or if it can be made to skip
verification — a modified `system.new.dat` paired with the genuine `normal.img` passes
`upgrade.sh` and installs.

The recovery binary is present in the system image. `/system/bin/update_engine` is not the
AOSP A/B `update_engine`: it contains the Android recovery UI strings, `Android Recovery`,
`/res/keys`, `load_keys`, `verify_file`, and `E:failed to verify whole-file signature`.
It loads trusted keys from **`/res/keys`**, not from `/system/etc/security/otacerts.zip`.
`/res` does not exist in `system.img`, so the verifier must be running with the recovery
ramdisk's `/res/keys` present (recovery boot, or an HBL root that carries it). That file is
inside the encrypted `recovery.img` LRFS and was not read.

**Consequence.** The sentence "the update is signed and verified before flashing" is true
of `normal.img` and, separately, of the zip if recovery verifies it. It is not a single
check over the whole package. The practical test is to modify `system.new.dat`, keep the
genuine `normal.img`, and see whether recovery refuses the package.

A local two-layer check (no camera involved) makes the gap concrete. Flip one byte in
`system.new.dat`, re-zip with the original `normal.img` and `META-INF/`, and compare:

| file | SHA-1 vs. `MANIFEST.MF` | what catches it |
|---|---|---|
| `normal.img` | still matches | `dji_fw_verify` would see a byte-identical file and pass |
| `system.new.dat` | no longer matches | only recovery's JAR verifier |
| `META-INF/CERT.RSA` over `CERT.SF` | still verifies | it was not touched |

So the pre-flash gate cannot see the system modification at all; the recovery verifier is
the only layer that does. That is the exact question F2 leaves open.

---

## 3. F3 — The trust stores disagree, and one is a `testkey` (confirmed, impact unknown)

There are two different DJI certificates in play:

| where | subject | key | sig alg | SHA-256 fingerprint | validity |
|---|---|---|---|---|---|
| OTA signer (`META-INF/CERT.RSA`, `otacert`) | `C=CN, ST=Guangdong, L=Shenzhen View, O=Dji, OU=Dji, CN=Dji` | RSA-2048 | SHA-1 | `9D:76:04:48:F1:7C:8A:4D:10:29:BA:78:7F:32:9F:37:F8:7D:0D:D8:DA:F5:1F:75:5B:82:BD:17:42:DD:BD:2A` | 2017-08-24 → 2045-01-09 |
| `/etc/security/otacerts.zip` → `testkey.x509.pem` | `C=CN, ST=Guangdong, L=Shenzhen, O=DJI, OU=DJI, CN=DJI` | RSA-2048 | SHA-256 | `15:9F:F0:8F:85:E7:89:C2:88:FA:F6:EA:63:82:5F:AB:5C:DE:25:14:88:C5:3F:72:8D:EA:A6:8E:C5:B6:BD:78` | 2017-12-25 → 2045-05-12 |

They are not the same key. The `testkey` is **not** the public AOSP test key either: its
modulus differs from AOSP's `testkey.x509.pem`, and the GCDs of all three moduli are 1. So
the familiar "AOSP testkey is public, therefore sign your own OTA" trick does not work
here. But a file named `testkey.x509.pem` in a production image is a process red flag: it
says the build is still carrying a test key in a trust store, and its private-key handling
and rotation are unknown.

Which one recovery trusts is unresolved because `/res/keys` was not read. The system
`otacerts.zip` is either unused (a build leftover), used by a component other than the
recovery verifier, or — the bad case — the recovery key set, in which case the official
OTA would not verify and something else must be going on. Reading `/res/keys` from
recovery settles it.

---

## 4. F4 — The OTA signature is real but SHA-1-only; the SHA-256 fields are inconsistent (confirmed anomaly, long-term risk)

The PKCS#7 signature over `META-INF/CERT.SF` verifies with the 9D:76 certificate
(`openssl smime -verify` returns 0). The digest algorithm is SHA-1, and `CERT.SF` carries
`SHA1-Digest-Manifest`.

More interesting: the signed `MANIFEST.MF` carries both SHA-1 and SHA-256 digests, but
only the SHA-1 values match the files in `ota.zip`. Of the 14 entries with a SHA-256 line,
6 mismatch:

| entry | SHA-1 | SHA-256 |
|---|---|---|
| `META-INF/com/google/android/updater-script` | match | **mismatch** |
| `normal.img` | match | **mismatch** |
| `vendor.new.dat` | match | **mismatch** |
| `bootarea.img` | match | **mismatch** |
| `vendor.transfer.list` | match | **mismatch** |
| `recovery.img` | match | **mismatch** |
| `update-binary`, `system.new.dat`, `system.transfer.list`, `vendor.patch.dat`, `system.patch.dat`, `metadata`, `file_contexts` | match | match |

The 1.5.2 package is the one the camera runs. If it was installed through the documented
recovery verifier, that verifier cannot be enforcing the SHA-256 lines as written: either
it uses SHA-1 (the Android 6 JAR default) or those lines are not over the file content.
The SHA-1 lines all match, and the PKCS#7 digest is SHA-1, so SHA-1 is the consistent
reading — and the "strong" SHA-256 fields in the manifest are decorative.

This is not a today's-laptop forgery. The signature is over the SHA-1 of the **manifest**,
so a modified file needs a modified manifest section whose overall SHA-1 still matches the
signed value — a chosen-prefix SHA-1 collision, not a second preimage. That is expensive,
but SHA-1 chosen-prefix collisions are a known, improving capability, and this key is
declared valid until 2045. The signing certificate dates from 2017 and is still on the
2022 build; no rotation is visible. The correct reading is "the OTA's integrity rests on SHA-1," not
"the OTA is broken."

---

## 5. F5 — The CIM/HBL layer: encrypted, not internally signed; TEE check unproven (open, potentially high)

The `.cim` outer container is fully understood ([`../tools/cim_unpack.py`](../tools/cim_unpack.py)):
public header + AES-128-CBC payload with a hard-coded key, restarting the IV every 4096
bytes. It contains four items:

| item | size | content |
|---|---:|---|
| `ota.zip` | 158,063,021 | the Android package |
| `hbl-upgrade` | 4,271 | `#!/system/bin/sh` script, run by the HBL |
| `hbl-post-upgrade` | 1,344 | `#!/system/bin/sh` script, run after reboot |
| `hbmanual_pack.tar` | 75,806,720 | PDFs |

There is **no RSA-sized signature in the CIM header** — the visible crypto is the AES
layer, which is only obfuscation because the key is in `upgraded`. The per-item 16-byte
fields are not a plain MD5, SHA-1 or SHA-256 of the item payload; their role (UUID, nonce,
MAC) is unresolved. Authentication, if it exists, is external (a `.sig` sidecar) or in the
TEE.

That matters because `hbl-upgrade.sh` runs in the HBL/root context: it copies the OTA to
`/cache`, switches the `unrd` slots, loads `dji-spinor`, chroots into the new system and
runs `program_nodes.sh`. If an attacker can supply a CIM whose `hbl-upgrade` item is
modified while the inner OTA stays genuine, that is root code execution without touching
the signed Android image.

The daemon does have the machinery for a check. `upgraded` contains:

```
*.sig
../sig/sig.cpp
/blackbox/upgraded/cim/current.sig
/blackbox/upgrade/sig.cim
dji_fw_verify
IM*H
```

and a `Sig::verify`-shaped function (around `0x29e98`) that maps a file with
`open`/`fstat`/`mmap`, calls `dji_fw_verify(data, size, id, …)`, then `munmap`/`close`.
The `dji_fw_verify` import resolves to the TEE client. So a detached signature checked by
the TEE is the likely design.

What is **not** established: whether that check is applied to the CIM as a whole, to each
item (including `hbl-upgrade`), or only to `ota.zip`; whether the `.sig` is mandatory or
only checked when present; and whether it is a signature rather than a hash container. The
per-item 16-byte fields do not look like a plain hash, which is consistent with a per-item
MAC/TEE policy, but it is not proof. Until a modified CIM is run through `upgraded` on a
bench unit (or the daemon is reversed further), this is the highest-impact open question.

---

## 6. F6 — `dji_fw_verify` itself: thin, fail-closed, TEE-backed (confirmed; TA unaudited)

`dji_fw_verify` is a 13.9 KB CLI. It parses `-n <name>` (required), `-c`, `-o`, `-s`,
`-h`; it maps the name through `str2id` and calls `dji_fw_verify_file(path, name, id, …)`.
The library path is:

```
bin/dji_fw_verify
  → libfw_util.so   (ION buffer, memcpy, fw_util_verify)
    → libfw_util_ca.so  (TEEC_* client)
      → libteec.so → TEE driver → TA in TZOS
```

`libfw_util_ca.so` returns `-1` on `TEEC_InitializeContext`, `TEEC_OpenSession`, or
`TEEC_InvokeCommand` failure. I found no fail-open path in the userland wrappers. There is
no RSA code in `libfw_util.so`/`libfw_util_ca.so`; the actual verification is in the TEE
trusted application.

`str2id` simply packs up to four characters into an integer (`str2id("normal")` →
`"norm"`), and returns 0 for an empty string. It does **not** reject unknown names; whether
the TA treats an unknown/zero ID as "no policy" is the one userland-visible question worth
testing. The CLI accepts `-n ""`, so `dji_fw_verify -n "" <file>` is a two-minute bench
test. If it returns 0, the gate can be skipped by anyone who can run the binary (the SD
script hardcodes `normal`, so this is not by itself a remote bypass, but the `upgraded`
daemon passes item-derived names).

The TA itself is in the TZOS sub-image of `normal.img`, which is encrypted (`TZOS` is
325,632 bytes at offset 0 in the bundle). The key is not in the OTA. So the visible PKI
being strong does not prove the TEE policy is strong; the root of trust was not audited.

---

## 7. F7 — Minor/latent issues (confirmed but low)

* `upgrade.sh` calls `dji_fw_verify` by bare name while the other scripts use absolute
  paths. On the normal Android `PATH` this is not writable, so it is latent, not a bypass.
* The pre-check ignores `busybox unzip`'s exit status and tests only
  `[ -f /tmp/normal.img ]`. A stale `normal.img` left by an earlier run can satisfy the
  check even when the new zip's extraction failed. It still needs a writable/persistent
  `/tmp`, and recovery's verifier (if it runs) remains the real gate, but the pre-check
  itself is weaker than it looks.
* `$fw_path` and `$device_fw` are unquoted; a crafted filename can inject extra arguments
  into `upgrade_*.sh`. The module list is fixed, so the effect is limited to those scripts'
  argument handling.
* `developer_override_downgrade_check` is a real service flag, but only reachable from a
  shell (`odindb-send`), not from USB or the SD card. Separately, `upgrade.sh` has no
  version/downgrade check at all; `dji_fw_verify` checks a signature, not a version. The
  version logic lives in the `upgraded` service path, so whether the SD-card UI can invoke
  `upgrade.sh` directly (and thus install an older, correctly signed build) is untested.
* `/system/bin/update_engine` is the recovery binary and includes adbd. If recovery ADB is
  enabled and unauthenticated, physical USB access in recovery is a total bypass. This was
  not tested; it needs a bench unit and `adb devices`/`ro.adb.secure` from the recovery
  ramdisk.

---

## 8. What would settle it

In rough order of value:

1. **Read `/res/keys`.** It is in the recovery ramdisk (LRFS in `recovery.img`, encrypted
   with the IM*H key). A recovery shell, a decrypted ramdisk, or a TZOS/TA dump would
   answer whether recovery trusts the 9D:76 OTA key, the 15:9F `testkey`, or both. This is
   the single fact that decides whether F2 is a defense-in-depth note or a full bypass.
2. **Run the scope test.** Modify `system.new.dat` (one byte is enough), keep the genuine
   `normal.img`, and feed the package through the SD-card path on a bench unit. If
   recovery accepts it, the pre-flash gate is bypassable exactly as described. If it
   rejects it, F2 is "only `normal.img` is pre-checked; recovery catches the rest."
3. **Test the ID handling.** On a bench unit: `dji_fw_verify -n "" <file>`,
   `dji_fw_verify -n test <file>`, and a name that is four characters but has no policy.
   A zero return for an unknown ID is a gate bypass.
4. **Test type binding.** Run `dji_fw_verify -n normal recovery.img` (and the reverse). If
   it returns 0, the `-n` ID selects a key but not the image type, and a genuine signed
   image of one type can be replayed as another. The file's own header name/ID (offset
   0x40 in the IM*H header) is the obvious thing to bind to; whether the TEE does is
   unknown.
5. **Test the CIM/HBL path.** Build a CIM with the genuine `ota.zip` and a modified
   `hbl-upgrade` (e.g. one that writes a marker to `/data`), with and without a `.sig`
   sidecar. Observe whether `upgraded` stages/runs it. This is the highest-impact
   unproven path.
6. **Audit TZOS.** Extract/decrypt the TZOS sub-image and find the `fw_util_verify` TA:
   key type/size, whether it checks the image type/ID, and how it handles unknown IDs.
7. **Check recovery ADB.** Boot recovery on a bench unit and see whether adbd is
   authenticated.

None of these requires modifying the production camera; items 1/6 are offline, the rest
are bench-unit tests.

---

## 9. Reproduction

The short version, assuming the 1.5.2 `.cim` is in the repo root and `/tmp/pylibs` carries
`pycryptodome`/`dissect`:

```sh
# 1. unpack the CIM (existing tool)
python3 tools/cim_unpack.py info    X1D_II_50C_v1_5_2.cim
python3 tools/cim_unpack.py extract X1D_II_50C_v1_5_2.cim /tmp/fw152

# 2. certificates and key sizes
openssl pkcs7 -in /tmp/fw152/META-INF_CERT.RSA -inform DER -print_certs \
  | openssl x509 -noout -subject -fingerprint -sha256 -text \
  | grep -E 'Public-Key|Exponent|Signature Algorithm|Not (Before|After)'
unzip -p /tmp/fw152/etc_security_otacerts.zip testkey.x509.pem \
  | openssl x509 -noout -subject -fingerprint -sha256 -text \
  | grep -E 'Public-Key|Exponent|Signature Algorithm|Not (Before|After)'

# 3. whole-file signature and manifest digests
python3 - <<'PY'
import base64, hashlib, re, zipfile
z = zipfile.ZipFile('/tmp/ota.zip')          # extracted from the CIM, see below
mf = z.read('META-INF/MANIFEST.MF').decode().replace('\r\n', '\n')
for sec in mf.split('\n\n'):
    m = re.search(r'^Name: (.+)$', sec, re.M)
    if not m:
        continue
    name = m.group(1)
    data = z.read(name) if name in z.namelist() else b''
    for alg, h in (('SHA1', hashlib.sha1), ('SHA-256', hashlib.sha256)):
        exp = re.search(rf'^{alg}-Digest: (.+)$', sec, re.M)
        if exp:
            got = base64.b64encode(h(data).digest()).decode()
            print(f'{name:48s} {alg:7s} {"OK" if got == exp.group(1) else "MISMATCH"}')
PY

# 4. normal.img table (TZOS/KERN/LRFS/DTB…)
python3 - <<'PY'
import struct
d = open('/tmp/fw152/normal.img', 'rb').read()
for i in range(15):
    o = 0xc0 + i * 0x20
    name = d[o:o+4].split(b'\0')[0].decode('latin1', 'replace')
    if not name:
        break
    off, size = struct.unpack_from('<II', d, o + 4)
    print(f'{name:5s} off={off:#010x} size={size:#010x}')
PY
```

`/tmp/ota.zip` can be carved exactly from the 1.5.2 CIM with `cim_unpack`:

```sh
python3 - <<'PY'
import sys
sys.path.insert(0, 'tools')
import cim_unpack as cu
data = open('X1D_II_50C_v1_5_2.cim', 'rb').read()
plain = cu.decrypt(data)
open('/tmp/ota.zip', 'wb').write(plain[0x580:0x580 + 158063021])
PY
```

---

## 10. Bottom line for the original question

* **Very short private keys / weak CAs?** Not here. Both visible keys are RSA-2048,
  `e = 65537`; no shared primes; the `testkey` is not the public AOSP one.
* **Is the check flawed and does it allow execution?** The check is real for `normal.img`,
  and it is TEE-backed. But it is not a check over the whole update, and the peripheral
  host path has no check; the MCUs' own validation is unknown. The most promising
  execution paths are (a) the system payload if recovery's verifier is skipped or bypassed,
  (b) a modified CIM `hbl-upgrade` if the detached signature does not cover the scripts,
  and (c) the unauthenticated peripheral path as a persistent, lower-layer foothold. None
  has been demonstrated end to end; F2 and F5 are the two tests worth running next.
