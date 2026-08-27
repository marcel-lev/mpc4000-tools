# MPC 4000 Tools

Modern macOS tools for the Akai MPC 4000, replacing the abandoned
PowerPC-era ak.sys software. Everything talks to the sampler over its
USB port using the ak.sys protocol (as reverse-engineered by the
open-source [Aksy](https://github.com/watzo/aksy) project) — no drivers,
no kernel extensions, no Rosetta.

| App | Source | What it does |
|-----|--------|--------------|
| **MPC 4000 File Manager** | `mpc4k/app/` | Browse/manage the MPC's internal drive + USB drive over USB: upload (drag & drop), download, rename, delete, folder tree, audio preview, RAM view with save-to-disk |
| **MPC 4000 Program Editor** | `mpc4k/editor/` | Live ak.sys-style editing of keygroup programs in the MPC's RAM: filter/envelopes/tune/zones, native EDIT ALL, waveform view with draggable trim & loop markers, remote audition |
| **MPC EXS to MPC** | `exs2akp/` | Convert Logic EXS24 sampler instruments to Akai `.akp` programs + 24-bit/44.1 kHz WAVs, ready for the MPC 4000 |

Shared backend: `mpc4k/mpc4k.py` (protocol + CLI + daemon) and
`mpc4k/usbio.py` (ctypes libusb binding). The GUI apps are thin SwiftUI
frontends; a single auto-spawned daemon owns the USB connection so all
apps (and the CLI) share the sampler safely.

## Building

Each app dir has a `build.sh` that compiles the SwiftUI frontend,
bundles the Python backend + libusb, renders the icon, and installs
into `/Applications`:

```bash
mpc4k/app/build.sh       # MPC 4000 File Manager.app
mpc4k/editor/build.sh    # MPC 4000 Program Editor.app
exs2akp/app/build.sh     # MPC EXS to MPC.app
```

Requirements: macOS 13+, Xcode command line tools, Homebrew libusb
(`brew install libusb`) for building (the built apps bundle it).

## Docs

- `mpc4k/README.md` — file manager, program editor, CLI, and the
  ak.sys USB protocol notes (including MPC4000 firmware gotchas:
  command pacing, the ~50s internal-drive mount, volatile RAM)
- `exs2akp/README.md` — converter usage and EXS24→AKP mapping details

## Credits

Protocol knowledge derives from the Aksy project (Walco van Loon) and
the burnIT AKP format specification (Seb Francis); EXS24 format details
cross-checked against ConvertWithMoss (Jürgen Moßgraber) and
renoise-exs24 (Matt Allan). Not affiliated with Akai / inMusic.
