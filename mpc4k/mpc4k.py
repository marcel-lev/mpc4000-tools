#!/usr/bin/env python3
"""mpc4k — file manager for the Akai MPC 4000 (and Z4/Z8) internal drive
over USB, speaking the ak.sys protocol (as reverse-engineered by Aksy).

Usage:
  mpc4k.py disks                       List disks
  mpc4k.py ls [PATH]                   List folder (default: root)
  mpc4k.py mkdir PATH                  Create folder
  mpc4k.py rm PATH [PATH...]          Delete file or folder (recursive!)
  mpc4k.py mv OLD NEW                  Rename/move file or folder
  mpc4k.py put LOCAL... REMOTE_DIR     Upload files into a folder
  mpc4k.py get REMOTE [LOCALDIR]       Download a file
  mpc4k.py info                        Connection info
  mpc4k.py mem                         List RAM contents (samples/programs/…)
  mpc4k.py memget NAME [LOCALDIR]      Download a RAM item to the Mac
  mpc4k.py memsave FOLDER [NAME]       Save RAM (all, or one item) to a folder
                                       on the MPC disk

Remote paths use '/' as separator (converted to the sampler's '\\').
Options: --disk HANDLE selects a disk (default: first writable hard disk).
"""

import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import usbio

VENDOR_AKAI = 0x09E8
PRODUCT_MPC4000 = 0x0061
PRODUCT_Z48 = 0x005F

BUSY = b"AkaI"  # 41 6B 61 49
REPLY_OK, REPLY_DONE, REPLY_REPLY, REPLY_ERROR = 0x4F, 0x44, 0x52, 0x45

ERRORS = {
    0x00: "unknown/unsupported command", 0x01: "checksum invalid",
    0x02: "unknown error", 0x03: "invalid message format",
    0x04: "parameter out of range", 0x101: "item not found",
    0x102: "invalid item handle", 0x103: "invalid item name",
    0x181: "no disks", 0x182: "disk unformatted", 0x185: "folder not empty",
    0x187: "disk write-protected", 0x188: "invalid disk handle",
    0x189: "disk full", 0x18A: "disk abort",
    0x203: "file not found", 0x204: "file already exists",
}

# RAM item kinds: sysex section, memory-GET bulk command, file extension,
# item type byte for save-to-disk (10 2C / 20 2D)
MEM_KINDS = {
    "Samples":  {"section": 0x1C, "cmd": 0x21, "ext": ".wav", "type": 3},
    "Programs": {"section": 0x14, "cmd": 0x22, "ext": ".akp", "type": 2},
    "Multis":   {"section": 0x18, "cmd": 0x23, "ext": ".akm", "type": 1},
    "Songs":    {"section": 0x28, "cmd": 0x24, "ext": ".mid", "type": 4},
}

DISK_TYPES = {0: "floppy", 1: "hard disk", 2: "CD-ROM", 3: "removable"}
DISK_FORMATS = {0: "other", 1: "MSDOS", 2: "FAT32", 3: "ISO9660", 4: "S1000",
                5: "S3000", 6: "EMU", 7: "ROLAND", 8: "CD-AUDIO", 100: "empty"}


class MpcError(RuntimeError):
    pass


def _pclamp(v, lo, hi):
    return max(lo, min(hi, int(round(float(v)))))


def word7(v):
    return bytes([v & 0x7F, (v >> 7) & 0x7F])


def dword7(b, o=0):
    return b[o] | (b[o + 1] << 7) | (b[o + 2] << 14) | (b[o + 3] << 21)


def enc_dword(v):
    return bytes([(v >> s) & 0x7F for s in (0, 7, 14, 21)])


def enc_qword(v):
    return bytes([(v >> s) & 0x7F for s in (0, 7, 14, 21, 28, 35, 42, 49)])


def enc_sword(v):
    sign = 1 if v < 0 else 0
    v = abs(int(v))
    return bytes([sign, v & 0x7F, (v >> 7) & 0x7F])


def enc_sbyte(v):
    return bytes([1 if v < 0 else 0, abs(int(v)) & 0x7F])


# typed reply parsing: values prefixed with a type-id byte; replies may carry
# leading echo bytes before the first typed value, so try successive offsets
_TYPE_SIZES = {0x00: 1, 0x01: 2, 0x02: 2, 0x03: 3, 0x04: 4, 0x05: 5,
               0x06: 8, 0x07: 9, 0x09: 2, 0x0A: 3, 0x0B: 4}


def _parse_typed_at(data, start):
    vals, p = [], start
    while p < len(data):
        t = data[p]
        p += 1
        if t == 0x08:  # string
            if 0 not in data[p:]:
                return None
            end = data.index(0, p)
            vals.append(data[p:end].decode("ascii", "replace"))
            p = end + 1
        elif t in _TYPE_SIZES:
            n = _TYPE_SIZES[t]
            if p + n > len(data):
                return None
            b = data[p:p + n]
            if t == 0x00:
                vals.append(b[0])
            elif t == 0x01:
                vals.append(-b[1] if b[0] else b[1])
            elif t == 0x02:
                vals.append(b[0] | (b[1] << 7))
            elif t == 0x03:
                m = b[1] | (b[2] << 7)
                vals.append(-m if b[0] else m)
            elif t == 0x04:
                vals.append(dword7(b))
            elif t == 0x05:
                m = dword7(b, 1)
                vals.append(-m if b[0] else m)
            elif t == 0x06:
                vals.append(sum(b[i] << (7 * i) for i in range(8)))
            elif t == 0x07:
                m = sum(b[1 + i] << (7 * i) for i in range(8))
                vals.append(-m if b[0] else m)
            else:
                vals.append(bytes(b))
            p += n
        else:
            return None
    return vals


def parse_typed(data):
    """Parse a typed reply payload, tolerating leading echo bytes."""
    for start in range(min(4, len(data) + 1)):
        vals = _parse_typed_at(data, start)
        if vals is not None:
            return vals
    raise MpcError("unparseable typed reply: %s" % data.hex(" "))


