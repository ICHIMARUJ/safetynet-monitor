"""실수 기록 탭에 보여줄 집계.

- 기간 비교: 최근 30일 / 1달 전(30~60일) / 2달 전(60~90일)
- 월별: 최근 12개월 유형별 건수
- 일별 점수: 최근 90일 중 매매했거나 경고가 있던 날
- 목록: 최근 경고 300건
실수가 줄었는지는 건수만이 아니라 "진입 10회당 실수"로도 본다 (매매를 덜 해서 줄어든 것과 구분).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from monitor.mistakes import KINDS, RULE_LABEL, kind_of
from monitor.store import Store
from monitor.trades import build_trades, cost_summary, link_violations

DAY_MS = 86_400_000


def _month(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m")


def _day(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _months_back(now: int, n: int) -> list[str]:
    d = datetime.fromtimestamp(now / 1000, tz=timezone.utc)
    y, m = d.year, d.month
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


def summary(cfg: dict[str, Any], store: Store, now: int) -> dict[str, Any]:
    start_score = int(cfg["score"]["start"])
    since = now - 400 * DAY_MS
    viols = store.violations_between(since)
    fills = store.fills_between(since)

    # 진입 횟수 (주문 단위)
    entries: dict[str, set] = defaultdict(set)      # 일자 → {(거래소, 주문)}
    for f in fills:
        if f.action in ("open", "flip"):
            entries[_day(f.ts)].add((f.exchange, f.oid))
    entry_ts: dict[tuple, int] = {}
    for f in fills:
        if f.action in ("open", "flip"):
            entry_ts.setdefault((f.exchange, f.oid), f.ts)

    def window(start: int, end: int) -> dict[str, Any]:
        vs = [v for v in viols if start <= v.ts < end]
        by_kind = {k: 0 for k in KINDS}
        for v in vs:
            by_kind[kind_of(v.rule)] += 1
        n_entries = sum(1 for t in entry_ts.values() if start <= t < end)
        days: dict[str, int] = defaultdict(int)
        for v in vs:
            days[_day(v.ts)] += v.penalty
        active = {_day(t) for t in entry_ts.values() if start <= t < end} | set(days)
        avg = (sum(max(0, start_score - days.get(d, 0)) for d in active) / len(active)) if active else None
        mistakes = sum(c for k, c in by_kind.items())
        return {"total": mistakes, "by_kind": by_kind, "entries": n_entries,
                "per10": round(mistakes / n_entries * 10, 2) if n_entries else None,
                "avg_score": round(avg, 1) if avg is not None else None,
                "active_days": len(active), "penalty": sum(v.penalty for v in vs)}

    trades = build_trades(fills)
    link_violations(trades, viols)
    kinds_list = list(KINDS)
    spans = [("최근 30일", now - 30 * DAY_MS, now + 1), ("1달 전", now - 60 * DAY_MS, now - 30 * DAY_MS),
             ("2달 전", now - 90 * DAY_MS, now - 60 * DAY_MS)]
    periods = [{"label": lb, **window(a, b), "cost": cost_summary(trades, a, b, kinds_list)} for lb, a, b in spans]

    months = _months_back(now, 12)
    monthly = {m: {"month": m, "by_kind": {k: 0 for k in KINDS}, "total": 0, "entries": 0} for m in months}
    for v in viols:
        m = monthly.get(_month(v.ts))
        if m:
            m["by_kind"][kind_of(v.rule)] += 1
            m["total"] += 1
    for t in entry_ts.values():
        m = monthly.get(_month(t))
        if m:
            m["entries"] += 1

    daily_pen: dict[str, int] = defaultdict(int)
    daily_cnt: dict[str, int] = defaultdict(int)
    for v in viols:
        if v.ts >= now - 90 * DAY_MS:
            daily_pen[_day(v.ts)] += v.penalty
            daily_cnt[_day(v.ts)] += 1
    act_days = sorted({d for d in entries if d >= _day(now - 90 * DAY_MS)} | set(daily_pen))
    daily = [{"day": d, "score": max(0, start_score - daily_pen.get(d, 0)),
              "mistakes": daily_cnt.get(d, 0), "entries": len(entries.get(d, ()))} for d in act_days]

    log = [{"id": v.id, "ts": v.ts, "rule": v.rule, "rule_label": RULE_LABEL.get(v.rule, v.rule),
            "kind": kind_of(v.rule), "message": v.message, "penalty": v.penalty,
            "exchange": v.exchange, "coin": v.coin, "retro": v.source == "retro"}
           for v in reversed(viols[-300:])]

    return {"now": now, "periods": periods, "monthly": [monthly[m] for m in months],
            "daily": daily, "log": log, "since": store.first_fill_ts(),
            "kinds": {k: {"label": d["label"], "accent": "#D9D4E6" if k == "liquidation" else d["accent"]}
                      for k, d in KINDS.items()},
            "retro": store.get_kv("retro_done_v1")}
