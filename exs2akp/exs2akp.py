#!/usr/bin/env python3
"""exs2akp — convert Logic EXS24 sampler instruments (.exs) to Akai .akp programs
(S5000/S6000/Z4/Z8/MPC4000) with samples exported as WAV files.

Usage:
  exs2akp.py dump <file.exs>                    Inspect a parsed EXS instrument
  exs2akp.py convert <file.exs>... [-o DIR]     Convert; writes DIR/<name>/<name>.akp + WAVs
      options:  -o DIR      output directory (default: ./akp-out)

Samples are always written as 24-bit / 44.1 kHz WAV (resampled if needed).

The .akp is written in the S5000/S6000 dialect, which Z4/Z8/MPC4000 also load.
Root note and loop points are carried in each WAV's 'smpl' chunk, as the
hardware expects. Copy the whole output folder onto the sampler's CF card.

No third-party dependencies. AIFF/CAF sources are converted with macOS's
built-in `afconvert`.
"""

import os
import re
import struct
import subprocess
import sys
import tempfile

# ---------------------------------------------------------------------------
# EXS24 reader
# ---------------------------------------------------------------------------

CHUNK_HEADER = 0x00
CHUNK_ZONE = 0x01
CHUNK_GROUP = 0x02
CHUNK_SAMPLE = 0x03
CHUNK_PARAMS = 0x04


class ExsZone:
    def __init__(self):
        self.name = ""
        self.id = 0
        self.one_shot = False
        self.pitch_tracking = True
        self.reverse = False
        self.vel_range_on = False
        self.root_note = 60
        self.fine_tune = 0
        self.coarse_tune = 0
        self.pan = 0
        self.volume = 0
        self.key_low = 0
        self.key_high = 127
        self.vel_low = 0
        self.vel_high = 127
        self.sample_start = 0
        self.sample_end = 0
        self.loop_start = 0
        self.loop_end = 0
        self.loop_on = False
        self.loop_direction = 0
        self.group_index = -1
        self.sample_index = -1


class ExsGroup:
    def __init__(self):
        self.name = ""
        self.id = 0
        self.volume = 0
        self.pan = 0
        self.polyphony = 0
        self.mute = False
        self.exclusive = 0
        self.vel_low = 0
        self.vel_high = 127
        self.enable_by_type = 0  # 2 = round robin
        self.round_robin_pos = -1


class ExsSample:
    def __init__(self):
        self.name = ""
        self.id = 0
        self.length = 0
        self.rate = 44100
        self.bit_depth = 16
        self.channels = 1
        self.file_type = ""
        self.compressed = False
        self.directory = ""
        self.file_name = ""


class ExsInstrument:
    def __init__(self):
        self.name = ""
        self.zones = []
        self.groups = []
        self.samples = []
        self.params = {}


def _cstr(data):
    return data.split(b"\x00", 1)[0].decode("utf-8", errors="replace")


def read_exs(path):
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 84:
        raise ValueError("file too small to be an EXS instrument")

    magic = data[16:20]
    if magic in (b"TBOS", b"JBOS"):
        end = "<"
    elif magic in (b"SOBT", b"SOBJ"):
        end = ">"
    else:
        raise ValueError("not an EXS24 file (missing TBOS/SOBT magic)")

    u32 = lambda b, o: struct.unpack_from(end + "I", b, o)[0]
    i32 = lambda b, o: struct.unpack_from(end + "i", b, o)[0]
    u16 = lambda b, o: struct.unpack_from(end + "H", b, o)[0]
    i16 = lambda b, o: struct.unpack_from(end + "h", b, o)[0]
    i8 = lambda b, o: struct.unpack_from("b", b, o)[0]

    expanded = u32(data, 4) > 0x8000

    inst = ExsInstrument()
    inst.name = os.path.splitext(os.path.basename(path))[0]

    pos = 0
    while pos + 84 <= len(data):
        sig = data[pos:pos + 4]
        ctype = sig[3] & 0x0F
        size = u32(data, pos + 4)
        if expanded and size > 0x8000:
            size -= 0x8000
        cid = u32(data, pos + 8)
        name = _cstr(data[pos + 20:pos + 84])
        body = data[pos + 84:pos + 84 + size]

        if ctype == CHUNK_ZONE and len(body) >= 96:
            z = ExsZone()
            z.name, z.id = name, cid
            opts = body[0]
            z.one_shot = bool(opts & 1)
            z.pitch_tracking = not (opts & 2)
            z.reverse = bool(opts & 4)
            z.vel_range_on = bool(opts & 8)
            z.root_note = body[1]
            z.fine_tune = i8(body, 2)
            z.pan = i8(body, 3)
            z.volume = i8(body, 4)
            z.key_low = body[6]
            z.key_high = body[7]
            z.vel_low = body[9]
            z.vel_high = body[10]
            z.sample_start = u32(body, 12)
            z.sample_end = u32(body, 16)
            z.loop_start = u32(body, 20)
            z.loop_end = u32(body, 24)
            if len(body) > 118:
                z.loop_on = bool(body[117] & 1)
                z.loop_direction = body[118]
            if len(body) > 80:
                z.coarse_tune = i8(body, 80)
            z.group_index = i32(body, 88)
            z.sample_index = i32(body, 92)
            inst.zones.append(z)

        elif ctype == CHUNK_GROUP:
            g = ExsGroup()
            g.name, g.id = name, cid
            if len(body) >= 8:
                g.volume = i8(body, 0)
                g.pan = i8(body, 1)
                g.polyphony = body[2]
                g.mute = bool(body[3] & 16)
                g.exclusive = body[4]
                g.vel_low = body[5]
                g.vel_high = body[6]
            if len(body) >= 85:
                g.round_robin_pos = i32(body, 80)
                g.enable_by_type = body[84]
            inst.groups.append(g)

        elif ctype == CHUNK_SAMPLE and len(body) >= 336:
            s = ExsSample()
            s.name, s.id = name, cid
            s.length = u32(body, 4)
            s.rate = u32(body, 8)
            s.bit_depth = u32(body, 12)
            s.channels = u32(body, 16)
            s.file_type = body[28:32].decode("ascii", errors="replace")
            s.compressed = u32(body, 36) != 0
            s.directory = _cstr(body[80:336])
            s.file_name = _cstr(body[336:592]) if len(body) >= 592 else name
            if not s.file_name:
                s.file_name = name
            inst.samples.append(s)

        elif ctype == CHUNK_PARAMS:
            try:
                n = u32(body, 0)
                ids = body[4:4 + n]
                vals = body[4 + n:4 + n + 2 * n]
                for i in range(n):
                    if ids[i]:
                        inst.params[ids[i]] = i16(vals, 2 * i)
                off = 4 + n + 2 * n
                if off + 4 <= len(body):
                    n2 = u32(body, off)
                    off += 4
                    if off + 4 * n2 <= len(body):
                        for i in range(n2):
                            pid = u16(body, off + 4 * i)
                            if pid:
                                v = u16(body, off + 4 * i + 2)
                                inst.params[pid] = v - 65536 if v > 32767 else v
            except (struct.error, IndexError):
                pass

        elif ctype == CHUNK_HEADER and name:
            inst.name = name

        pos += 84 + size

    return inst


