"""Windows Credential Manager storage. Secrets never live in the project tree.

This module has no import-time credential access. Generic credentials are protected
by Windows for the signed-in user and persist locally, without source/backup files.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import getpass
import os


DEEPSEEK_TARGET = "MYATRI/DeepSeek/api.deepseek.com"
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168


class CredentialError(RuntimeError):
    pass


class CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(wintypes.BYTE)),
        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _wincred():
    if os.name != "nt":
        raise CredentialError("Windows Credential Manager is only available on Windows; use a process environment variable elsewhere")
    api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    api.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
    api.CredReadW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    api.CredDeleteW.restype = wintypes.BOOL
    return api


def save_deepseek_key(secret: str) -> None:
    if not secret.startswith("sk-") or len(secret) < 20 or any(c.isspace() for c in secret):
        raise CredentialError("invalid key format")
    api = _wincred()
    encoded = bytearray(secret.encode("utf-8"))
    buffer = (wintypes.BYTE * len(encoded)).from_buffer(encoded)
    credential = CREDENTIALW(Type=CRED_TYPE_GENERIC, TargetName=DEEPSEEK_TARGET,
        Comment="MYATRI official DeepSeek API; kept outside source and archives",
        CredentialBlobSize=len(encoded), CredentialBlob=buffer, Persist=CRED_PERSIST_LOCAL_MACHINE,
        UserName="MYATRI")
    try:
        if not api.CredWriteW(ctypes.byref(credential), 0):
            raise CredentialError(f"Windows credential write failed (code {ctypes.get_last_error()})")
    finally:
        for i in range(len(encoded)):
            encoded[i] = 0


def read_deepseek_key() -> str | None:
    if os.name != "nt":
        return None
    api = _wincred()
    pointer = ctypes.POINTER(CREDENTIALW)()
    if not api.CredReadW(DEEPSEEK_TARGET, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
        error = ctypes.get_last_error()
        if error == ERROR_NOT_FOUND:
            return None
        raise CredentialError(f"Windows credential read failed (code {error})")
    try:
        record = pointer.contents
        return ctypes.string_at(record.CredentialBlob, record.CredentialBlobSize).decode("utf-8")
    finally:
        api.CredFree(pointer)


def delete_deepseek_key() -> None:
    api = _wincred()
    if not api.CredDeleteW(DEEPSEEK_TARGET, CRED_TYPE_GENERIC, 0):
        error = ctypes.get_last_error()
        if error != ERROR_NOT_FOUND:
            raise CredentialError(f"Windows credential delete failed (code {error})")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Local Windows credentials; never prints the key")
    parser.add_argument("action", choices=["store-deepseek", "status", "delete-deepseek"])
    args = parser.parse_args()
    if args.action == "store-deepseek":
        # Require a terminal; never fall back to echoing input in redirected stdin.
        import sys
        if not sys.stdin.isatty():
            raise CredentialError("credential entry requires an interactive terminal")
        value = getpass.getpass("DeepSeek API key (hidden): ")
        save_deepseek_key(value)
        if read_deepseek_key() != value:
            raise CredentialError("credential read-back verification failed")
        print("Stored and verified in Windows Credential Manager; no project file written.")
    elif args.action == "status":
        print("configured" if read_deepseek_key() else "not configured")
    else:
        delete_deepseek_key()
        print("deleted")


if __name__ == "__main__":
    main()
