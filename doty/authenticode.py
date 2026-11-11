"""Who signed a Windows program: Windows' own answer (Authenticode), asked in-process.

Used by the updater: once Doty itself is code-signed, it only installs an update signed by the same
publisher, so a download from a tampered release page is refused even when its checksum "matches".

`publisher(path)` returns the name on the file's signature ("Contoso Ltd") when Windows says the signature
is valid and chains to a root it trusts, else "". It never shows a prompt and never starts another process
(no PowerShell, no signtool). Revocation isn't looked up: that needs the network, and an offline PC must
still be able to tell that its own Doty.exe is signed.
"""

from __future__ import annotations

import sys
from pathlib import Path

WTD_UI_NONE = 2
WTD_REVOKE_NONE = 0
WTD_CHOICE_FILE = 1
WTD_STATEACTION_VERIFY = 1
WTD_STATEACTION_CLOSE = 2
WTD_CACHE_ONLY_URL_RETRIEVAL = 0x1000  # use what's cached; don't go online for this
CERT_NAME_SIMPLE_DISPLAY_TYPE = 4


def publisher(path: str | Path) -> str:
    """The signer's name if `path` carries a valid, trusted code signature, else ""."""
    if sys.platform != "win32":
        return ""
    try:
        if not Path(path).is_file():
            return ""
        return _publisher(str(Path(path).resolve()))
    except (OSError, ValueError):
        return ""


def _publisher(path: str) -> str:
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD), ("d3", wintypes.WORD), ("d4", ctypes.c_ubyte * 8)]

    class FileInfo(ctypes.Structure):  # WINTRUST_FILE_INFO
        _fields_ = [("cbStruct", wintypes.DWORD), ("pcwszFilePath", wintypes.LPCWSTR),
                    ("hFile", wintypes.HANDLE), ("pgKnownSubject", ctypes.c_void_p)]

    class TrustData(ctypes.Structure):  # WINTRUST_DATA
        _fields_ = [("cbStruct", wintypes.DWORD), ("pPolicyCallbackData", ctypes.c_void_p),
                    ("pSIPClientData", ctypes.c_void_p), ("dwUIChoice", wintypes.DWORD),
                    ("fdwRevocationChecks", wintypes.DWORD), ("dwUnionChoice", wintypes.DWORD),
                    ("pFile", ctypes.POINTER(FileInfo)), ("dwStateAction", wintypes.DWORD),
                    ("hWVTStateData", wintypes.HANDLE), ("pwszURLReference", wintypes.LPCWSTR),
                    ("dwProvFlags", wintypes.DWORD), ("dwUIContext", wintypes.DWORD),
                    ("pSignatureSettings", ctypes.c_void_p)]

    class ProviderCert(ctypes.Structure):  # the start of CRYPT_PROVIDER_CERT
        _fields_ = [("cbStruct", wintypes.DWORD), ("pCert", ctypes.c_void_p)]

    class ProviderSigner(ctypes.Structure):  # the start of CRYPT_PROVIDER_SGNR
        _fields_ = [("cbStruct", wintypes.DWORD), ("sftVerifyAsOf", wintypes.FILETIME),
                    ("csCertChain", wintypes.DWORD), ("pasCertChain", ctypes.POINTER(ProviderCert))]

    wintrust = ctypes.WinDLL("wintrust", use_last_error=True)
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    wintrust.WinVerifyTrust.argtypes = [wintypes.HANDLE, ctypes.POINTER(GUID), ctypes.POINTER(TrustData)]
    wintrust.WinVerifyTrust.restype = wintypes.LONG
    wintrust.WTHelperProvDataFromStateData.argtypes = [wintypes.HANDLE]
    wintrust.WTHelperProvDataFromStateData.restype = ctypes.c_void_p
    wintrust.WTHelperGetProvSignerFromChain.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    wintrust.WTHelperGetProvSignerFromChain.restype = ctypes.POINTER(ProviderSigner)
    crypt32.CertGetNameStringW.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                           wintypes.LPWSTR, wintypes.DWORD]
    crypt32.CertGetNameStringW.restype = wintypes.DWORD

    # WINTRUST_ACTION_GENERIC_VERIFY_V2: the ordinary Authenticode policy.
    action = GUID(0x00AAC56B, 0xCD44, 0x11D0, (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE))
    file_info = FileInfo(ctypes.sizeof(FileInfo), path, None, None)
    data = TrustData()
    data.cbStruct = ctypes.sizeof(TrustData)
    data.dwUIChoice = WTD_UI_NONE
    data.fdwRevocationChecks = WTD_REVOKE_NONE
    data.dwUnionChoice = WTD_CHOICE_FILE
    data.pFile = ctypes.pointer(file_info)
    data.dwStateAction = WTD_STATEACTION_VERIFY
    data.dwProvFlags = WTD_CACHE_ONLY_URL_RETRIEVAL
    no_window = wintypes.HANDLE(-1)  # INVALID_HANDLE_VALUE: there is no user to ask

    name = ""
    trusted = wintrust.WinVerifyTrust(no_window, ctypes.byref(action), ctypes.byref(data)) == 0
    try:
        if trusted and data.hWVTStateData:
            prov = wintrust.WTHelperProvDataFromStateData(data.hWVTStateData)
            signer = wintrust.WTHelperGetProvSignerFromChain(prov, 0, False, 0) if prov else None
            if signer and signer.contents.csCertChain and signer.contents.pasCertChain:
                cert = signer.contents.pasCertChain[0].pCert
                if cert:
                    buf = ctypes.create_unicode_buffer(512)
                    crypt32.CertGetNameStringW(cert, CERT_NAME_SIMPLE_DISPLAY_TYPE, 0, None, buf, len(buf))
                    name = buf.value.strip()
    finally:
        data.dwStateAction = WTD_STATEACTION_CLOSE
        wintrust.WinVerifyTrust(no_window, ctypes.byref(action), ctypes.byref(data))
    return name
