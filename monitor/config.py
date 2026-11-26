"""config.yaml 과 .env 를 읽어오는 모듈.

사용자 config.yaml 은 defaults.py 의 기본값 위에 합쳐진다.
config.yaml 에 없는 항목은 기본값을 쓰고, 적힌 항목은 그대로 유지된다.
"""
from __future__ import annotations

import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

import yaml
from dotenv import load_dotenv

from monitor.defaults import DEFAULTS, deep_merge

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: Path | str | None = None, env_path: Path | str | None = None) -> dict[str, Any]:
    cfg_path = Path(path) if path else ROOT / "config.yaml"
    try:
        with open(cfg_path, encoding="utf-8") as f:
            user_cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:   # 설치 직후 config.yaml 이 없으면 기본값으로 동작
        user_cfg = {}
    cfg = deep_merge(DEFAULTS, user_cfg)

    load_dotenv(Path(env_path) if env_path else ROOT / ".env")
    cfg["secrets"] = {
        "hl_wallet": os.getenv("HL_WALLET", "").strip(),
        "bybit_key": os.getenv("BYBIT_API_KEY", "").strip(),
        "bybit_secret": os.getenv("BYBIT_API_SECRET", "").strip(),
        "telegram_token": os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID", "").strip(),
    }
    # 앱 설정 화면에서 넣은 키 (Windows 암호화 보관). .env 에 없는 항목만 채움
    from monitor import secretstore
    for k, v in secretstore.load(data_dir(cfg)).items():
        if not cfg["secrets"].get(k):
            cfg["secrets"][k] = v
    return cfg


def data_dir(cfg: dict[str, Any]) -> Path:
    p = Path(cfg.get("data_dir", "data"))
    d = p if p.is_absolute() else ROOT / p
    d.mkdir(parents=True, exist_ok=True)
    return d


def max_leverage_for(cfg: dict[str, Any], coin: str) -> float:
    """코인별 레버리지 한도. HIP-3 코인("xyz:TSLA")은 접두어를 떼고 찾는다."""
    lev = cfg.get("rules", {}).get("leverage", {})
    per_coin = {str(k).upper(): v for k, v in (lev.get("per_coin") or {}).items()}
    base = coin.split(":")[-1].upper()
    return float(per_coin.get(base, lev.get("default_max", 5)))


def load_events(cfg: dict[str, Any] | None = None, path: Path | str | None = None) -> list[tuple[int, str]]:
    """events.yaml + config 의 rules.event_entry.extra → [(발표 시각 ms, 이름)] 시각순.
    형식: {at: "YYYY-MM-DD HH:MM" (UTC), name: "이름"}"""
    p = Path(path) if path else ROOT / "events.yaml"
    items: list = []
    if p.exists():
        with open(p, encoding="utf-8") as f:
            items += (yaml.safe_load(f) or {}).get("events") or []
    if cfg:
        items += ((cfg.get("rules") or {}).get("event_entry") or {}).get("extra") or []
    out: list[tuple[int, str]] = []
    for it in items:
        try:
            t = datetime.strptime(str(it["at"]).strip(), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except (KeyError, ValueError, TypeError):
            continue
        out.append((int(t.timestamp() * 1000), str(it.get("name") or "지표 발표")))
    return sorted(out)