# ---------------------------------------------------------------------------
# Sample file location
# ---------------------------------------------------------------------------

def _bounded_search(root_dir, file_name, max_depth=6):
    target = file_name.lower()
    depth0 = root_dir.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(root_dir):
        if root.count(os.sep) - depth0 > max_depth:
            dirs[:] = []
            continue
        for f in files:
            if f.lower() == target:
                return os.path.join(root, f)
    return None


def find_sample_file(sample, exs_path):
    """Locate the audio file for an ExsSample, tolerating stale stored paths
    (EXS files routinely carry dead absolute or classic-Mac colon paths)."""
    exs_dir = os.path.dirname(os.path.abspath(exs_path))
    fname = sample.file_name

    candidates = []
    if sample.directory.startswith("/"):
        candidates.append(os.path.join(sample.directory, fname))
    candidates.append(os.path.join(exs_dir, fname))

    # classic-Mac colon path: try trailing components under each ancestor
    parts = [p for p in sample.directory.split(":") if p]
    ancestors = []
    d = exs_dir
    for _ in range(8):
        ancestors.append(d)
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    if parts:
        for anc in ancestors:
            for k in range(len(parts), 0, -1):
                candidates.append(os.path.join(anc, *parts[-k:], fname))
    for c in candidates:
        if os.path.isfile(c):
            return c

    # EXS library layout: <root>/Sampler Instruments/... with samples in
    # sibling folders (EXSamples, ...) — search from the library root
    norm = exs_dir.split(os.sep)
    if "Sampler Instruments" in norm:
        lib_root = os.sep.join(norm[:norm.index("Sampler Instruments")])
        hit = _bounded_search(lib_root, fname)
        if hit:
            return hit
    hit = _bounded_search(exs_dir, fname, max_depth=3)
    if hit:
        return hit

    # last resort: Spotlight; prefer hits sharing the longest prefix with the .exs
    try:
        r = subprocess.run(["mdfind", "-name", fname], capture_output=True,
                           text=True, timeout=15)
        hits = [h for h in r.stdout.splitlines()
                if os.path.basename(h).lower() == fname.lower()
                and os.path.isfile(h)]
        if hits:
            def common(h):
                return len(os.path.commonprefix([h, exs_path]))
            return max(hits, key=common)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


# ---------------------------------------------------------------------------
# WAV reading / writing (with 'smpl' chunk for root note + loop)
# ---------------------------------------------------------------------------

