# Firmware signing and the upgrade path

Why this matters: every remaining "on-camera" idea — ProRes, the unexposed 2756×1240 and
2756×2064 video modes, any GUI change — needs modified firmware on the body. This document
records what actually stands in the way, established from the 1.5.2 image rather than
assumed.

Short version: **the Android boot bundle (`normal.img`) is signed with a DJI key and
verified by a TEE-backed `dji_fw_verify`; the rest of the update is not covered by that
check, and the peripheral upgrade path has no signature check.** Re-signing the Android
image still requires DJI's private key or a flaw in the TEE/recovery verifier. The
2026-11-05 audit in [`SIGNING-AUDIT.md`](SIGNING-AUDIT.md) separates what is actually gated
from what was assumed to be.

---

## 1. The OTA is signed with a DJI key, not AOSP test keys

The build reports `userdebug/test-keys`, which is misleading — that refers to Android's
*app*-signing keys, not the OTA signing key. The package is signed with a self-signed DJI
certificate:

```
subject = C=CN, ST=Guangdong, L=Shenzhen View, O=Dji, OU=Dji, CN=Dji,
          emailAddress=dji@dji.com
issuer  = (same - self-signed)
sha256   9D:76:04:48:F1:7C:8A:4D:10:29:BA:78:7F:32:9F:37:
         F8:7D:0D:D8:DA:F5:1F:75:5B:82:BD:17:42:DD:BD:2A
```

`META-INF/com/android/otacert` is a copy of the signer certificate, and
`META-INF/CERT.RSA` is the package signature; both resolve to that fingerprint. That does
not, by itself, prove what recovery trusts: the trust anchor is `/res/keys` in the
recovery ramdisk, which is inside the encrypted `recovery.img` and was not read. The
system's `/etc/security/otacerts.zip` holds a **different** `testkey` certificate (see
[`SIGNING-AUDIT.md`](SIGNING-AUDIT.md) §F3). The private key is not in the image, and the
familiar AOSP test-key trick does not apply: the DJI `testkey` is a different RSA-2048 key,
not the public AOSP one.

Evidence: `openssl x509` / `openssl pkcs7 -print_certs` over the two files in the
extracted 1.5.2 package.

---

## 2. The upgrade path verifies `normal.img` before it flashes

From `/bin/upgrade.sh` inside the extracted `system.img`:

```sh
fw_path=/storage/sdcard0/upgrade/              # firmware is installed from the SD card
upgrade_list=("cpld" "fpga" "spc" "suc" "eagle")

for module_name in ${upgrade_list[*]}; do
    device_fw=`busybox find $fw_path -name "$module_name-*.bin"`
    ...
done
```

Firmware is dropped on the card as `cpld-*.bin`, `fpga-*.bin`, `spc-*.bin`, `suc-*.bin`,
`eagle-*.bin`, each installed by its own script. The Android SoC ("eagle") is the one that
matters for anything discussed here:

```sh
# upgrade_eagle()
busybox unzip /cache/ota.zip normal.img -d /tmp
dji_fw_verify -n normal /tmp/normal.img        # <- signature gate
if [ $? != 0 ]; then
    echo "dji_fw_verify normal.img failure, please make sure that you've
          used a right user/prod ota.zip."
    exit 1
fi
mkdir -p /cache/recovery
echo "--update_package=/cache/ota.zip" > /cache/recovery/command
unrd -s boot.mode recovery
reboot recovery
```

So a modified `normal.img` must satisfy `dji_fw_verify` before the camera will even reboot
into recovery to apply it. Note what this does **not** say: the script checks only
`normal.img`. The rest of `ota.zip` — `system.new.dat`, `vendor.new.dat`, `recovery.img`,
`bootarea.img`, `updater-script` — is not passed to the verifier here. It is covered, if at
all, by recovery's whole-file JAR check after the reboot. See
[`SIGNING-AUDIT.md`](SIGNING-AUDIT.md) §F2.

---

## 3. There is an on-device service API (`odindb-send`)

The camera exposes its services over an internal bus, accessible from shell during
development. `/bin/test_sd_upgrade.sh` is the most revealing:

```sh
odindb-send -s upgrade -m developer_override_downgrade_check 1
odindb-send -s upgrade -m upgrade_check
cim_uuid=$(odindb-send -s upgrade -p upgrades_available | ... | sort | tail -n1)
odindb-send -s upgrade -m upgrade sd:$cim_uuid
```

and `/bin/upgrade_common.sh` shows camera control through the same bus:

```sh
odindb-send -s camera -p live_view
odindb-send -s camera -m set_live_view_state false
```

Two observations:

* There is a **`developer_override_downgrade_check`** flag — downgrade protection is
  explicitly overridable in a developer path.
* This bus is far richer than the USB parameter protocol (it has per-service methods, not
  just 16-bit codes). It is only reachable with a shell on the camera.

