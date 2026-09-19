"""User-scoped Windows DPAPI. Never falls back to plaintext on Windows."""
import base64
import ctypes
import json
import os
from ctypes import wintypes

from .models import GridError


def _dpapi(payload, *, decrypt=False):
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(payload)
    source = Blob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    destination = Blob()
    crypt = ctypes.WinDLL("crypt32.dll", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    # CRYPTPROTECT_UI_FORBIDDEN, without CRYPTPROTECT_LOCAL_MACHINE.
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(destination)):
        raise GridError("Windows could not protect/unprotect session for this account; re-import it")
    try:
        return ctypes.string_at(destination.data, destination.size)
    finally:
        kernel.LocalFree(ctypes.cast(destination.data, ctypes.c_void_p))


def encode_session(data):
    if os.name != "nt":
        return data
    protected = _dpapi(json.dumps(data).encode("utf-8"))
    return {"format": "windows-dpapi-v1", "protected": base64.b64encode(protected).decode("ascii")}


def decode_session(data):
    if not isinstance(data, dict):
        raise GridError("Invalid session file format")
    if os.name == "nt":
        if data.get("format") != "windows-dpapi-v1":
            raise GridError("Plaintext Windows sessions are refused; run init-session again")
        try:
            data = json.loads(_dpapi(base64.b64decode(data["protected"], validate=True), decrypt=True))
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise GridError("Invalid protected Windows session") from None
    if not isinstance(data, dict) or "token" not in data:
        raise GridError("Invalid session file format; re-import on this machine")
    return data