def _read_wav_pcm(path):
    """Return (channels, rate, bits, frames_bytes) for an integer-PCM WAV,
    or None if the file needs afconvert."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    pos, fmt, pcm = 12, None, None
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        csz = struct.unpack_from("<I", data, pos + 4)[0]
        body = data[pos + 8:pos + 8 + csz]
        if cid == b"fmt ":
            fmt = body
        elif cid == b"data":
            pcm = body
        pos += 8 + csz + (csz & 1)
    if fmt is None or pcm is None or len(fmt) < 16:
        return None
    tag, ch, rate, _, _, bits = struct.unpack_from("<HHIIHH", fmt, 0)
    if tag == 0xFFFE and len(fmt) >= 40:
        tag = struct.unpack_from("<H", fmt, 24)[0]
    if tag != 1 or bits not in (16, 24):
        return None
    return ch, rate, bits, pcm


def _read_source_metadata(path):
    """Extract (unity_note, loop) embedded in a source WAV ('smpl' chunk) or
    AIFF ('INST' + 'MARK' chunks). loop is (start, end_exclusive) in frames.
    Returns (None, None) when absent — afconvert drops this metadata, so it
    must be carried over explicitly."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None, None

    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        pos = 12
        while pos + 8 <= len(data):
            cid = data[pos:pos + 4]
            csz = struct.unpack_from("<I", data, pos + 4)[0]
            if cid == b"smpl" and csz >= 36:
                body = data[pos + 8:pos + 8 + csz]
                unity = struct.unpack_from("<I", body, 12)[0]
                nloops = struct.unpack_from("<I", body, 28)[0]
                loop = None
                if nloops and len(body) >= 60:
                    ls, le = struct.unpack_from("<II", body, 44)
                    if le >= ls:
                        loop = (ls, le + 1)  # smpl end is inclusive
                return (unity if unity < 128 else None), loop
            pos += 8 + csz + (csz & 1)
        return None, None

    if data[:4] == b"FORM" and data[8:12] in (b"AIFF", b"AIFC"):
        markers, inst = {}, None
        pos = 12
        while pos + 8 <= len(data):
            cid = data[pos:pos + 4]
            csz = struct.unpack_from(">I", data, pos + 4)[0]
            body = data[pos + 8:pos + 8 + csz]
            if cid == b"MARK" and len(body) >= 2:
                n = struct.unpack_from(">H", body, 0)[0]
                mp = 2
                for _ in range(n):
                    if mp + 6 > len(body):
                        break
                    mid, mpos = struct.unpack_from(">HI", body, mp)
                    markers[mid] = mpos
                    namelen = body[mp + 6]
                    mp += 6 + 1 + namelen + (1 if namelen % 2 == 0 else 0)
            elif cid == b"INST" and len(body) >= 20:
                inst = body
            pos += 8 + csz + (csz & 1)
        if inst is None:
            return None, None
        unity = inst[0] if inst[0] < 128 else None
        play_mode, begin_id, end_id = struct.unpack_from(">HHH", inst, 8)
        loop = None
        if play_mode and begin_id in markers and end_id in markers \
                and markers[end_id] > markers[begin_id]:
            loop = (markers[begin_id], markers[end_id])
        return unity, loop

    return None, None


OUT_BITS = 24
OUT_RATE = 44100


