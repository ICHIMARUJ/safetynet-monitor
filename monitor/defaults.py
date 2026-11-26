"""모든 설정의 기본값.

업데이트 원칙
- 사용자 config.yaml 에 적힌 값은 언제나 우선한다 (업데이트로 덮어쓰지 않음).
- 새 기능이 추가되며 생긴 설정 키는 사용자 config.yaml 에 없어도 여기 기본값으로 동작한다.
  → 상대 PC 에 코드만 업데이트해도 새 기능이 켜지고, 상대가 정한 경고 기준은 그대로 유지된다.
"""
from __future__ import annotations

import copy
from typing import Any

DEFAULTS: dict[str, Any] = {
    "poll_interval_sec": 15,
    "data_dir": "data",                 # 기록 DB 위치 (프로그램 폴더 기준 상대경로 또는 절대경로)
    "exchanges": {
        "hyperliquid": {"enabled": False, "dexes": [""]},
        "bybit": {"enabled": False},
    },
    "rules": {
        "leverage": {"default_max": 5, "per_coin": {}, "account_max": 20},
        "stop_loss_grace_min": 3,
        "stop_breach_grace_sec": 60,
        "averaging_down": True,
        "liq_distance_min_pct": 10,
        "max_risk_per_trade_pct": 2,
        "revenge_cooldown_min": 15,
        "overtrading": {"lookback_days": 30, "sigma": 2, "min_trades_floor": 10},
        "size_spike_multiple": 2,
        "daily_loss_limit_pct": 5,
        # 계획 진입가와 실제 체결가가 이 범위(%) 안이면 "계획된 분할 진입"으로 인정
        "plan_entry_tolerance_pct": 0.5,
        # FOMO: 계획 없이, 진입 전 window_min 분 동안 move_pct % 이상 움직인 방향으로 추격 진입
        "fomo": {"window_min": 15, "move_pct": 5},
        # 뇌동매매: events.yaml 의 지표 발표 before_min 분 전 ~ after_min 분 후 진입
        "event_entry": {"before_min": 30, "after_min": 5, "extra": []},
        # 승리 후 과신: 최근 청산 win_streak 건 연속 수익 직후, 평소의 size_multiple 배 넘는 진입
        "overconfident": {"win_streak": 3, "size_multiple": 1.5},
        # 본전 알림(감점 없음): 평단 대비 min_loss_pct % 이상 불리했다가 평단 ± tolerance_pct % 로 돌아오면 알림
        "breakeven": {"enabled": True, "min_loss_pct": 1.0, "tolerance_pct": 0.05},
    },
    "ui": {"theme": "akira"},
    "update": {"auto": True},           # 새 버전이 있으면 자동 설치 후 재시작 (False: 확인만)           # akira | lime | ocean (앱의 테마 메뉴에서 바꾸면 data 폴더에 저장)
    "score": {
        "start": 100,
        # 위반 1건당 차감 점수
        "penalty": {
            "leverage": 10,
            "account_leverage": 10,
            "no_stop": 15,
            "stop_moved": 20,
            "stop_removed": 20,
            "stop_breach": 25,
            "averaging_down": 15,
            "liq_distance": 15,
            "risk_per_trade": 10,
            "revenge": 20,
            "overtrading": 10,
            "size_spike": 10,
            "daily_loss_limit": 25,
            "liquidation": 30,
            "fomo": 10,
            "event_entry": 10,
            "overconfident": 10,
            "rest_day_entry": 20,
        },
    },
}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """base 를 복사한 뒤 override 값으로 덮는다. dict 는 재귀적으로 합침."""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out
