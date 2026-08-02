import base64
import ctypes
import ctypes.wintypes
import json
import os
import sys


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _dpapi_available() -> bool:
    return sys.platform == "win32"


def _blob_from_bytes(data: bytes):
    buffer = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _bytes_from_blob(blob: DATA_BLOB) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob.pbData)


def protect_bytes(data: bytes) -> bytes | None:
    if not _dpapi_available():
        return None

    in_blob, in_buffer = _blob_from_bytes(data)
    out_blob = DATA_BLOB()
    try:
        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(out_blob),
        )
        if not ok:
            return None
        return _bytes_from_blob(out_blob)
    finally:
        # Keep the input buffer alive until CryptProtectData returns.
        _ = in_buffer


def unprotect_bytes(data: bytes) -> bytes | None:
    if not _dpapi_available():
        return None

    in_blob, in_buffer = _blob_from_bytes(data)
    out_blob = DATA_BLOB()
    try:
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(out_blob),
        )
        if not ok:
            return None
        return _bytes_from_blob(out_blob)
    finally:
        _ = in_buffer


def normalize_cookie_data(data: dict, keys=("qidian", "netease")) -> dict:
    return {key: (data.get(key) or "").strip() for key in keys}


def read_plain_cookie_file(path: str, keys=("qidian", "netease")) -> dict:
    if not os.path.exists(path):
        return normalize_cookie_data({}, keys)
    with open(path, "r", encoding="utf-8-sig") as f:
        return normalize_cookie_data(json.load(f), keys)


def write_plain_cookie_file(path: str, data: dict) -> bool:
    try:
        dir_path = os.path.dirname(os.path.abspath(path))
        if dir_path and not os.path.isdir(dir_path):
            os.makedirs(dir_path, exist_ok=True)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_path, path)
        return True
    except Exception:
        return False


def read_encrypted_cookie_file(path: str, keys=("qidian", "netease")) -> dict | None:
    try:
        if not os.path.exists(path):
            return None
        payload = json.loads(open(path, "r", encoding="utf-8").read())
        if payload.get("format") != "dpapi-v1":
            return None
        encrypted = base64.b64decode(payload.get("data") or "")
        decrypted = unprotect_bytes(encrypted)
        if not decrypted:
            return None
        return normalize_cookie_data(json.loads(decrypted.decode("utf-8")), keys)
    except Exception:
        return None


def write_encrypted_cookie_file(path: str, data: dict) -> bool:
    try:
        encrypted = protect_bytes(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        if not encrypted:
            return False
        payload = {
            "format": "dpapi-v1",
            "platform": "windows",
            "data": base64.b64encode(encrypted).decode("ascii"),
        }
        return write_plain_cookie_file(path, payload)
    except Exception:
        return False
