"""Minimal ctypes binding for libusb-1.0 — just enough to talk to an Akai
Z4/Z8/MPC4000 over its bulk endpoints. No pip dependencies; uses the
Homebrew libusb dylib (or a bundled copy next to this file).
"""

import ctypes
import ctypes.util
import os

_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "libusb-1.0.dylib"),
    "/opt/homebrew/lib/libusb-1.0.dylib",
    "/usr/local/lib/libusb-1.0.dylib",
]


def _load():
    for p in _CANDIDATES:
        if os.path.exists(p):
            return ctypes.CDLL(p)
    found = ctypes.util.find_library("usb-1.0")
    if found:
        return ctypes.CDLL(found)
    raise RuntimeError(
        "libusb-1.0 not found — install it with: brew install libusb")


_lib = _load()

_lib.libusb_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
_lib.libusb_exit.argtypes = [ctypes.c_void_p]
_lib.libusb_open_device_with_vid_pid.restype = ctypes.c_void_p
_lib.libusb_open_device_with_vid_pid.argtypes = [
    ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint16]
_lib.libusb_close.argtypes = [ctypes.c_void_p]
_lib.libusb_claim_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
_lib.libusb_release_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
_lib.libusb_set_configuration.argtypes = [ctypes.c_void_p, ctypes.c_int]
_lib.libusb_bulk_transfer.argtypes = [
    ctypes.c_void_p, ctypes.c_ubyte, ctypes.POINTER(ctypes.c_ubyte),
    ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_uint]
_lib.libusb_strerror.restype = ctypes.c_char_p
_lib.libusb_strerror.argtypes = [ctypes.c_int]
_lib.libusb_clear_halt.argtypes = [ctypes.c_void_p, ctypes.c_ubyte]
_lib.libusb_reset_device.argtypes = [ctypes.c_void_p]
_lib.libusb_get_device.restype = ctypes.c_void_p
_lib.libusb_get_device.argtypes = [ctypes.c_void_p]
_lib.libusb_get_bus_number.restype = ctypes.c_uint8
_lib.libusb_get_bus_number.argtypes = [ctypes.c_void_p]
_lib.libusb_get_device_address.restype = ctypes.c_uint8
_lib.libusb_get_device_address.argtypes = [ctypes.c_void_p]

LIBUSB_ERROR_TIMEOUT = -7


class UsbError(RuntimeError):
    def __init__(self, code, what=""):
        self.code = code
        msg = _lib.libusb_strerror(code).decode("ascii", "replace")
        super().__init__("%s: %s (libusb %d)" % (what or "usb", msg, code))


class UsbDevice:
    """One claimed USB device with a bulk OUT and bulk IN endpoint."""

    def __init__(self, vid, pid, interface=0, ep_out=0x02, ep_in=0x82,
                 configuration=1):
        self._ctx = ctypes.c_void_p()
        rc = _lib.libusb_init(ctypes.byref(self._ctx))
        if rc:
            raise UsbError(rc, "libusb_init")
        self._h = _lib.libusb_open_device_with_vid_pid(self._ctx, vid, pid)
        if not self._h:
            _lib.libusb_exit(self._ctx)
            self._ctx = None
            raise RuntimeError(
                "device %04x:%04x not found (is the sampler on and connected?)"
                % (vid, pid))
        self.interface = interface
        self.ep_out = ep_out
        self.ep_in = ep_in
        if configuration is not None:
            _lib.libusb_set_configuration(self._h, configuration)
        rc = _lib.libusb_claim_interface(self._h, interface)
        if rc:
            self.close()
            raise UsbError(rc, "claim_interface")

    def write(self, data, timeout_ms=2000):
        buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        done = ctypes.c_int(0)
        rc = _lib.libusb_bulk_transfer(self._h, self.ep_out, buf, len(data),
                                       ctypes.byref(done), timeout_ms)
        if rc:
            raise UsbError(rc, "bulk write")
        if done.value != len(data):
            raise RuntimeError("short bulk write: %d/%d" % (done.value, len(data)))

    def read(self, size, timeout_ms=2000):
        buf = (ctypes.c_ubyte * size)()
        done = ctypes.c_int(0)
        rc = _lib.libusb_bulk_transfer(self._h, self.ep_in, buf, size,
                                       ctypes.byref(done), timeout_ms)
        if rc == LIBUSB_ERROR_TIMEOUT:
            raise TimeoutError("bulk read timeout")
        if rc:
            raise UsbError(rc, "bulk read")
        return bytes(buf[:done.value])

    def bus_address(self):
        """(bus, address) — the address changes when the device re-enumerates
        (e.g. after the sampler reboots), which callers use to detect that."""
        dev = _lib.libusb_get_device(self._h)
        return (_lib.libusb_get_bus_number(dev),
                _lib.libusb_get_device_address(dev))

    def clear_halt(self):
        _lib.libusb_clear_halt(self._h, self.ep_in)
        _lib.libusb_clear_halt(self._h, self.ep_out)

    def reset(self):
        _lib.libusb_reset_device(self._h)

    def close(self):
        if getattr(self, "_h", None):
            try:
                _lib.libusb_release_interface(self._h, self.interface)
            except Exception:
                pass
            _lib.libusb_close(self._h)
            self._h = None
        if getattr(self, "_ctx", None):
            _lib.libusb_exit(self._ctx)
            self._ctx = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