class Mpc4000:
    def __init__(self, product_ids=(PRODUCT_MPC4000, PRODUCT_Z48)):
        self.dev = None
        for pid in product_ids:
            try:
                self.dev = usbio.UsbDevice(VENDOR_AKAI, pid)
                self.product_id = pid
                break
            except RuntimeError:
                continue
        if self.dev is None:
            raise MpcError("no Akai MPC4000/Z4/Z8 found on USB — "
                           "is it connected and powered on?")
        self._next_cmd_at = 0.0
        self.dev.write(b"\x03\x01", timeout_ms=1000)  # ak.sys init handshake
        time.sleep(0.2)
        # silence unsolicited notifications / front-panel item sync
        self.sysex(0x00, 0x01, b"\x00")
        self.sysex(0x00, 0x03, b"\x00")

    def close(self):
        if self.dev:
            self.dev.close()
            self.dev = None

    # -- sysex ------------------------------------------------------------

    # minimum spacing between commands — the sampler firmware can hang if
    # commands are fired back-to-back at USB speed (observed on hardware)
    GAP_FAST = 0.03
    GAP_SLOW = 0.09

    def _pace(self, gap):
        now = time.time()
        wait = self._next_cmd_at - now
        if wait > 0:
            time.sleep(wait)
        self._next_cmd_at = max(now, self._next_cmd_at) + gap

    def _send_sysex(self, msg):
        self.dev.write(bytes([0x10, len(msg) & 0xFF, len(msg) >> 8]) + msg)

    def _read_reply(self, overall_timeout=60.0):
        """Read until a non-OK sysex reply arrives; swallow busy markers and
        OK acknowledgements (slow disk ops stream those first)."""
        buf = b""
        deadline = time.time() + overall_timeout
        while True:
            if time.time() > deadline:
                raise MpcError("timeout waiting for sampler reply")
            try:
                chunk = self.dev.read(16384, timeout_ms=5000)
            except TimeoutError:
                continue
            buf += chunk
            while True:
                buf = buf.lstrip(b"\x00")
                if buf.startswith(BUSY):
                    buf = buf[4:]
                    continue
                if not buf.startswith(b"\xF0"):
                    if buf:
                        buf = buf[1:]
                        continue
                    break
                end = buf.find(b"\xF7")
                if end < 0:
                    break  # incomplete, read more
                msg, buf = buf[:end + 1], buf[end + 1:]
                if len(msg) < 5:
                    continue  # F0 F7 keep-alive
                parsed = self._parse_reply(msg)
                if parsed is not None:
                    return parsed

    def _parse_reply(self, msg):
        # F0 47 <devid> <userref...> <reply_id> <sect> <item> <data...> F7
        p = 3
        p += 1 + (msg[p] >> 4)  # skip userref
        reply_id = msg[p]
        data = msg[p + 3:-1]
        if reply_id == REPLY_OK:
            return None  # ack of a slow command; real reply follows
        if reply_id == REPLY_ERROR:
            code = data[0] | (data[1] << 7) if len(data) >= 2 else -1
            raise MpcError("sampler error: %s (0x%x)"
                           % (ERRORS.get(code, "unknown"), code))
        return reply_id, data

    def sysex(self, section, item, args=b"", timeout=60.0):
        """Fast command set (device id 0x5F, userref 00)."""
        self._pace(self.GAP_FAST)
        msg = bytes([0xF0, 0x47, 0x5F, 0x00, section, item]) + args + b"\xF7"
        self._send_sysex(msg)
        return self._read_reply(timeout)

    def sysex_slow(self, item, args=b"", timeout=120.0):
        """S56K-compatible command set (device id 0x5E, section 0x10,
        2-byte userref) — required for disk-touching commands."""
        self._pace(self.GAP_SLOW)
        msg = bytes([0xF0, 0x47, 0x5E, 0x20, 0x00, 0x00, 0x10, item]) + args + b"\xF7"
        self._send_sysex(msg)
        return self._read_reply(timeout)

    # -- disk / folder operations -----------------------------------------

    def get_disklist(self):
        _, data = self.sysex_slow(0x05)
        disks, p = [], 0
        while p + 6 < len(data):
            handle = data[p] | (data[p + 1] << 7)
            dtype, fmt, scsi, writable = data[p + 2:p + 6]
            p += 6
            end = data.index(0, p)
            name = data[p:end].decode("ascii", "replace")
            p = end + 1
            disks.append({"handle": handle, "type": dtype, "format": fmt,
                          "scsi": scsi, "writable": bool(writable), "name": name})
        return disks

    def update_disklist(self):
        self.sysex_slow(0x01)

    def select_disk(self, handle):
        self.sysex_slow(0x02, word7(handle))

    def open_folder(self, path=""):
        """Select current folder. '' = root; path uses backslashes."""
        self.sysex(0x20, 0x13, b"\x00")  # root
        if path:
            self.sysex(0x20, 0x13, path.encode("ascii") + b"\x00")

    def get_curr_path(self):
        _, data = self.sysex(0x20, 0x09)
        return data[1:data.index(0, 1)].decode("ascii", "replace")

    def get_folder_names(self):
        _, data = self.sysex_slow(0x12)
        names, p = [], 0
        while p < len(data):
            end = data.index(0, p) if 0 in data[p:] else len(data)
            if end > p:
                names.append(data[p:end].decode("ascii", "replace"))
            p = end + 1
        return names

    def get_filenames(self):
        """[(name, size_bytes)] of the current folder."""
        _, data = self.sysex_slow(0x22)
        out, p = [], 0
        while p < len(data):
            if 0 not in data[p:]:
                break
            end = data.index(0, p)
            name = data[p:end].decode("ascii", "replace")
            p = end + 1
            if p + 4 > len(data):
                break
            size = dword7(data, p)
            p += 4
            if name:
                out.append((name, size))
        return out

    def create_folder(self, name):
        self.sysex(0x20, 0x16, name.encode("ascii") + b"\x00")

    def rename_folder(self, old, new):
        self.sysex(0x20, 0x18,
                   old.encode("ascii") + b"\x00" + new.encode("ascii") + b"\x00")

    def rename_file(self, old, new):
        self.sysex_slow(0x28,
                        old.encode("ascii") + b"\x00" + new.encode("ascii") + b"\x00")

    def delete(self, name):
        """Delete file or folder (recursive) in the current folder."""
        self.sysex_slow(0x29, name.encode("ascii") + b"\x00")

    def download_folder(self, remote_path, local_root, progress=None,
                        item_cb=None, max_depth=10):
        """Recursively download a remote folder into local_root. Resumable:
        files that already exist locally with the matching size are skipped,
        so re-running an interrupted backup only fills the gaps. Individual
        file failures are retried once (with endpoint-stall recovery) and
        collected instead of aborting the rest of the run."""
        plan = []  # (relative_dir, [(name, size), ...])

        def walk(rel, depth):
            if depth > max_depth:
                return
            full = remote_path + ("\\" + rel if rel else "")
            self.open_folder(full)
            folders = self.get_folder_names()
            files = self.get_filenames()
            plan.append((rel, files))
            for f in folders:
                walk(rel + "\\" + f if rel else f, depth + 1)

        walk("", 0)
        total = sum(len(files) for _, files in plan)
        i = downloaded = skipped = 0
        failed = []
        for rel, files in plan:
            ldir = os.path.join(local_root, *rel.split("\\")) if rel \
                else local_root
            os.makedirs(ldir, exist_ok=True)
            opened = False
            for name, size in files:
                i += 1
                if item_cb:
                    item_cb(name, i, total)
                lpath = os.path.join(ldir, name)
                if size > 0 and os.path.isfile(lpath) \
                        and os.path.getsize(lpath) == size:
                    skipped += 1
                    continue
                if not opened:
                    self.open_folder(remote_path +
                                     ("\\" + rel if rel else ""))
                    opened = True
                ok = False
                for attempt in (1, 2):
                    try:
                        self.get_file(name, lpath, progress)
                        ok = True
                        break
                    except (MpcError, usbio.UsbError, TimeoutError):
                        if attempt == 1:
                            try:
                                self.dev.clear_halt()
                            except Exception:
                                pass
                            time.sleep(1.5)
                if ok:
                    downloaded += 1
                else:
                    failed.append((rel + "\\" + name) if rel else name)
                    if os.path.isfile(lpath):
                        os.unlink(lpath)  # never leave truncated files
                time.sleep(0.5)  # cooldown: sustained transfer bursts can
                                 # hang the sampler firmware
        return {"files": total, "downloaded": downloaded,
                "skipped": skipped, "failed": failed}

    def load_file(self, name, with_deps=True):
        """Load a file from the current disk folder into RAM. with_deps loads
        a program's samples along with it (like LOAD on the front panel)."""
        if with_deps:
            self.sysex(0x20, 0x2B, name.encode("ascii") + b"\x00", timeout=600.0)
        else:
            self.sysex_slow(0x2A, name.encode("ascii") + b"\x00", timeout=600.0)

    # -- memory (RAM) operations -------------------------------------------

    def get_memory_names(self, section):
        """Names of items loaded in RAM for one section (see MEM_KINDS).
        Uses the handles+names listing (02 02): the names-only variant
        (02 01) sometimes returns an empty reply on the MPC4000 even when
        items exist (observed on hardware)."""
        try:
            _, data = self.sysex(section, 0x02, b"\x02")
        except MpcError as e:
            if "item not found" in str(e) or "0x101" in str(e):
                return []
            raise
        try:
            vals = parse_typed(data)
        except MpcError:
            return []
        return [v for v in vals if isinstance(v, str) and v]

    def get_memory_handle(self, section, name):
        _, data = self.sysex(section, 0x08, name.encode("ascii") + b"\x00")
        # typed DWORD reply, possibly preceded by a marker byte
        for i in range(min(4, len(data))):
            if data[i] == 0x04 and len(data) >= i + 5:
                return dword7(data, i + 1)
        raise MpcError("could not resolve memory handle for %r" % name)

    def get_memory_item(self, cmd, handle, local_path, progress=None):
        """Download a RAM item (sample/program/multi/song) to local_path."""
        self._pace(0.25)
        self.dev.write(bytes([cmd]) + struct.pack(">I", handle))
        return self._recv_file(local_path, "memory item", progress)

    def save_memory_item(self, handle, type_byte, overwrite=True, children=True):
        """Save one RAM item into the current disk folder (10 2C)."""
        args = bytes([handle & 0x7F, (handle >> 7) & 0x7F,
                      (handle >> 14) & 0x7F, (handle >> 21) & 0x7F,
                      type_byte, 1 if overwrite else 0, 1 if children else 0])
        self.sysex_slow(0x2C, args, timeout=600.0)

    def save_memory_all(self, type_byte=0, overwrite=True, children=True):
        """Save all RAM items of a type (0=everything) into the current
        disk folder (20 2D)."""
        self.sysex(0x20, 0x2D,
                   bytes([type_byte, 1 if overwrite else 0,
                          1 if children else 0]), timeout=600.0)

    # -- program / keygroup / zone editing (live, in RAM) ------------------

    def typed(self, section, item, args=b""):
        _, data = self.sysex(section, item, args)
        return parse_typed(data)

    def prog_select(self, name):
        self.sysex(0x14, 0x04, name.encode("ascii") + b"\x00")

    def prog_get_overview(self, name):
        self.prog_select(name)
        no_kg = self.typed(0x17, 0x09)[0]
        out = {"name": name, "no_keygroups": no_kg}
        out["level"] = self.typed(0x17, 0x0C)[0] / 10.0
        out["tune"] = self.typed(0x17, 0x30)[0]
        out["polyphony"] = self.typed(0x17, 0x10)[0]
        out["pb_up"] = self.typed(0x17, 0x40)[0]
        out["pb_down"] = self.typed(0x17, 0x41)[0]
        kgs = []
        for i in range(no_kg):
            self.sysex(0x10, 0x01, bytes([i]))
            lo = self.typed(0x13, 0x04)[0]
            hi = self.typed(0x13, 0x05)[0]
            kgs.append({"index": i, "low": lo, "high": hi})
        out["keygroups"] = kgs
        return out

    PROG_SET = {"level": ("sword10", 0x0C), "tune": ("sword", 0x30),
                "polyphony": ("byte", 0x10), "pb_up": ("byte", 0x40),
                "pb_down": ("byte", 0x41)}

    def prog_set(self, name, param, value):
        self.prog_select(name)
        kind, item = self.PROG_SET[param]
        self.sysex(0x16, item, self._enc(kind, value))

    @staticmethod
    def _enc(kind, value):
        if kind == "byte":
            return bytes([_pclamp(value, 0, 127)])
        if kind == "sbyte":
            return enc_sbyte(value)
        if kind == "sword":
            return enc_sword(value)
        if kind == "sword10":
            return enc_sword(int(round(float(value) * 10)))
        if kind == "qword":
            return enc_qword(max(0, int(value)))
        raise MpcError("bad encoding kind %r" % kind)

    def kg_select(self, prog_name, index):
        self.prog_select(prog_name)
        self.sysex(0x10, 0x01, bytes([index]))

    def kg_get(self, prog_name, index):
        self.kg_select(prog_name, index)
        g = self.typed
        out = {"index": index,
               "low": g(0x13, 0x04)[0], "high": g(0x13, 0x05)[0],
               "mute_group": g(0x13, 0x06)[0],
               "tune": g(0x13, 0x10)[0],
               "level": g(0x13, 0x11)[0] / 10.0,
               "polyphony": g(0x13, 0x0E)[0],
               "filter_mode": g(0x13, 0x20, b"\x00")[0],
               "filter_cutoff": g(0x13, 0x21, b"\x00")[0],
               "filter_res": g(0x13, 0x22, b"\x00")[0],
               "amp_attack": g(0x13, 0x30, b"\x00")[0],
               "amp_decay": g(0x13, 0x32, b"\x00")[0],
               "amp_sustain": g(0x13, 0x33, b"\x00")[0],
               "amp_release": g(0x13, 0x34, b"\x00")[0],
               "lfo1_rate": g(0x13, 0x50, b"\x00")[0],
               "lfo1_depth": g(0x13, 0x52, b"\x00\x00")[0],
               "lfo1_wave": g(0x13, 0x53, b"\x00")[0]}
        for e, pfx in ((1, "fenv"),):
            out[pfx + "_rates"] = [g(0x13, it, bytes([e]))[0]
                                   for it in (0x30, 0x32, 0x36, 0x34)]
            out[pfx + "_levels"] = [g(0x13, it, bytes([e]))[0]
                                    for it in (0x31, 0x33, 0x35, 0x37)]
        zones = []
        for z in range(1, 5):
            zb = bytes([z])
            zones.append({
                "zone": z,
                "sample": g(0x0F, 0x01, zb)[0],
                "level": g(0x0F, 0x02, zb)[0] / 10.0,
                "pan": g(0x0F, 0x03, zb)[0],
                "tune": g(0x0F, 0x06, zb)[0],
                "kbd_track": g(0x0F, 0x07, zb)[0],
                "playback": g(0x0F, 0x08, zb)[0],
                "vel_low": g(0x0F, 0x0A, zb)[0],
                "vel_high": g(0x0F, 0x0B, zb)[0]})
        out["zones"] = zones
        return out

    KG_SET = {"low": ("byte", 0x04), "high": ("byte", 0x05),
              "mute_group": ("byte", 0x06), "tune": ("sword", 0x10),
              "level": ("sword10", 0x11), "polyphony": ("byte", 0x0E)}
    KG_SET_BLOCK = {"filter_mode": 0x20, "filter_cutoff": 0x21,
                    "filter_res": 0x22}
    KG_SET_ENV = {"amp_attack": (0, 0x30), "amp_decay": (0, 0x32),
                  "amp_sustain": (0, 0x33), "amp_release": (0, 0x34),
                  "fenv_rate1": (1, 0x30), "fenv_level1": (1, 0x31),
                  "fenv_rate2": (1, 0x32), "fenv_level2": (1, 0x33),
                  "fenv_rate3": (1, 0x36), "fenv_level3": (1, 0x35),
                  "fenv_rate4": (1, 0x34), "fenv_level4": (1, 0x37)}
    KG_SET_LFO = {"lfo1_rate": 0x50, "lfo1_delay": 0x51,
                  "lfo1_depth": 0x52, "lfo1_wave": 0x53}
    ZONE_SET = {"level": ("sword10", 0x02), "pan": ("byte", 0x03),
                "tune": ("sword", 0x06), "kbd_track": ("byte", 0x07),
                "playback": ("byte", 0x08), "vel_low": ("byte", 0x0A),
                "vel_high": ("byte", 0x0B), "sample": ("string", 0x01)}

    def kg_set(self, prog_name, index, param, value, zone=None, edit_all=False):
        self.kg_select(prog_name, index)
        if edit_all:
            self.sysex(0x12, 0x02, b"\x01")  # edit mode: ALL
        try:
            if zone is not None:
                kind, item = self.ZONE_SET[param]
                if kind == "string":
                    args = bytes([zone]) + str(value).encode("ascii") + b"\x00"
                else:
                    args = bytes([zone]) + self._enc(kind, value)
                self.sysex(0x0E, item, args)
            elif param in self.KG_SET:
                kind, item = self.KG_SET[param]
                self.sysex(0x12, item, self._enc(kind, value))
            elif param in self.KG_SET_BLOCK:
                self.sysex(0x12, self.KG_SET_BLOCK[param],
                           b"\x00" + bytes([_pclamp(value, 0, 127)]))
            elif param in self.KG_SET_ENV:
                env, item = self.KG_SET_ENV[param]
                self.sysex(0x12, item,
                           bytes([env, _pclamp(value, 0, 100)]))
            elif param in self.KG_SET_LFO:
                self.sysex(0x12, self.KG_SET_LFO[param],
                           b"\x00" + bytes([_pclamp(value, 0, 127)]))
            else:
                raise MpcError("unknown keygroup param %r" % param)
        finally:
            if edit_all:
                self.sysex(0x12, 0x02, b"\x00")  # back to SINGLE

    # -- sample editing (trim / loop / audition) ---------------------------

    def sample_select(self, name):
        self.sysex(0x1C, 0x04, name.encode("ascii") + b"\x00")

    def sample_info(self, name):
        self.sample_select(name)
        g = self.typed
        info = {"name": name,
                "length": g(0x1F, 0x50)[0],
                "rate": g(0x1F, 0x51)[0],
                "bits": g(0x1F, 0x52)[0],
                "channels": g(0x1F, 0x55)[0],
                "trim_start": g(0x1F, 0x20)[0],
                "trim_end": g(0x1F, 0x21)[0],
                "playback_mode": g(0x1F, 0x26)[0],
                "root": g(0x1F, 0x24)[0],
                "no_loops": g(0x1F, 0x38)[0]}
        if info["no_loops"] > 0:
            info["loop_start"] = g(0x1F, 0x30, b"\x00")[0]
            info["loop_end"] = g(0x1F, 0x31, b"\x00")[0]
        return info

    SAMPLE_SET = {"trim_start": 0x20, "trim_end": 0x21,
                  "playback_mode": 0x26, "root": 0x24}

    def sample_set(self, name, param, value):
        self.sample_select(name)
        if param in ("loop_start", "loop_end"):
            item = 0x30 if param == "loop_start" else 0x31
            self.sysex(0x1E, item, b"\x00" + enc_qword(max(0, int(value))))
        elif param == "create_loop":
            self.sysex(0x1C, 0x48)
        elif param in ("trim_start", "trim_end"):
            self.sysex(0x1E, self.SAMPLE_SET[param],
                       enc_qword(max(0, int(value))))
        elif param in ("playback_mode", "root"):
            self.sysex(0x1E, self.SAMPLE_SET[param],
                       bytes([_pclamp(value, 0, 127)]))
        else:
            raise MpcError("unknown sample param %r" % param)

    def sample_play(self, name, velocity=110, loop=False):
        self.sample_select(name)
        self.sysex(0x1C, 0x40, bytes([_pclamp(velocity, 1, 127),
                                      1 if loop else 0]))

    def sample_stop(self):
        self.sysex(0x1C, 0x41)

    def put_memory_file(self, local_path, name_with_ext, progress=None):
        """Upload a file (wav/akp/akm/mid) into the sampler's RAM."""
        size = os.path.getsize(local_path)
        if size == 0:
            raise MpcError("refusing to transfer empty file %s" % local_path)
        cmd = b"\x20" + struct.pack(">I", size) + \
            name_with_ext.encode("ascii") + b"\x00"
        return self._put_stream(cmd, local_path, size, name_with_ext, progress)

    # -- raw file transfer (bulk block protocol) ---------------------------

    def put_file(self, local_path, remote_name, progress=None):
        size = os.path.getsize(local_path)
        if size == 0:
            raise MpcError("refusing to transfer empty file %s" % local_path)
        cmd = b"\x40" + struct.pack(">I", size) + \
            remote_name.encode("ascii") + b"\x00"
        return self._put_stream(cmd, local_path, size, remote_name, progress)

    def _put_stream(self, cmd, local_path, size, remote_name, progress=None):
        self._pace(0.25)
        self.dev.write(cmd)
        with open(local_path, "rb") as f:
            deadline = time.time() + 3600
            while True:
                if time.time() > deadline:
                    raise MpcError("upload timeout")
                try:
                    r = self.dev.read(64, timeout_ms=8000)
                except TimeoutError:
                    continue
                if r == BUSY:
                    continue
                if len(r) == 1:
                    if r == b"\x01":
                        raise MpcError("sampler rejected file %r" % remote_name)
                    if r == b"\x00":
                        return size
                    continue
                if len(r) == 5:
                    return size  # end-of-transfer status
                if len(r) == 8:
                    done, blocksize = struct.unpack(">II", r)
                    if done >= size and blocksize == 0:
                        return size
                    f.seek(done)
                    block = f.read(blocksize)
                    self.dev.write(block, timeout_ms=20000)
                    self.dev.write(b"\x00")
                    if progress:
                        progress(min(done + len(block), size), size)

    def get_file(self, remote_name, local_path, progress=None):
        self._pace(0.25)
        self.dev.write(b"\x41" + remote_name.encode("ascii") + b"\x00")
        return self._recv_file(local_path, remote_name, progress)

    def _recv_file(self, local_path, what, progress=None):
        expected = None
        written = 0
        with open(local_path, "wb") as f:
            deadline = time.time() + 3600
            pending = b""
            while True:
                if time.time() > deadline:
                    raise MpcError("download timeout")
                if pending:
                    r, pending = pending, b""
                else:
                    try:
                        r = self.dev.read(16384, timeout_ms=8000)
                    except TimeoutError:
                        continue
                if r == BUSY:
                    continue
                if len(r) == 8:
                    done, blocksize = struct.unpack(">II", r)
                    if done == 1 and expected is None:
                        os.unlink(local_path)
                        raise MpcError("not found: %r" % what)
                    if blocksize == 0:
                        return max(done, written)
                    expected = (done, blocksize)
                    block = b""
                    while len(block) < blocksize:
                        try:
                            part = self.dev.read(blocksize - len(block),
                                                 timeout_ms=8000)
                        except TimeoutError:
                            continue
                        if part == BUSY and not block:
                            continue
                        block += part
                    f.seek(done)
                    f.write(block)
                    written = max(written, done + len(block))
                    if progress:
                        progress(written, None)
                    self.dev.write(b"\x00")
                    continue
                # unexpected chunk — could be status glued to data
                if len(r) > 8:
                    pending = r
                    continue


