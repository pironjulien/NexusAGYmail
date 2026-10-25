"""Chiffrement DPAPI (portée utilisateur Windows) et droits de fichier réservés à l'utilisateur courant.

Aucun réseau : CryptProtectData lie le chiffré au compte Windows de l'utilisateur ; il est illisible pour un autre
compte ou sur un autre PC. Les droits NTFS sont réduits à une seule entrée (l'utilisateur courant), sans héritage.
Hors Windows (macOS, Linux) : pas de DPAPI, le jeton est écrit en clair dans un fichier lisible par l'utilisateur seul
(droits 0600), comme gcloud ou gh.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path

# Entropie propre à NexusAGYmail : un autre programme du même utilisateur qui appellerait CryptUnprotectData sans
# elle ne lirait pas le jeton. Valeur figée à l'ancien nom (NexusGmail) : les jetons déjà chiffrés restent lisibles.
_ENTROPY = b"nexus-gmail/token/v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SDDL_REVISION_1 = 1
ENCRYPTED = os.name == "nt"  # DPAPI seulement sous Windows


class DpapiError(OSError):
    """Échec de DPAPI ou d'un appel de sécurité Windows."""


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dlls() -> tuple[ctypes.WinDLL, ctypes.WinDLL, ctypes.WinDLL]:
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    blob_p = ctypes.POINTER(_Blob)
    crypt32.CryptProtectData.argtypes = [
        blob_p,
        wintypes.LPCWSTR,
        blob_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        blob_p,
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        blob_p,
        ctypes.c_void_p,
        blob_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        blob_p,
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi32.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    advapi32.SetFileSecurityW.restype = wintypes.BOOL
    return crypt32, kernel32, advapi32


def _blob(data: bytes) -> tuple[_Blob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _fail(what: str) -> DpapiError:
    code = ctypes.get_last_error()
    return DpapiError(code, f"{what} (Windows error {code})")


def protect(data: bytes) -> bytes:
    """Chiffre des octets pour l'utilisateur Windows courant (hors Windows : inchangés, protégés par les droits)."""
    if not ENCRYPTED:
        return data
    crypt32, kernel32, _ = _dlls()
    source, _keep = _blob(data)
    entropy, _keep_entropy = _blob(_ENTROPY)
    out = _Blob()
    if not crypt32.CryptProtectData(
        ctypes.byref(source),
        "nexus-agymail",
        ctypes.byref(entropy),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out),
    ):
        raise _fail("DPAPI encryption refused")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def unprotect(data: bytes) -> bytes:
    """Déchiffre des octets chiffrés par protect() pour le même utilisateur Windows."""
    if not ENCRYPTED:
        return data
    crypt32, kernel32, _ = _dlls()
    source, _keep = _blob(data)
    entropy, _keep_entropy = _blob(_ENTROPY)
    out = _Blob()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source), None, ctypes.byref(entropy), None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)
    ):
        raise _fail("DPAPI decryption refused (another user, another PC or a damaged file)")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def current_user_sid() -> str:
    """SID texte de l'utilisateur du processus (ex. S-1-5-21-...)."""
    _, kernel32, advapi32 = _dlls()
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        raise _fail("OpenProcessToken refused")
    try:
        size = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, _TOKEN_USER, buffer, size, ctypes.byref(size)):
            raise _fail("GetTokenInformation refused")
        # TOKEN_USER commence par SID_AND_ATTRIBUTES { PSID Sid; DWORD Attributes; }
        psid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents.value
        text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(psid, ctypes.byref(text)):
            raise _fail("ConvertSidToStringSidW refused")
        try:
            return str(text.value)
        finally:
            kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        kernel32.CloseHandle(token)


def restrict_to_current_user(path: Path) -> None:
    """Réduit les droits du fichier à l'utilisateur courant seul (DACL protégée, sans héritage ; ailleurs 0600)."""
    if not ENCRYPTED:
        os.chmod(path, 0o600)
        return
    _, kernel32, advapi32 = _dlls()
    sddl = f"D:P(A;;FA;;;{current_user_sid()})"
    descriptor = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, _SDDL_REVISION_1, ctypes.byref(descriptor), None
    ):
        raise _fail("invalid security descriptor")
    try:
        flags = _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION
        if not advapi32.SetFileSecurityW(str(path), flags, descriptor):
            raise _fail(f"permissions not applied to {path}")
    finally:
        kernel32.LocalFree(descriptor)
