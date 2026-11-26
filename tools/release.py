"""릴리스 파일 만들기: 업데이트 zip + version.json + update.json.

  python tools/release.py --repo OWNER/REPO --version 1.0.1 --notes "본전 알림 추가"

만드는 것
  dist/SafetyNet-<버전>.zip  : 코드만 (config.yaml·.env·data·alerts 그림은 절대 포함 안 함)
  dist/SafetyNet.zip         : 위와 같은 파일 (이모 첫 설치용 고정 주소 releases/latest/download/SafetyNet.zip)
  version.json               : 앱이 확인하는 최신 버전 정보 (저장소 main 에 올림)
  update.json, VERSION       : 앱에 들어가는 업데이트 주소·현재 버전
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from monitor.updater import allowed, parse_ver  # noqa: E402

FIXED_TIME = (2026, 1, 1, 0, 0, 0)   # zip 안 파일 시각 고정 → 같은 내용이면 같은 해시


def collect() -> list[str]:
    out = []
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT).as_posix()
        if rel.startswith(("dist/", "data/", ".git/")) or ".pytest_cache" in rel:
            continue
        if allowed(rel):
            out.append(rel)
    return out


def build(repo: str, version: str, notes: str) -> dict:
    if not repo or repo.count("/") != 1:
        raise SystemExit("--repo 는 OWNER/REPO 형식")
    old = (ROOT / "VERSION").read_text(encoding="utf-8").strip() if (ROOT / "VERSION").exists() else "0.0.0"
    if parse_ver(version) < parse_ver(old):
        raise SystemExit(f"버전이 현재({old})보다 낮음")
    (ROOT / "VERSION").write_text(version + "\n", encoding="utf-8")
    manifest_url = f"https://raw.githubusercontent.com/{repo}/main/version.json"
    (ROOT / "update.json").write_text(json.dumps({"manifest": manifest_url}, indent=2) + "\n", encoding="utf-8")

    files = collect()
    for must in ("app.py", "VERSION", "update.json", "requirements.txt", "install.bat", "SafetyNet.bat"):
        if must not in files:
            raise SystemExit(f"필수 파일 없음: {must}")
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    zpath = dist / f"SafetyNet-{version}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in files:
            info = zipfile.ZipInfo(rel, FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, (ROOT / rel).read_bytes())
    blob = zpath.read_bytes()
    (dist / "SafetyNet.zip").write_bytes(blob)
    manifest = {"version": version,
                "url": f"https://github.com/{repo}/releases/download/v{version}/SafetyNet-{version}.zip",
                "sha256": hashlib.sha256(blob).hexdigest(), "size": len(blob), "notes": notes,
                "date": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
    (ROOT / "version.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**manifest, "zip": str(zpath), "files": len(files)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--notes", default="")
    print(json.dumps(build(**vars(ap.parse_args())), ensure_ascii=False, indent=2))