# ---------------------------------------------------------------------------
# serve mode — JSON-line protocol over stdin/stdout for the GUI app
# ---------------------------------------------------------------------------

SOCKET_PATH = "/tmp/mpc4kd-%d.sock" % os.getuid()


class Engine:
    """Owns the single USB connection. All ops are serialized through a lock;
    commands are paced (see Mpc4000) so the sampler firmware is not flooded.
    After the sampler reboots (detected via USB re-enumeration) the default
    disk is re-selected so browsing cannot silently target the wrong disk."""

    def __init__(self):
        import threading
        self.lock = threading.Lock()
        self.mpc = None
        self.saved = {}
        self.last_bus_addr = None
        self.last_op_time = time.time()

    def ensure(self):
        if self.mpc is not None:
            return self.mpc
        last_err = None
        for _ in range(8):
            try:
                self.mpc = Mpc4000()
                break
            except (MpcError, usbio.UsbError, RuntimeError) as e:
                last_err = e
                time.sleep(0.5)
        else:
            raise MpcError("sampler busy or unavailable (%s)" % last_err)
        m = self.mpc
        addr = m.dev.bus_address()
        rebooted = addr != self.last_bus_addr
        self.last_bus_addr = addr
        if self.saved and not rebooted:
            m.disks = self.saved["disks"]
            m.current_disk = self.saved["current_disk"]
        else:
            disks = m.get_disklist()
            hard = [d for d in disks if d["type"] == 1 and d["writable"]]
            chosen = (hard or disks)[0]
            # first contact with this sampler session: select the default
            # disk explicitly (the internal drive takes ~50s to mount, but
            # skipping this after a reboot silently browses the wrong disk)
            m.select_disk(chosen["handle"])
            m.disks, m.current_disk = disks, chosen
            self.saved = {}
        return m

    def release(self):
        if self.mpc is not None:
            self.saved = {"disks": self.mpc.disks,
                          "current_disk": self.mpc.current_disk}
            try:
                self.mpc.close()
            except Exception:
                pass
            self.mpc = None

    def dispatch(self, req, emit):
        """Execute one request, emitting exactly one final ok/error object
        (plus progress events)."""

        def progress(done, total):
            emit({"event": "progress", "done": done, "total": total})

        self.last_op_time = time.time()
        try:
            op = req.get("op")
            m = self.ensure()

            if op == "connect":
                emit({"ok": True, "result": {
                    "product": "MPC4000" if m.product_id == PRODUCT_MPC4000
                               else "Z4/Z8",
                    "disks": m.disks,
                    "disk": m.current_disk}})
            elif op == "select_disk":
                if int(req["handle"]) != m.current_disk["handle"]:
                    m.select_disk(int(req["handle"]))
                    m.current_disk = next(d for d in m.disks
                                          if d["handle"] == int(req["handle"]))
                emit({"ok": True, "result": None})
            elif op == "ls":
                m.open_folder(req.get("path", ""))
                emit({"ok": True, "result": {
                    "folders": sorted(m.get_folder_names(), key=str.lower),
                    "files": [{"name": n, "size": s} for n, s in
                              sorted(m.get_filenames(),
                                     key=lambda t: t[0].lower())]}})
            elif op == "mkdir":
                m.open_folder(req.get("dir", ""))
                m.create_folder(req["name"])
                emit({"ok": True, "result": None})
            elif op == "rm":
                m.open_folder(req.get("dir", ""))
                m.delete(req["name"])
                emit({"ok": True, "result": None})
            elif op == "rename":
                m.open_folder(req.get("dir", ""))
                if req.get("is_folder"):
                    m.rename_folder(req["old"], req["new"])
                else:
                    m.rename_file(req["old"], req["new"])
                emit({"ok": True, "result": None})
            elif op == "put":
                m.open_folder(req.get("dir", ""))
                size = m.put_file(req["local"], req["name"], progress)
                emit({"ok": True, "result": {"size": size}})
            elif op == "get":
                m.open_folder(req.get("dir", ""))
                size = m.get_file(req["name"], req["local"], progress)
                emit({"ok": True, "result": {"size": size}})
            elif op == "move":
                import tempfile
                m.open_folder(req["src_dir"])
                with tempfile.NamedTemporaryFile(delete=False) as tf:
                    tmp = tf.name
                try:
                    m.get_file(req["name"], tmp, progress)
                    m.open_folder(req["dst_dir"])
                    m.put_file(tmp, req["name"], progress)
                    m.open_folder(req["src_dir"])
                    m.delete(req["name"])
                finally:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
                emit({"ok": True, "result": None})
            elif op == "mem_list":
                out = {}
                for kind, spec in MEM_KINDS.items():
                    out[kind] = m.get_memory_names(spec["section"])
                emit({"ok": True, "result": out})
            elif op == "mem_get":
                spec = MEM_KINDS[req["kind"]]
                handle = m.get_memory_handle(spec["section"], req["name"])
                size = m.get_memory_item(spec["cmd"], handle, req["local"],
                                         progress)
                emit({"ok": True, "result": {"size": size}})
            elif op == "mem_save":
                m.open_folder(req.get("dir", ""))
                if req.get("name"):
                    spec = MEM_KINDS[req["kind"]]
                    handle = m.get_memory_handle(spec["section"], req["name"])
                    m.save_memory_item(handle, spec["type"])
                else:
                    m.save_memory_all()
                emit({"ok": True, "result": None})
            elif op == "prog_open":
                emit({"ok": True, "result": m.prog_get_overview(req["name"])})
            elif op == "prog_set":
                m.prog_set(req["name"], req["param"], req["value"])
                emit({"ok": True, "result": None})
            elif op == "kg_get":
                emit({"ok": True,
                      "result": m.kg_get(req["prog"], int(req["index"]))})
            elif op == "kg_set":
                m.kg_set(req["prog"], int(req["index"]), req["param"],
                         req["value"], zone=req.get("zone"),
                         edit_all=bool(req.get("edit_all")))
                emit({"ok": True, "result": None})
            elif op == "sample_info":
                emit({"ok": True, "result": m.sample_info(req["name"])})
            elif op == "sample_set":
                m.sample_set(req["name"], req["param"], req["value"])
                emit({"ok": True, "result": None})
            elif op == "sample_play":
                m.sample_play(req["name"], int(req.get("velocity", 110)),
                              bool(req.get("loop")))
                emit({"ok": True, "result": None})
            elif op == "sample_stop":
                m.sample_stop()
                emit({"ok": True, "result": None})
            elif op == "mem_put":
                size = m.put_memory_file(req["local"], req["name"], progress)
                emit({"ok": True, "result": {"size": size}})
            elif op == "get_folder":
                base = req.get("dir", "")
                remote = (base + "\\" + req["name"]) if base else req["name"]

                def item_cb(name, i, n):
                    emit({"event": "item", "name": name,
                          "index": i, "count": n})

                result = m.download_folder(remote, req["local"], progress,
                                           item_cb)
                emit({"ok": True, "result": result})
            elif op == "load_file":
                m.open_folder(req.get("dir", ""))
                m.load_file(req["name"], bool(req.get("deps", True)))
                emit({"ok": True, "result": None})
            elif op == "ping":
                emit({"ok": True, "result": "pong"})
            else:
                emit({"ok": False, "error": "unknown op %r" % op})
        except (MpcError, usbio.UsbError, RuntimeError, TimeoutError,
                KeyError, ValueError, OSError) as e:
            if self.mpc is not None and isinstance(e, (usbio.UsbError,
                                                       TimeoutError)):
                try:
                    self.mpc.close()
                except Exception:
                    pass
                self.mpc = None  # reconnect on next command
            emit({"ok": False, "error": str(e)})
        finally:
            self.last_op_time = time.time()