`/bin/start_upgrade_daemon.sh` is simply `/system/bin/upgraded`, the daemon that services
these requests.

---

## 4. What this closes — and what it does not

* **No re-signed Android firmware.** The boot bundle is TEE-verified and the OTA is
  RSA-signed; re-signing needs DJI's private key.
* **Not "no unsigned firmware".** The CPLD, FPGA, SPC and SUC firmware is flashed from
  `cpld-*.bin`, `fpga-*.bin`, `spc-*.bin`, `suc-*.bin` with no signature check in the
  upgrade scripts/tools (§6, F1). Whether the MCU bootloader self-verifies is not
  established. That is a real unauthenticated-firmware path, just not one that carries
  `victory-gui-static`.
* **Not "the whole update is verified before flashing".** `upgrade.sh` verifies only
  `normal.img`; `system.new.dat`, `vendor.new.dat`, `recovery.img`, `bootarea.img` and the
  updater script are not passed to `dji_fw_verify`. Recovery's whole-file verifier is the
  only thing covering them (§6, F2).
* **Downgrade protection is not part of the signing gate.** `dji_fw_verify` checks a
  signature, not a version; `upgrade.sh` has no version check. The `upgrade_check` /
  `developer_override_downgrade_check` logic is in the `upgraded` service path. Whether
  the SD-card UI can reach `upgrade.sh` without that service check is untested.

The original conclusion — that replacing the Android system requires a signing bypass or
code execution — still holds. What changes is where those might come from: the TEE TA, the
recovery verifier, the CIM/HBL staging path, and the unauthenticated peripheral path, not
the visible RSA key.

## 5. What is still unexamined (updated 2026-11-05)

The four items from the first pass, now with what the audit established:

1. **`dji_fw_verify` itself.** It is a thin CLI over `libfw_util.so` →
   `libfw_util_ca.so` → TEEC; the userland side is fail-closed, and the actual check is in
   the TEE. `str2id` maps the `-n` name to a four-character ID and does not reject unknown
   names; the TA is in the encrypted TZOS sub-image of `normal.img` and was not audited.
2. **The non-eagle modules.** Now read. `upgrade_cpld.sh`, `upgrade_fpga.sh`,
   `upgrade_spc.sh`, `upgrade_suc.sh` perform **no** signature verification; the CPLD only
   md5-compares the read-back. They do not carry the Android system, but the host path does
   not authenticate them (F1).
3. **The `upgraded` daemon.** `*.sig`, `current.sig`, `sig.cim` and a TEE-backed
   `Sig::verify` path exist. Whether the check covers the CIM's `hbl-upgrade` script (as
   opposed to only `ota.zip`) is the highest-impact open question (F5).
4. **Recovery mode.** `/system/bin/update_engine` is the recovery/verifier binary and
   loads `/res/keys`; the recovery ramdisk is encrypted, so `/res/keys` was not read.
   Recovery ADB was not tested.

Any of these is still a research project with a real chance of an unusable camera. The
2026-11-05 audit below is the offline part; nothing was flashed.

## 6. Poking the gate — summary of the 2026-11-05 audit

Full detail, evidence and reproduction commands: [`SIGNING-AUDIT.md`](SIGNING-AUDIT.md).

| # | finding | status | effect |
|---|---|---|---|
| F1 | no signature check in the CPLD/FPGA/SPC/SUC host upgrade path | confirmed | persistent unsigned firmware from an SD card if the MCU does not self-verify |
| F2 | `upgrade.sh` verifies only `normal.img` | confirmed scope | system/vendor/recovery/bootarea depend on recovery's separate zip check |
| F3 | system `otacerts.zip` is a different `testkey` from the OTA signer | confirmed | trust-store mismatch; recovery's real `/res/keys` unread |
| F4 | OTA signature is RSA-2048/SHA-1; manifest SHA-256 fields are inconsistent | confirmed | integrity rests on SHA-1; long-term collision risk |
| F5 | CIM is encrypted, not internally signed; HBL scripts run as root | open | **if** the `.sig` check does not cover them, crafted CIM = root code execution |
| F6 | `dji_fw_verify` is fail-closed in userland; TA unaudited | confirmed/open | unknown-ID handling (`-n ""`) is the bench test |
| F7 | bare-name `dji_fw_verify`, unquoted paths, stale `/tmp/normal.img`, no version check in `upgrade.sh`, recovery ADB | latent | low/unknown |

The two facts that most change the earlier conclusion:

* the phrase "no unsigned firmware" is at best unproven — the host path does not check
  the peripherals;
* the phrase "the update is verified" describes `normal.img`, not the whole package.

The Android system still cannot be re-signed without DJI's key, and no end-to-end bypass
was demonstrated. The next tests that would settle it are in
[`SIGNING-AUDIT.md`](SIGNING-AUDIT.md) §8: read `/res/keys`, run the modified
`system.new.dat` package on a bench unit, and test the CIM `hbl-upgrade` path.