def _afconvert(src, dst):
    r = subprocess.run(["afconvert", "-f", "WAVE",
                        "-d", "LEI%d@%d" % (OUT_BITS, OUT_RATE), src, dst],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("afconvert failed for %s: %s" % (src, r.stderr.strip()))


def write_sample_wav(src_path, dst_path, src_rate=None,
                     root_note=None, loop=None, trim=None, reverse=False):
    """Copy/convert src audio to dst as 24-bit/44.1kHz PCM WAV, embedding a
    'smpl' chunk with unity note and optional (start, end_exclusive) loop.
    trim and loop are in SOURCE-rate frames; they are rescaled here when the
    source needs resampling."""
    pcm_info = _read_wav_pcm(src_path)
    native_rate = pcm_info[1] if pcm_info else (src_rate or OUT_RATE)
    if pcm_info is not None and pcm_info[2] == OUT_BITS and pcm_info[1] == OUT_RATE:
        ch, rate, bits, pcm = pcm_info
    else:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            tmp = tf.name
        try:
            _afconvert(src_path, tmp)
            conv = _read_wav_pcm(tmp)
            if conv is None:
                raise RuntimeError("afconvert produced unreadable WAV for %s" % src_path)
            ch, rate, bits, pcm = conv
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # frame positions from the EXS zone / source metadata are in native-rate
    # frames; rescale them onto the (possibly resampled) output
    factor = rate / native_rate if native_rate else 1.0

    def scale(v):
        return int(round(v * factor))

    if trim:
        trim = (scale(trim[0]), None if trim[1] is None else scale(trim[1]))
    if loop:
        loop = (scale(loop[0]), scale(loop[1]))

    fs = ch * bits // 8
    if trim:
        total = len(pcm) // fs
        st = min(max(trim[0], 0), total)
        en = total if trim[1] is None else min(max(trim[1], st), total)
        pcm = pcm[st * fs:en * fs]
    if reverse:
        n = len(pcm) // fs
        pcm = b"".join(pcm[i * fs:(i + 1) * fs] for i in range(n - 1, -1, -1))
    frames = len(pcm) // fs

    # merge in root note / loop embedded in the source file — the EXS zone
    # (already trim-adjusted by the caller) wins over the source metadata
    src_unity, src_loop = _read_source_metadata(src_path)
    if root_note is None:
        root_note = src_unity
    if loop is None and src_loop and not reverse:
        ls, le = scale(src_loop[0]), scale(src_loop[1])
        if trim:
            ls, le = ls - trim[0], le - trim[0]
        if 0 <= ls < le <= frames:
            loop = (ls, le)
    if loop:
        loop = (min(loop[0], frames), min(loop[1], frames))
        if loop[1] <= loop[0]:
            loop = None

    fmt = struct.pack("<HHIIHH", 1, ch, rate,
                      rate * ch * bits // 8, ch * bits // 8, bits)
    chunks = [(b"fmt ", fmt), (b"data", pcm)]

    if root_note is not None or loop:
        nloops = 1 if loop else 0
        smpl = struct.pack("<IIIIIIIII",
                           0, 0, 1000000000 // max(rate, 1),
                           root_note if root_note is not None else 60,
                           0, 0, 0, nloops, 0)
        if loop:
            start, end = loop
            smpl += struct.pack("<IIIIII", 0, 0, start, max(end - 1, start), 0, 0)
        chunks.append((b"smpl", smpl))

    body = b"WAVE"
    for cid, cdata in chunks:
        body += cid + struct.pack("<I", len(cdata)) + cdata
        if len(cdata) & 1:
            body += b"\x00"
    with open(dst_path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", len(body)) + body)
    return ch, rate, bits, frames, bool(loop)


# ---------------------------------------------------------------------------
# EXS parameter interpretation
# ---------------------------------------------------------------------------
# EXS envelope times are 0..127 knob values on a quartic curve to 10s max;
# Akai knobs are 0..100 perceptual values, so knob maps to knob linearly.
# Filter cutoff/resonance/keytrack are 0..1000 in EXS.
# (Scales per ConvertWithMoss EXS24Detector.)

P_ENV1_ATTACK, P_ENV1_DECAY, P_ENV1_SUSTAIN, P_ENV1_RELEASE = 0x52, 0x54, 0x51, 0x55
P_ENV2_ATTACK, P_ENV2_DECAY, P_ENV2_SUSTAIN, P_ENV2_RELEASE = 0x4C, 0x4E, 0x4F, 0x50
P_ENV1_VEL_SENS = 0x5A
P_FILT_ON, P_FILT_TYPE, P_FILT_CUTOFF, P_FILT_RESO, P_FILT_KEYTRACK = \
    0x2C, 0xF3, 0x1E, 0x1D, 0x2E
MOD_ROW_BASES = [0xAD + 6 * r for r in range(10)] + [0x19B]
MOD_SRC_ENV2, MOD_DST_FILTER_CUTOFF = -14, 8

# EXS filter types -> AKP filter modes
FILTER_TYPE_MAP = {0: 1, 1: 1,   # LP 24/18 dB -> 4-POLE LP
                   2: 0, 3: 0,   # LP 12/6 dB  -> 2-POLE LP
                   4: 7,         # HP 12 dB    -> 2-POLE HP
                   5: 3}         # BP 12 dB    -> 2-POLE BP


def _knob(v):
    return max(0, min(100, int(round(v * 100 / 127))))


def _env_knobs(P, ids, defaults):
    """(attack, decay, sustain, release) as AKP 0..100 knob values."""
    return tuple(_knob(P[i]) if i in P else d for i, d in zip(ids, defaults))


def derive_settings(inst):
    """Program-wide AKP settings derived from the EXS parameter block."""
    P = inst.params
    s = {}
    s["amp_env"] = _env_knobs(
        P, (P_ENV1_ATTACK, P_ENV1_DECAY, P_ENV1_SUSTAIN, P_ENV1_RELEASE),
        (0, 50, 100, 15))
    vs = P.get(P_ENV1_VEL_SENS)
    s["vel_sens"] = _clamp(vs / -60 * 100, 0, 100) if vs is not None else 25

    if P.get(P_FILT_ON, 0) > 0:
        s["filt_mode"] = FILTER_TYPE_MAP.get(P.get(P_FILT_TYPE, 0), 0)
        s["filt_cutoff"] = _clamp(P.get(P_FILT_CUTOFF, 1000) / 10, 0, 100)
        s["filt_reso"] = _clamp(P.get(P_FILT_RESO, 0) * 12 / 1000, 0, 12)
        s["filt_keytrack"] = _clamp(P.get(P_FILT_KEYTRACK, 0) * 12 / 1000, -36, 36)
        s["filt_env"] = _env_knobs(
            P, (P_ENV2_ATTACK, P_ENV2_DECAY, P_ENV2_SUSTAIN, P_ENV2_RELEASE),
            (0, 50, 100, 15))
        depth = None
        for base in MOD_ROW_BASES:
            if P.get(base + 1) == MOD_SRC_ENV2 and P.get(base) == MOD_DST_FILTER_CUTOFF:
                depth = _clamp(P.get(base + 3, 1000) / 10, -100, 100)
                break
        if depth is None:
            has_env2 = any(i in P for i in
                           (P_ENV2_ATTACK, P_ENV2_DECAY, P_ENV2_SUSTAIN, P_ENV2_RELEASE))
            depth = 100 if has_env2 else 0
        s["filt_env_depth"] = depth
    else:
        s["filt_mode"], s["filt_cutoff"], s["filt_reso"] = 0, 100, 0
        s["filt_keytrack"], s["filt_env_depth"] = 0, 0
        s["filt_env"] = (0, 50, 100, 15)
    return s


DEFAULT_SETTINGS = {"amp_env": (0, 50, 100, 15), "vel_sens": 25,
                    "filt_mode": 0, "filt_cutoff": 100, "filt_reso": 0,
                    "filt_keytrack": 0, "filt_env": (0, 50, 100, 15),
                    "filt_env_depth": 0}


# ---------------------------------------------------------------------------
# AKP writer (S5000/S6000 dialect — loads on Z4/Z8/MPC4000 as well)
# ---------------------------------------------------------------------------

def _chunk(cid, data):
    assert len(data) % 2 == 0, "odd AKP chunk size"
    return cid + struct.pack("<I", len(data)) + data


def _clamp(v, lo, hi):
    return max(lo, min(hi, int(round(v))))


class AkpZone:
    """One velocity zone: a sample name (<=20 chars) + ranges/tweaks."""

    def __init__(self, sample_name="", vel_low=0, vel_high=127, fine=0, semi=0,
                 pan=0, playback=4, level=0, kbd_track=True):
        self.sample_name = sample_name
        self.vel_low = vel_low
        self.vel_high = vel_high
        self.fine = fine
        self.semi = semi
        self.pan = pan
        self.playback = playback
        self.level = level
        self.kbd_track = kbd_track

    def pack(self):
        name = self.sample_name.encode("ascii", errors="replace")[:20]
        d = bytearray(46)
        d[0] = 0x01
        d[1] = len(name)
        d[2:2 + len(name)] = name
        d[34] = _clamp(self.vel_low, 0, 127)
        d[35] = _clamp(self.vel_high, 0, 127)
        struct.pack_into("b", d, 36, _clamp(self.fine, -50, 50))
        struct.pack_into("b", d, 37, _clamp(self.semi, -36, 36))
        struct.pack_into("b", d, 39, _clamp(self.pan, -50, 50))
        d[40] = self.playback
        struct.pack_into("b", d, 42, _clamp(self.level, -100, 100))
        d[43] = 1 if self.kbd_track else 0
        return _chunk(b"zone", bytes(d))


class AkpKeygroup:
    def __init__(self, low_note, high_note, zones, settings=None, mute_group=0):
        self.low_note = low_note
        self.high_note = high_note
        self.zones = zones  # list of AkpZone, max 4
        self.settings = settings or DEFAULT_SETTINGS
        self.mute_group = mute_group

    def pack(self):
        s = self.settings
        kloc = bytearray(b"\x01\x03\x01\x04" + b"\x00" * 12)
        kloc[4] = _clamp(self.low_note, 21, 127)
        kloc[5] = _clamp(self.high_note, 21, 127)
        kloc[10] = 100  # Pitch Mod 1 = +100 so pitchbend works
        kloc[14] = _clamp(self.mute_group, 0, 64)

        def env(adsr, depth=0):
            a, d, sus, r = adsr
            e = bytearray([1, a, 0, d, r, 0, 0, sus] + [0] * 10)
            struct.pack_into("b", e, 9, depth)
            return bytes(e)

        amp_env = env(s["amp_env"])
        filt_env = env(s["filt_env"], _clamp(s["filt_env_depth"], -100, 100))
        aux_env = bytes([1, 0, 0x32, 0x32, 0x0F, 0x64, 0x64, 0x64, 0] +
                        [0] * 8 + [0x85])
        filt = bytearray([1, s["filt_mode"], _clamp(s["filt_cutoff"], 0, 100),
                          _clamp(s["filt_reso"], 0, 12), 0, 0, 0, 0, 0, 0])
        struct.pack_into("b", filt, 4, _clamp(s["filt_keytrack"], -36, 36))
        filt = bytes(filt)

        zones = list(self.zones)[:4]
        while len(zones) < 4:
            zones.append(AkpZone())

        body = (_chunk(b"kloc", bytes(kloc)) +
                _chunk(b"env ", amp_env) +
                _chunk(b"env ", filt_env) +
                _chunk(b"env ", aux_env) +
                _chunk(b"filt", filt) +
                b"".join(z.pack() for z in zones))
        assert len(body) == 336, len(body)
        return _chunk(b"kgrp", body)


def write_akp(path, keygroups, settings=None):
    if not 1 <= len(keygroups) <= 99:
        raise ValueError("AKP needs 1..99 keygroups, got %d" % len(keygroups))
    s = settings or DEFAULT_SETTINGS
    prg = bytes([0x01, 0x00, len(keygroups), 0x00, 0x02, 0x00])
    out = bytearray([0x01, 0x55, 0, 0, 0, 0, 0, 0])
    struct.pack_into("b", out, 7, _clamp(s["vel_sens"], -100, 100))
    out = bytes(out)
    tune = bytes([0x01] + [0] * 14 + [0x02, 0x02] + [0] * 5)
    lfo1 = bytes([0x01, 0x01, 0x2B, 0, 0, 0, 0x01, 0x0F, 0, 0, 0, 0])
    lfo2 = bytes([0x01, 0, 0, 0, 0, 0x01, 0, 0, 0, 0, 0, 0])
    mods = bytes([0x01, 0x00, 0x11, 0x00, 0x02, 0x06, 0x02, 0x03, 0x01, 0x08,
                  0x01, 0x06, 0x01, 0x01, 0x04, 0x06, 0x05, 0x06, 0x03, 0x06,
                  0x07, 0x00, 0x08, 0x00, 0x06, 0x00, 0x00, 0x07, 0x00, 0x0B,
                  0x02, 0x05, 0x09, 0x05, 0x09, 0x08, 0x09, 0x09])

    body = (b"APRG" +
            _chunk(b"prg ", prg) + _chunk(b"out ", out) + _chunk(b"tune", tune) +
            _chunk(b"lfo ", lfo1) + _chunk(b"lfo ", lfo2) + _chunk(b"mods", mods) +
            b"".join(k.pack() for k in keygroups))

    # S5000/S6000 dialect: RIFF size field written as zero (sampler ignores it)
    data = b"RIFF" + struct.pack("<I", 0) + body
    expected = 158 + 344 * len(keygroups)
    assert len(data) == expected, (len(data), expected)
    with open(path, "wb") as f:
        f.write(data)


# ---------------------------------------------------------------------------
# EXS -> AKP conversion
# ---------------------------------------------------------------------------

def sanitize_sample_name(stem, used):
    name = re.sub(r"[^A-Za-z0-9 _#+\-.]", "_", stem).strip()[:20].strip()
    if not name:
        name = "sample"
    base, n = name, 1
    while name.lower() in used:
        n += 1
        suffix = "~%d" % n
        name = base[:20 - len(suffix)] + suffix
    used.add(name.lower())
    return name


def convert_instrument(exs_path, out_root, warnings):
    inst = read_exs(exs_path)
    stem = os.path.splitext(os.path.basename(exs_path))[0]
    prog_name = re.sub(r'[\\/:*?"<>|]', "_", stem).strip() or "program"
    out_dir = os.path.join(out_root, prog_name)
    os.makedirs(out_dir, exist_ok=True)
    settings = derive_settings(inst)

    def group_of(z):
        if 0 <= z.group_index < len(inst.groups):
            return inst.groups[z.group_index]
        return None

    # --- select zones: drop muted groups, gate velocity by group ----------
    selected = []
    for z in inst.zones:
        g = group_of(z)
        if g and g.mute:
            warnings.append("zone %r skipped (group %r is muted)" % (z.name, g.name))
            continue
        vel_lo, vel_hi = z.vel_low, z.vel_high
        if vel_hi == 0 or vel_lo > vel_hi:
            vel_lo, vel_hi = 0, 127
        if g and (g.vel_low, g.vel_high) != (0, 127) and g.vel_low <= g.vel_high:
            vel_lo, vel_hi = max(vel_lo, g.vel_low), min(vel_hi, g.vel_high)
            if vel_lo > vel_hi:
                warnings.append("zone %r skipped (empty velocity range after "
                                "group gating)" % z.name)
                continue
        selected.append((z, g, vel_lo, vel_hi))

    # --- round robin: AKP has no equivalent, keep the first round only ----
    by_range = {}
    for item in selected:
        z = item[0]
        by_range.setdefault((z.key_low, z.key_high), []).append(item)

    for rng, items in by_range.items():
        rr_pos = {i[1].round_robin_pos for i in items
                  if i[1] and i[1].enable_by_type == 2 and i[1].round_robin_pos >= 0}
        if len(rr_pos) > 1:
            keep = min(rr_pos)
            kept = [i for i in items
                    if not (i[1] and i[1].enable_by_type == 2
                            and i[1].round_robin_pos >= 0
                            and i[1].round_robin_pos != keep)]
            warnings.append("key range %s..%s: kept 1 of %d round-robin rounds "
                            "(no AKP equivalent)" % (note_name(rng[0]),
                                                     note_name(rng[1]), len(rr_pos)))
            by_range[rng] = kept

    # --- export samples: one WAV per distinct (sample, trim, reverse, ...) -
    used_names = set()
    exports = {}      # export key -> akp sample name
    src_cache = {}    # sample_index -> source path or None

    def export_for(z):
        idx = z.sample_index
        if not (0 <= idx < len(inst.samples)):
            return None
        s = inst.samples[idx]
        if idx not in src_cache:
            src_cache[idx] = find_sample_file(s, exs_path)
            if src_cache[idx] is None:
                warnings.append("sample not found on disk: %r (dir %r)"
                                % (s.file_name, s.directory))
        src = src_cache[idx]
        if src is None:
            return None

        start = z.sample_start
        end = z.sample_end if z.sample_end > start else None
        trimmed = start > 0 or (end is not None and end < s.length)
        trim = (start, end) if trimmed else None

        loop = None
        if z.loop_on and z.loop_end > z.loop_start:
            ls, le = z.loop_start, z.loop_end
            if trimmed:
                ls, le = ls - start, le - start
            if z.reverse:
                warnings.append("zone %r: loop dropped (reversed sample)" % z.name)
            elif ls < 0:
                warnings.append("zone %r: loop outside trimmed region, dropped"
                                % z.name)
            else:
                loop = (ls, le)

        root = z.root_note if z.pitch_tracking else None
        key = (idx, trim, z.reverse, root, loop)
        if key in exports:
            return exports[key]

        akp_name = sanitize_sample_name(os.path.splitext(s.file_name)[0], used_names)
        dst = os.path.join(out_dir, akp_name + ".wav")
        try:
            ch, rate, bits, frames, has_loop = write_sample_wav(
                src, dst, src_rate=s.rate,
                root_note=root, loop=loop, trim=trim, reverse=z.reverse)
        except (RuntimeError, OSError) as e:
            warnings.append(str(e))
            exports[key] = None
            return None
        exports[key] = akp_name
        print("  wav  %-24s %dHz/%dbit/%dch %d frames%s%s%s" %
              (akp_name + ".wav", rate, bits, ch, frames,
               " loop" if has_loop else "", " trim" if trim else "",
               " rev" if z.reverse else ""))
        return akp_name

    # --- build keygroups --------------------------------------------------
    keygroups = []
    for (lo, hi), items in sorted(by_range.items()):
        akp_zones = []
        mute_group = 0
        for z, g, vel_lo, vel_hi in sorted(items, key=lambda i: (i[2], i[3])):
            akp_name = export_for(z)
            if akp_name is None:
                continue
            if g and g.exclusive > 0 and mute_group == 0:
                mute_group = g.exclusive
            playback = 1 if z.one_shot else 4  # ONE SHOT / AS SAMPLE (obey WAV loop)
            akp_zones.append(AkpZone(
                sample_name=akp_name,
                vel_low=vel_lo, vel_high=vel_hi,
                fine=z.fine_tune, semi=z.coarse_tune,
                pan=_clamp(z.pan + (g.pan if g else 0), -50, 50),
                playback=playback,
                level=_clamp(z.volume + (g.volume if g else 0), -100, 100),
                kbd_track=z.pitch_tracking))
        if not akp_zones:
            continue
        if lo < 21:
            warnings.append("key range %d..%d clamped to A0 (AKP low limit is 21)"
                            % (lo, hi))
        for i in range(0, len(akp_zones), 4):
            group = akp_zones[i:i + 4]
            if i > 0:
                warnings.append("range %d..%d has >4 velocity layers — "
                                "split into overlapping keygroups" % (lo, hi))
            keygroups.append(AkpKeygroup(lo, hi, group, settings, mute_group))

    if not keygroups:
        raise RuntimeError("no usable zones (all samples missing?)")

    akp_path = os.path.join(out_dir, prog_name + ".akp")
    write_akp(akp_path, keygroups, settings)
    print("  akp  %s  (%d keygroups)" % (os.path.basename(akp_path), len(keygroups)))
    return akp_path


# ---------------------------------------------------------------------------
# AKP checker (independent re-parse of a .akp against the spec)
# ---------------------------------------------------------------------------

AKP_SIZES = {b"prg ": (6,), b"out ": (8,), b"tune": (22, 24), b"lfo ": (12, 14),
             b"mods": (38,), b"kgrp": (336, 344), b"kloc": (16,),
             b"env ": (18,), b"filt": (10,), b"zone": (46, 48)}

AKP_PLAYBACK = {0: "no-loop", 1: "one-shot", 2: "loop-in-rel",
                3: "loop-until-rel", 4: "as-sample"}


def _walk_riff(data, pos, end):
    while pos + 8 <= end:
        cid = data[pos:pos + 4]
        csz = struct.unpack_from("<I", data, pos + 4)[0]
        yield cid, data[pos + 8:pos + 8 + csz]
        pos += 8 + csz + (csz & 1)


def _read_smpl(wav_path):
    try:
        with open(wav_path, "rb") as f:
            data = f.read()
        if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            return None
        for cid, body in _walk_riff(data, 12, len(data)):
            if cid == b"smpl" and len(body) >= 36:
                unity, nloops = struct.unpack_from("<I", body, 12)[0], \
                    struct.unpack_from("<I", body, 28)[0]
                loop = None
                if nloops and len(body) >= 60:
                    loop = struct.unpack_from("<II", body, 44)
                return unity, loop
    except OSError:
        return None
    return None


def cmd_check(path):
    with open(path, "rb") as f:
        data = f.read()
    problems = []
    if data[:4] != b"RIFF" or data[8:12] != b"APRG":
        print("%s: not an AKP (RIFF/APRG) file" % path)
        return 1
    chunks = list(_walk_riff(data, 12, len(data)))
    order = [c[0] for c in chunks]
    if order[:6] != [b"prg ", b"out ", b"tune", b"lfo ", b"lfo ", b"mods"] or \
            any(c != b"kgrp" for c in order[6:]):
        problems.append("unexpected chunk order: %s" %
                        [c.decode("ascii", "replace") for c in order])
    n_kgrp = order.count(b"kgrp")
    print("%s: %d keygroups" % (os.path.basename(path), n_kgrp))

    for cid, body in chunks:
        if len(body) not in AKP_SIZES.get(cid, (len(body),)):
            problems.append("chunk %r has size %d, expected %s"
                            % (cid, len(body), AKP_SIZES[cid]))

    prg = chunks[0][1]
    if prg[2] != n_kgrp:
        problems.append("prg keygroup count %d != %d kgrp chunks" % (prg[2], n_kgrp))

    akp_dir = os.path.dirname(os.path.abspath(path))
    for ki, (cid, body) in enumerate(c for c in chunks if c[0] == b"kgrp"):
        subs = list(_walk_riff(body, 0, len(body)))
        sub_order = [s[0] for s in subs]
        if sub_order != [b"kloc", b"env ", b"env ", b"env ", b"filt"] + [b"zone"] * 4:
            problems.append("kgrp %d bad subchunk order: %s" % (ki, sub_order))
            continue
        for scid, sbody in subs:
            if len(sbody) not in AKP_SIZES[scid]:
                problems.append("kgrp %d chunk %r size %d, expected %s"
                                % (ki, scid, len(sbody), AKP_SIZES[scid]))
        kloc = subs[0][1]
        line = "  kgrp %-2d %s..%s" % (ki, note_name(kloc[4]), note_name(kloc[5]))
        for zcid, z in subs[5:]:
            nlen = z[1]
            if nlen == 0:
                continue
            name = z[2:2 + min(nlen, 20)].decode("ascii", "replace")
            wav = os.path.join(akp_dir, name + ".wav")
            smpl = _read_smpl(wav) if os.path.isfile(wav) else None
            if not os.path.isfile(wav):
                problems.append("kgrp %d references missing WAV %r" % (ki, name + ".wav"))
                wavinfo = " [WAV MISSING]"
            elif smpl:
                unity, loop = smpl
                wavinfo = " root=%s%s" % (note_name(unity),
                                          " loop=%d..%d" % loop if loop else "")
            else:
                wavinfo = ""
            line += "  | %-20s vel %3d..%-3d %s%s" % (
                name, z[34], z[35], AKP_PLAYBACK.get(z[40], "?%d" % z[40]), wavinfo)
        print(line)

    if problems:
        print("PROBLEMS:")
        for p in problems:
            print("  " + p)
        return 1
    print("  structure OK (%d bytes)" % len(data))
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def note_name(n):
    names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    return "%s%d" % (names[n % 12], n // 12 - 2)


def cmd_dump(path):
    inst = read_exs(path)
    print("Instrument: %s" % inst.name)
    print("  zones=%d groups=%d samples=%d params=%d" % (
        len(inst.zones), len(inst.groups), len(inst.samples), len(inst.params)))
    for g in inst.groups:
        rr = " rr_pos=%d" % g.round_robin_pos if g.enable_by_type == 2 else ""
        print("  group %-3d %-24s vol=%-4d vel=%d..%d%s" % (
            g.id, repr(g.name), g.volume, g.vel_low, g.vel_high, rr))
    for z in inst.zones:
        print("  zone %-3d %-24s key=%s..%s root=%s vel=%d..%d vol=%-3d pan=%-3d "
              "semi=%+d fine=%+d smp=%d grp=%d%s%s" % (
                  z.id, repr(z.name), note_name(z.key_low), note_name(z.key_high),
                  note_name(z.root_note), z.vel_low, z.vel_high, z.volume, z.pan,
                  z.coarse_tune, z.fine_tune, z.sample_index, z.group_index,
                  " loop" if z.loop_on else "",
                  " oneshot" if z.one_shot else ""))
    for s in inst.samples:
        print("  sample %-3d %-28s %dHz/%dbit/%dch %s%s dir=%s" % (
            s.id, repr(s.file_name), s.rate, s.bit_depth, s.channels,
            s.file_type.strip(), " COMPRESSED" if s.compressed else "",
            repr(s.directory)))


def main(argv):
    args = argv[1:]
    if not args or args[0] not in ("dump", "convert", "check"):
        print(__doc__)
        return 2
    cmd, args = args[0], args[1:]

    if cmd == "dump":
        for p in args:
            cmd_dump(p)
        return 0

    if cmd == "check":
        return max(cmd_check(p) for p in args) if args else 2

    out_root, files = "./akp-out", []
    i = 0
    while i < len(args):
        if args[i] == "-o":
            out_root = args[i + 1]
            i += 2
        elif args[i] == "--bits":
            print("--bits was removed: output is always 24-bit/44.1kHz WAV")
            return 2
        else:
            files.append(args[i])
            i += 1
    if not files:
        print("no input files")
        return 2

    all_warnings = []
    for p in files:
        print("converting %s" % os.path.basename(p))
        warns = []
        try:
            convert_instrument(p, out_root, warns)
        except (RuntimeError, ValueError, OSError) as e:
            warns.append("FAILED: %s" % e)
            print("  FAILED: %s" % e)
        all_warnings += [(p, w) for w in warns]
    if all_warnings:
        print("\nwarnings:")
        for p, w in all_warnings:
            print("  [%s] %s" % (os.path.basename(p), w))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