DAEMON_LOG = "/tmp/mpc4kd.log"


def _dlog(msg):
    try:
        with open(DAEMON_LOG, "a") as f:
            f.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
    except OSError:
        pass


def daemon():
    """Single process that owns the sampler connection; GUI apps and future
    tools connect as clients over a unix socket, so the sampler sees exactly
    one session (one init handshake, serialized + paced commands)."""
    import json
    import socket
    import threading
    _dlog("daemon starting (pid %d)" % os.getpid())

    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass
    except OSError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(SOCKET_PATH)
    except OSError:
        return 0  # another daemon is already running
    server.listen(8)

    engine = Engine()
    clients = set()
    clients_lock = threading.Lock()

    def idle_watch():
        while True:
            time.sleep(2.0)
            with clients_lock:
                n = len(clients)
            if engine.lock.acquire(blocking=False):
                try:
                    if engine.mpc is not None and \
                            time.time() - engine.last_op_time > 10.0:
                        engine.release()  # frees the claim for CLI use
                finally:
                    engine.lock.release()
            if n == 0 and time.time() - engine.last_op_time > 900:
                os._exit(0)

    threading.Thread(target=idle_watch, daemon=True).start()

    def handle(conn):
        f = conn.makefile("rwb")

        def emit(obj):
            try:
                f.write((json.dumps(obj) + "\n").encode())
                f.flush()
            except OSError:
                raise BrokenPipeError

        try:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    req = json.loads(raw)
                except ValueError:
                    continue
                _dlog("op %s waiting for lock" % req.get("op"))
                with engine.lock:
                    _dlog("op %s executing" % req.get("op"))
                    t0 = time.time()
                    try:
                        engine.dispatch(req, emit)
                    except BrokenPipeError:
                        _dlog("op %s client gone" % req.get("op"))
                        break
                    _dlog("op %s done in %.1fs" % (req.get("op"),
                                                   time.time() - t0))
        except (OSError, BrokenPipeError):
            pass
        finally:
            with clients_lock:
                clients.discard(conn)
            try:
                conn.close()
            except OSError:
                pass

    while True:
        conn, _ = server.accept()
        with clients_lock:
            clients.add(conn)
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


