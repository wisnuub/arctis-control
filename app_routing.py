"""
Per-app audio device routing: the same (undocumented) Windows API behind
Settings > Sound > Volume mixer > per-app output/input, also used by EarTrumpet
and by Sonar itself. Windows remembers the choice per app.
"""
import ctypes
from ctypes import POINTER, byref, c_int, c_long, c_uint, c_void_p, c_wchar_p

from comtypes import GUID

combase = ctypes.WinDLL("combase")
combase.WindowsCreateString.argtypes = [c_wchar_p, c_uint, POINTER(c_void_p)]
combase.WindowsDeleteString.argtypes = [c_void_p]
combase.WindowsGetStringRawBuffer.argtypes = [c_void_p, POINTER(c_uint)]
combase.WindowsGetStringRawBuffer.restype = c_wchar_p
combase.RoGetActivationFactory.argtypes = [c_void_p, POINTER(GUID), POINTER(c_void_p)]

# IID changed in Windows 10 21H2; try the current one first.
FACTORY_IIDS = ["{ab3d4648-e242-459f-b02f-541c70306324}", "{2a59116d-6c4f-45e0-a74f-707e3fef9258}"]
# vtable: IUnknown(3) + IInspectable(3) + 19 unused methods, then these
SET_PERSISTED, GET_PERSISTED = 25, 26
RENDER, CAPTURE = 0, 1
CONSOLE, MULTIMEDIA = 0, 1
INTERFACE = {RENDER: "{e6327cad-dcec-4949-ae8a-991e976a79d2}",
             CAPTURE: "{2eef81be-33fa-4800-9670-1cd474972c3f}"}


def _hstring(s):
    h = c_void_p()
    combase.WindowsCreateString(s, len(s), byref(h))
    return h


class AppRouting:
    def __init__(self):
        combase.RoInitialize(1)              # already-initialised is fine
        name = _hstring("Windows.Media.Internal.AudioPolicyConfig")
        self.p = None
        try:
            for iid in FACTORY_IIDS:
                p = c_void_p()
                if combase.RoGetActivationFactory(name, byref(GUID(iid)), byref(p)) == 0:
                    self.p = p
                    break
        finally:
            combase.WindowsDeleteString(name)
        if self.p is None:
            raise OSError("AudioPolicyConfig factory not available on this Windows build")

    def _method(self, index, *argtypes):
        vtbl = ctypes.cast(self.p, POINTER(POINTER(c_void_p))).contents
        return ctypes.WINFUNCTYPE(c_long, c_void_p, *argtypes)(vtbl[index])

    def set(self, pid, flow, device_id):
        """Send process `pid` to MMDevice `device_id` ({0.0.0.00000000}.{guid}); None clears."""
        h = _hstring(f"\\\\?\\SWD#MMDEVAPI#{device_id}#{INTERFACE[flow]}") if device_id else c_void_p()
        fn = self._method(SET_PERSISTED, c_uint, c_int, c_int, c_void_p)
        try:
            for role in (CONSOLE, MULTIMEDIA):
                hr = fn(self.p, pid, flow, role, h)
                if hr < 0:
                    raise OSError(f"SetPersistedDefaultAudioEndpoint failed: {hr & 0xFFFFFFFF:#x}")
        finally:
            if device_id:
                combase.WindowsDeleteString(h)

    def get(self, pid, flow):
        """MMDevice id the process is pinned to, or None if it follows the default."""
        out = c_void_p()
        hr = self._method(GET_PERSISTED, c_uint, c_int, c_int, POINTER(c_void_p))(
            self.p, pid, flow, MULTIMEDIA, byref(out))
        if hr < 0 or not out.value:
            return None
        try:
            full = combase.WindowsGetStringRawBuffer(out, None)
        finally:
            combase.WindowsDeleteString(out)
        # \\?\SWD#MMDEVAPI#{0.0.0.00000000}.{guid}#{interface}
        parts = full.split("#")
        return parts[2] if len(parts) > 3 else full
