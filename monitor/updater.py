"""자동 업데이트 (GitHub 공개 저장소).

흐름: version.json 확인 → 새 버전이면 zip 다운로드 → sha256 확인 → 바뀔 파일 백업 → 코드만 교체
      → requirements.txt 가 바뀌었으면 패키지 설치 → (앱이) 재시작.
절대 건드리지 않는 것: config.yaml(각자의 경고 기준), .env·data/(키·기록·설정), alerts/ 그림.
교체 도중 실패하면 백업으로 되돌린다.
"""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import requests

MAX_ZIP = 60 * 1024 * 1024
ALLOWED_FILES = {"app.py", "service.py", "run.py", "plan.py", "check.py", "check_hyperliquid.py", "conftest.py",
                 "VERSION", "update.json", "events.yaml", "requirements.txt", "README.md", "SafetyNet.bat",
                 "setup.bat", "install.bat", "config.example.yaml", ".env.example", "alerts/README.txt"}
ALLOWED_DIRS = ("monitor/", "ui/", "tests/", "tools/", "presets/")


def parse_ver(v: str) -> tuple[int, ...]:
    out = []
    for part in str(v).strip().lstrip("v").split("."):
        num = "".join(ch for ch in part if ch.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out + [0] * (3 - len(out)))[:3]


def allowed(name: str) -> bool:
    """zip 안 경로가 교체해도 되는 코드 파일인지 (상위 폴더 탈출·절대경로·보호 파일 차단)."""
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts or "\\" in name or ":" in name or name.endswith("/"):
        return False
    s = str(p)
    if "__pycache__" in p.parts or s.endswith(".pyc"):
        return False
    return s in ALLOWED_FILES or s.startswith(ALLOWED_DIRS)


def read_manifest(path: Path) -> str:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("manifest", "").strip()
    except (OSError, ValueError):
        return ""


def check(manifest_url: str, current: str, session: Any = requests) -> dict[str, Any]:
    if not manifest_url:
        return {"ok": False, "error": "업데이트 주소가 설정되지 않음"}
    try:
        r = session.get(manifest_url, timeout=15, headers={"Cache-Control": "no-cache"})
        m = r.json()
    except (requests.RequestException, ValueError) as e:
        return {"ok": False, "error": f"업데이트 확인 실패: {e}"}
    if r.status_code != 200 or not m.get("version") or not m.get("url") or not m.get("sha256"):
        return {"ok": False, "error": f"업데이트 정보가 올바르지 않음 (HTTP {r.status_code})"}
    return {"ok": True, "current": current, "latest": m["version"], "notes": m.get("notes", ""),
            "url": m["url"], "sha256": m["sha256"].lower(),
            "available": parse_ver(m["version"]) > parse_ver(current)}


def default_pip(root: Path) -> tuple[bool, str]:
    """requirements.txt 설치. 창 모드(pythonw)여도 python.exe 로 실행."""
    exe = Path(sys.executable)
    py = exe.with_name("python.exe") if exe.name.lower() == "pythonw.exe" else exe
    flags = 0x08000000 if sys.platform == "win32" else 0      # 검은 창 안 띄움
    try:
        r = subprocess.run([str(py), "-m", "pip", "install", "-q", "-r", str(root / "requirements.txt")],
                           capture_output=True, text=True, timeout=600, creationflags=flags)
        return r.returncode == 0, (r.stderr or r.stdout)[-400:]
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)


def apply(info: dict[str, Any], root: Path, data_dir: Path, session: Any = requests,
          pip: Callable[[Path], tuple[bool, str]] = default_pip) -> dict[str, Any]:
    root, data_dir = Path(root), Path(data_dir)
    try:
        r = session.get(info["url"], timeout=120)
        blob = r.content
    except requests.RequestException as e:
        return {"ok": False, "error": f"다운로드 실패: {e}"}
    if getattr(r, "status_code", 200) != 200 or len(blob) > MAX_ZIP:
        return {"ok": False, "error": f"다운로드 실패 (HTTP {getattr(r, 'status_code', '?')}, {len(blob)} bytes)"}
    if hashlib.sha256(blob).hexdigest() != info["sha256"]:
        return {"ok": False, "error": "파일 확인값(sha256)이 다름 — 설치하지 않음"}
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        return {"ok": False, "error": "업데이트 파일이 손상됨"}
    names = [n for n in zf.namelist() if not n.endswith("/")]
    files = [n for n in names if allowed(n)]
    if "VERSION" not in files or "app.py" not in files:
        return {"ok": False, "error": "업데이트 파일 구성이 올바르지 않음"}

    old_req = (root / "requirements.txt").read_bytes() if (root / "requirements.txt").exists() else b""
    backup = data_dir / "backup" / (_version(root) or "unknown")
    if backup.exists():
        shutil.rmtree(backup)
    written: list[Path] = []
    created: list[Path] = []
    try:
        for n in files:
            dst = root / n
            if dst.exists():
                b = backup / n
                b.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst, b)
            else:
                created.append(dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(zf.read(n))
            written.append(dst)
    except Exception as e:  # noqa: BLE001 - 중간 실패 → 되돌리기
        for dst in written:
            rel = dst.relative_to(root)
            if (backup / rel).exists():
                shutil.copy2(backup / rel, dst)
            elif dst in created:
                dst.unlink(missing_ok=True)
        return {"ok": False, "error": f"교체 중 실패 → 원래대로 되돌림: {e}"}

    res: dict[str, Any] = {"ok": True, "version": info.get("latest") or _version(root), "files": len(files),
                           "skipped": sorted(set(names) - set(files)), "backup": str(backup)}
    if (root / "requirements.txt").read_bytes() != old_req:
        ok, out = pip(root)
        res.update(deps_installed=ok, deps_log=out)
    return res


def _version(root: Path) -> str:
    try:
        return (Path(root) / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
