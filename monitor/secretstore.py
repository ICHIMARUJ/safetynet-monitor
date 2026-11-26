"""거래소 키 보관 (data/secrets.bin).

Windows: DPAPI(CryptProtectData)로 암호화 — 같은 PC·같은 Windows 계정에서만 풀 수 있다.
그 외 OS(개발·테스트): 암호화 없이 base64 (표시만 'plain').
.env 에 값이 있으면 .env 가 우선이고, 여기 값은 .env 에 없는 항목만 채운다.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Any

FILE = "secrets.bin"
FIELDS = ("hl_wallet", "bybit_key", "bybit_secret")


def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    src = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(src), None, None, None, None, 0x01, ctypes.byref(out))   # 0x01 = UI 없음
    if not ok:
        raise OSError("Windows 암호화 처리 실패")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def save(data_dir: Path, values: dict[str, Any]) -> None:
    clean = {k: str(values.get(k) or "").strip() for k in FIELDS if str(values.get(k) or "").strip()}
    raw = json.dumps(clean).encode()
    if sys.platform == "win32":
        blob = {"v": 1, "kind": "dpapi", "data": base64.b64encode(_dpapi(raw, True)).decode()}
    else:
        blob = {"v": 1, "kind": "plain", "data": base64.b64encode(raw).decode()}
    p = Path(data_dir) / FILE
    p.write_text(json.dumps(blob), encoding="utf-8")


def load(data_dir: Path) -> dict[str, str]:
    p = Path(data_dir) / FILE
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
        raw = base64.b64decode(blob["data"])
        if blob.get("kind") == "dpapi":
            raw = _dpapi(raw, False)
        d = json.loads(raw.decode())
        return {k: str(d[k]) for k in FIELDS if d.get(k)}
    except (OSError, ValueError, KeyError):
        return {}


def mask(v: str) -> str:
    """화면 표시용: 앞 4자리만."""
    return (v[:4] + "•" * 8) if v else ""
