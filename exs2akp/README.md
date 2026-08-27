# exs2akp

Convert Logic EXS24 sampler instruments (`.exs`) into Akai `.akp` keygroup
programs + WAV samples for the S5000/S6000, Z4/Z8 and **MPC 4000**.

No dependencies — plain Python 3 (uses macOS's built-in `afconvert` for
format conversion). Samples are always written as **24-bit / 44.1 kHz**
WAV; other rates are resampled and loop/trim points rescaled to match.

## macOS app

**`MPC EXS to MPC.app`** (installed in `/Applications`) — drag-and-drop GUI
around the same converter.
Drop `.exs` files (or folders — they're scanned recursively) onto the
window, or click the drop zone to pick files. Choose the output folder,
hit **Convert**, then **Show in Finder**. Each instrument becomes its
own subfolder with the `.akp` + WAVs.

Rebuild after changing `exs2akp.py` or the UI:

```bash
~/mpc4000-tools/exs2akp/app/build.sh
```

(The app bundles a copy of `exs2akp.py` in its Resources; rebuilding
picks up converter changes and reinstalls into /Applications.)

## CLI usage

```bash
# convert one or more instruments
python3 exs2akp.py convert "My Instrument.exs" -o ./akp-out

# inspect an .exs
python3 exs2akp.py dump "My Instrument.exs"

# validate any .akp (also works on programs from the sampler itself)
python3 exs2akp.py check akp-out/*/*.akp
```

Output: one folder per instrument containing `<name>.akp` and its WAVs.
Copy the folder to the sampler's CF card / disk; program and samples must
stay in the same directory. Load the program with "with samples" enabled.

## What gets mapped

- EXS zones → AKP keygroups by key range; up to 4 velocity layers per
  keygroup (more layers → overlapping keygroups, which the format allows)
- Root note and loop points → written into each WAV's `smpl` chunk
  (that's where the Akai hardware reads them; zone playback = AS SAMPLE).
  Loops come from the EXS zone, or are carried over from the source
  audio's own metadata (WAV `smpl`, AIFF `INST`/`MARK`) — afconvert alone
  would silently drop these.
- Amp envelope (A/D/S/R), velocity sensitivity → AKP amp env / velo sens
  (EXS 0..127 knobs map linearly onto Akai 0..100 knobs)
- Filter: type (LP/HP/BP pole counts), cutoff, resonance, key tracking,
  filter envelope + env depth (depth read from the EXS mod matrix)
- Group volume/pan folded into zones; group velocity ranges gate zones;
  muted groups are skipped
- EXS `exclusive` groups → AKP mute groups (hi-hat choking works)
- Round robin: AKP has no equivalent — the first round-robin round is
  kept, the rest are dropped (with a warning), so RR instruments play
  their primary articulation instead of flamming
- Zone sample start/end trimming and reverse playback → applied to the
  exported WAV audio (per-zone WAV variants created when needed)
- One-shot zones → ONE SHOT playback; pitch tracking on/off per zone
- Per-zone velocity range, coarse/fine tune, pan, volume
- Sample names sanitized to the 20-char AKP limit, deduplicated
- Stale sample paths inside the `.exs` (dead absolute paths, classic-Mac
  colon paths) are resolved by searching the EXS library layout
  (`Sampler Instruments` / `EXSamples`), then Spotlight as a fallback

## Not mapped

- LFOs, mod matrix routings other than filter-env depth (AKP defaults)
- Loop crossfades (the loop itself is kept)
- AKP key ranges start at MIDI note 21 (A0) — lower EXS keys are clamped
- Envelope times use a knob-to-knob mapping (both machines use
  perceptual 0..max knob curves); absolute times may differ slightly

## Format notes

- Writes the S5000/S6000 dialect of `.akp` (RIFF size field zero, 46-byte
  zones), which the Z4/Z8/MPC4000 line also loads.
- The MPC4000's own keygroup programs are `.akp`; drum programs are a
  different (undocumented) internal layout — this tool produces keygroup
  programs, playable from pads via note ranges.