def client_bridge():
    """stdin/stdout <-> daemon socket bridge; spawns the daemon on demand.
    Used by the GUI apps (invoked as 'serve' for compatibility)."""
    import socket
    import subprocess
    import threading

    sock = None
    for attempt in range(40):
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(SOCKET_PATH)
            break
        except OSError:
            sock.close()
            sock = None
            if attempt == 0:
                subprocess.Popen(
                    [sys.executable, os.path.abspath(__file__), "daemon"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True)
            time.sleep(0.25)
    if sock is None:
        sys.stdout.write('{"ok": false, "error": "could not start mpc4k daemon"}\n')
        sys.stdout.flush()
        return 1

    def down():
        f = sock.makefile("rb")
        for line in f:
            sys.stdout.buffer.write(line)
            sys.stdout.flush()
        os._exit(0)

    threading.Thread(target=down, daemon=True).start()
    try:
        for line in sys.stdin:
            sock.sendall(line.encode())
    except (OSError, KeyboardInterrupt):
        pass
    try:
        sock.close()
    except OSError:
        pass
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def split_remote(path):
    path = path.strip("/").replace("/", "\\")
    if "\\" in path:
        d, _, n = path.rpartition("\\")
        return d, n
    return "", path


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%d %s" % (n, unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0


def connect(disk=None):
    """CLI connection. Selects the target disk explicitly — after a sampler
    reboot the current disk is unpredictable, and browsing the wrong disk
    is worse than the wait (the internal drive takes ~50s to mount). The
    GUI apps avoid the wait by going through the daemon, which selects once
    per sampler session."""
    mpc = Mpc4000()
    disks = mpc.get_disklist()
    if not disks:
        mpc.update_disklist()
        disks = mpc.get_disklist()
    if not disks:
        raise MpcError("sampler reports no disks")
    if disk is not None:
        chosen = next((d for d in disks if d["handle"] == disk), None)
        if chosen is None:
            raise MpcError("no disk with handle %d" % disk)
    else:
        hard = [d for d in disks if d["type"] == 1 and d["writable"]]
        chosen = (hard or disks)[0]
    mpc.select_disk(chosen["handle"])
    return mpc, disks, chosen


def progress_bar(done, total):
    if total:
        sys.stdout.write("\r  %s / %s (%d%%)" %
                         (human(done), human(total), 100 * done // total))
    else:
        sys.stdout.write("\r  %s" % human(done))
    sys.stdout.flush()


def main(argv):
    args = [a for a in argv[1:]]
    disk = None
    if "--disk" in args:
        i = args.index("--disk")
        disk = int(args[i + 1])
        del args[i:i + 2]
    if not args:
        print(__doc__)
        return 2
    cmd, args = args[0], args[1:]

    if cmd == "serve":
        return client_bridge()
    if cmd == "daemon":
        return daemon()

    if cmd == "info":
        mpc, disks, chosen = connect(disk)
        print("connected: Akai %s (usb 09e8:%04x)" %
              ("MPC4000" if mpc.product_id == PRODUCT_MPC4000 else "Z4/Z8",
               mpc.product_id))
        for d in disks:
            mark = " *" if d["handle"] == chosen["handle"] else ""
            print("  disk %d: %s (%s, %s%s)%s" %
                  (d["handle"], d["name"] or "(unnamed)",
                   DISK_TYPES.get(d["type"], d["type"]),
                   DISK_FORMATS.get(d["format"], d["format"]),
                   "" if d["writable"] else ", read-only", mark))
        mpc.close()
        return 0

    if cmd == "disks":
        mpc, disks, _ = connect(disk)
        for d in disks:
            print("%3d  %-16s %-10s %-8s %s" %
                  (d["handle"], d["name"] or "(unnamed)",
                   DISK_TYPES.get(d["type"], d["type"]),
                   DISK_FORMATS.get(d["format"], d["format"]),
                   "writable" if d["writable"] else "read-only"))
        mpc.close()
        return 0

    mpc, _, _ = connect(disk)
    try:
        if cmd == "ls":
            path = args[0].strip("/").replace("/", "\\") if args else ""
            mpc.open_folder(path)
            for name in sorted(mpc.get_folder_names()):
                print("%-28s <DIR>" % (name + "/"))
            for name, size in sorted(mpc.get_filenames()):
                print("%-28s %10s" % (name, human(size)))

        elif cmd == "mkdir":
            d, n = split_remote(args[0])
            mpc.open_folder(d)
            mpc.create_folder(n)
            print("created %s" % args[0])

        elif cmd == "rm":
            for a in args:
                d, n = split_remote(a)
                mpc.open_folder(d)
                mpc.delete(n)
                print("deleted %s" % a)

        elif cmd == "mv":
            d1, n1 = split_remote(args[0])
            d2, n2 = split_remote(args[1])
            if d1 == d2:
                mpc.open_folder(d1)
                folders = mpc.get_folder_names()
                if n1 in folders:
                    mpc.rename_folder(n1, n2)
                else:
                    mpc.rename_file(n1, n2)
                print("renamed %s -> %s" % (args[0], n2))
            else:
                import tempfile
                mpc.open_folder(d1)
                files = dict(mpc.get_filenames())
                if n1 not in files:
                    raise MpcError("moving folders between directories is not "
                                   "supported (file not found: %s)" % args[0])
                with tempfile.NamedTemporaryFile(delete=False) as tf:
                    tmp = tf.name
                try:
                    mpc.get_file(n1, tmp, progress_bar)
                    print()
                    mpc.open_folder(d2)
                    mpc.put_file(tmp, n2, progress_bar)
                    print()
                    mpc.open_folder(d1)
                    mpc.delete(n1)
                finally:
                    os.unlink(tmp)
                print("moved %s -> %s" % (args[0], args[1]))

        elif cmd == "put":
            *locals_, remote_dir = args
            if len(locals_) < 1:
                raise MpcError("usage: put LOCAL... REMOTE_DIR")
            mpc.open_folder(remote_dir.strip("/").replace("/", "\\"))
            for lp in locals_:
                name = os.path.basename(lp)
                print("uploading %s" % name)
                mpc.put_file(lp, name, progress_bar)
                print()

        elif cmd == "mem":
            for kind, spec in MEM_KINDS.items():
                names = mpc.get_memory_names(spec["section"])
                print("%s (%d)" % (kind, len(names)))
                for n in names:
                    print("  %s" % n)

        elif cmd == "memget":
            # memget NAME [LOCALDIR] — download a RAM item to the Mac
            name, localdir = args[0], (args[1] if len(args) > 1 else ".")
            found = None
            for kind, spec in MEM_KINDS.items():
                if name in mpc.get_memory_names(spec["section"]):
                    found = spec
                    break
            if not found:
                raise MpcError("no RAM item named %r" % name)
            handle = mpc.get_memory_handle(found["section"], name)
            local = os.path.join(localdir, name + found["ext"])
            print("downloading %s from RAM" % name)
            size = mpc.get_memory_item(found["cmd"], handle, local, progress_bar)
            print("\nsaved %s (%s)" % (local, human(size)))

        elif cmd == "memsave":
            # memsave FOLDER [NAME] — save RAM to a folder on the MPC disk
            folder = args[0].strip("/").replace("/", "\\")
            mpc.open_folder(folder)
            if len(args) > 1:
                name = args[1]
                for kind, spec in MEM_KINDS.items():
                    if name in mpc.get_memory_names(spec["section"]):
                        handle = mpc.get_memory_handle(spec["section"], name)
                        mpc.save_memory_item(handle, spec["type"])
                        print("saved %s -> %s" % (name, args[0]))
                        break
                else:
                    raise MpcError("no RAM item named %r" % name)
            else:
                mpc.save_memory_all()
                print("saved all RAM items -> %s" % args[0])

        elif cmd == "get":
            d, n = split_remote(args[0])
            local = os.path.join(args[1] if len(args) > 1 else ".", n)
            mpc.open_folder(d)
            print("downloading %s" % n)
            size = mpc.get_file(n, local, progress_bar)
            print("\nsaved %s (%s)" % (local, human(size)))

        else:
            print(__doc__)
            return 2
    finally:
        mpc.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except MpcError as e:
        print("ERROR: %s" % e, file=sys.stderr)
        sys.exit(1)
