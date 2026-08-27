# mpc4k — MPC 4000 file manager over USB

Manage the files on the Akai MPC 4000's internal drive (and CF card) from
the Mac, over the USB cable, speaking the ak.sys protocol (as
reverse-engineered by the Aksy project). Works with the Z4/Z8 too.

## macOS app

**`MPC 4000 File Manager.app`** (installed in `/Applications`).

- Connects automatically when the MPC is on and plugged in (green dot).
- Browse the internal drive; the segmented control switches between the
  internal drive and the removable USB drive.
- **Upload**: drag files from Finder into the window (or toolbar ⬆).
- **Download**: select files → toolbar ⬇ (or right-click → Download…).
- **Play**: double-click a WAV/AIFF (or select it and press Space) to
  hear it — the file streams from the MPC into a local cache and plays
  on the Mac; press Space again to stop. Repeat plays are instant.
- **Rename** (✏), **Delete** (🗑, with confirmation — folder deletes are
  recursive), **New Folder** (📁+), double-click to enter folders,
  breadcrumbs to go back.
- **Finder-style tree**: click the chevron next to a folder to expand it
  in place (contents load over USB on first expand and stay cached until
  refresh); all operations work on nested rows too.
- **RAM**: the third segment shows what is loaded in the MPC's memory
  (Samples / Programs / Multis / Songs). Double-click a sample to hear
  it, download items to the Mac (samples come out as WAV, programs as
  .akp, multis as .akm, songs as .mid), or use the save button (✓ drive
  icon, or right-click an item) to write RAM to a folder on the MPC disk
  — programs are saved together with their samples, like SAVE on the
  unit. RAM is read-only from the manager: no upload/rename/delete.

The app bundles the Python backend and libusb, so it has no external
dependencies. Rebuild after changes: `~/mpc4000-tools/mpc4k/app/build.sh`.

## CLI

```bash
python3 ~/mpc4000-tools/mpc4k/mpc4k.py info                 # connection + disk list
python3 ~/mpc4000-tools/mpc4k/mpc4k.py ls "2 Drums/06 Snare"
python3 ~/mpc4000-tools/mpc4k/mpc4k.py put local.wav "2 Drums/06 Snare"
python3 ~/mpc4000-tools/mpc4k/mpc4k.py get "2 Drums/06 Snare/file.wav" ~/Desktop
python3 ~/mpc4000-tools/mpc4k/mpc4k.py mkdir NewFolder
python3 ~/mpc4000-tools/mpc4k/mpc4k.py mv old.wav new.wav   # also cross-folder move
python3 ~/mpc4000-tools/mpc4k/mpc4k.py rm file.wav          # or a folder (recursive!)
python3 ~/mpc4000-tools/mpc4k/mpc4k.py mem                  # list RAM contents
python3 ~/mpc4000-tools/mpc4k/mpc4k.py memget "Kik 90s 2 MPC60" ~/Desktop
python3 ~/mpc4000-tools/mpc4k/mpc4k.py memsave "7 Recordings" # save all RAM to a disk folder
python3 ~/mpc4000-tools/mpc4k/mpc4k.py serve                # JSON backend used by the app
```

Remote paths use `/`; `--disk HANDLE` picks a disk (default: first
writable hard disk = the internal drive).

## Protocol notes

- USB: vendor 0x09E8, product 0x0061 (MPC4000) / 0x005F (Z4/Z8),
  bulk endpoints 0x02/0x82, init bytes `03 01` after claiming interface 0.
- Sysex commands wrapped as `10 <len16-LE> F0 47 5F ... F7`; slow
  disk-touching commands use the S56K-compat set (device id 0x5E,
  section 0x10, userref `20 00 00`); the sampler streams `AkaI` busy
  markers while working.
- File transfer is a separate bulk block protocol: PUT = `40 <size BE32>
  <name> 00`, GET = `41 <name> 00`, 8-byte status blocks
  (transferred BE32, next-block BE32), 1-byte `00` acks.
- Architecture: a single daemon (`mpc4k.py daemon`, auto-spawned, unix
  socket `/tmp/mpc4kd-<uid>.sock`) owns the sampler connection; both GUI
  apps are clients of it. The sampler sees exactly one session. The daemon
  releases the USB claim after ~10s idle so the CLI can also get in.
- The MPC4000's firmware can HANG (front panel freeze, USB drop) when
  sysex commands are sent back-to-back at full USB speed — observed twice
  on hardware, reproduced with a recursive folder scan. All commands are
  therefore paced (30ms fast / 90ms slow minimum gaps). Avoid tight
  command loops without pacing. Recovery from a hang: power-cycle the MPC.
- After the sampler reboots, its current disk is unpredictable (it came
  back on the USB stick once). The daemon detects reboots via USB
  re-enumeration and re-selects the default disk; the CLI selects its
  target disk on every invocation (slow on the internal drive but safe).
- RAM is volatile: a power-cycle clears all loaded samples/programs/
  sequences. Save to disk before heavy experimentation.
- `select_disk` on the internal drive triggers a ~50s firmware mount/
  re-scan (the USB stick switches instantly; test_disk and the fast-set
  select don't avoid it — it's a fixed firmware wait). The manager
  therefore (a) never re-selects a disk that is already current, and
  (b) on a real internal↔USB switch shows the cached tree immediately
  and runs the select + refresh in the background — operations clicked
  meanwhile are queued and run after the switch.
- If a transfer/command is interrupted mid-stream the bulk endpoints can
  stall ("pipe error"); recover with `UsbDevice.clear_halt()` +
  `reset()` (usbio.py) or power-cycle nothing — reconnect after reset.
- The MPC needs no special screen/mode; transfers work while it idles.
