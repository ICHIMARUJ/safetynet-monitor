"""체결 → 거래(포지션을 0 에서 열어 다시 0 으로 닫기까지) 단위로 묶고, 경고와 연결해 실수별 손익을 계산한다.

같은 시각(ms)에 체결 수십 건이 순서 없이 들어오므로, 같은 시각 묶음마다
'묶음 시작 포지션'(다른 체결의 직후 포지션과 겹치지 않는 시작값)과 순변화량으로 포지션 흐름을 복원한다.
손익 = 청산손익 − 수수료 (Bybit 은 청산손익이 이미 수수료 차감 후라 수수료 0).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from itertools import groupby
from typing import Optional

from monitor.mistakes import kind_of
from monitor.models import Fill
from monitor.store import Violation

LINK_SLACK_MS = 60_000   # 경고 시각과 거래 시작·끝의 허용 오차


@dataclass
class Trade:
    exchange: str
    coin: str
    side: str
    start_ts: int
    end_ts: Optional[int] = None          # None = 아직 열려 있음
    pnl: float = 0.0
    kinds: set = field(default_factory=set)
    rules: set = field(default_factory=set)

    @property
    def closed(self) -> bool:
        return self.end_ts is not None


def _signed(f: Fill) -> float:
    return f.sz if f.side == "buy" else -f.sz


def _flat(x: float, scale: float) -> bool:
    return abs(x) <= 1e-9 + 1e-7 * scale


def build_trades(fills: list[Fill]) -> list[Trade]:
    by_key: dict[tuple, list[Fill]] = defaultdict(list)
    for f in fills:
        by_key[(f.exchange, f.coin)].append(f)
    out: list[Trade] = []
    for (exch, coin), fs in by_key.items():
        fs.sort(key=lambda f: f.ts)
        cur: Optional[Trade] = None
        for ts, grp in groupby(fs, key=lambda f: f.ts):
            g = list(grp)
            scale = max([abs(f.start_position) for f in g] + [f.sz for f in g] + [1.0])
            posts = [f.start_position + _signed(f) for f in g]
            heads = [f.start_position for f in g if not any(_flat(f.start_position - p, scale) for p in posts)]
            start = heads[0] if heads else min((f.start_position for f in g), key=abs)
            net = sum(_signed(f) for f in g)
            end = start + net
            pnl = sum(f.closed_pnl - f.fee for f in g)

            if cur is None and not _flat(start, scale):
                # 기록 시작 전에 열린 포지션: 기록 구간부터 하나의 거래로 본다
                cur = Trade(exch, coin, "long" if start > 0 else "short", ts)
            if cur is None:
                if _flat(end, scale):
                    continue
                cur = Trade(exch, coin, "long" if end > 0 else "short", ts)
                cur.pnl += pnl
                continue
            flipped = not _flat(end, scale) and (end > 0) != (cur.side == "long")
            cur.pnl += pnl
            if _flat(end, scale) or flipped:
                cur.end_ts = ts
                out.append(cur)
                cur = Trade(exch, coin, "long" if end > 0 else "short", ts) if flipped else None
        if cur is not None:
            out.append(cur)
    out.sort(key=lambda t: t.start_ts)
    return out


def link_violations(trades: list[Trade], viols: list[Violation]) -> None:
    """경고를 그 경고가 걸린 거래에 붙인다."""
    by_key: dict[tuple, list[Trade]] = defaultdict(list)
    by_exch: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        by_key[(t.exchange, t.coin)].append(t)
        by_exch[t.exchange].append(t)

    def open_at(t: Trade, ts: int) -> bool:
        return t.start_ts - LINK_SLACK_MS <= ts and (t.end_ts is None or ts <= t.end_ts + LINK_SLACK_MS)

    for v in viols:
        k = kind_of(v.rule)
        targets: list[Trade] = []
        if v.exchange and v.coin:
            cands = [t for t in by_key.get((v.exchange, v.coin), []) if open_at(t, v.ts)]
            if cands:
                targets = [max(cands, key=lambda t: t.start_ts)]
        elif v.rule == "overtrading":          # 과매매 경고 이후 그날 새로 연 거래들
            day_end = v.ts - v.ts % 86_400_000 + 86_400_000
            targets = [t for t in trades if v.ts - 1000 <= t.start_ts < day_end]
        elif v.exchange:                       # 계좌 레버리지 등: 그때 열려 있던 거래들
            targets = [t for t in by_exch.get(v.exchange, []) if open_at(t, v.ts)]
        for t in targets:
            t.kinds.add(k)
            t.rules.add(v.rule)


def _stats(ts: list[Trade]) -> dict:
    n = len(ts)
    pnl = sum(t.pnl for t in ts)
    wins = sum(1 for t in ts if t.pnl > 0)
    return {"n": n, "pnl": round(pnl, 2), "avg": round(pnl / n, 2) if n else None,
            "win_rate": round(wins / n * 100, 1) if n else None}


def cost_summary(trades: list[Trade], start: int, end: int, kinds: list[str]) -> dict:
    """기간 안에 끝난 거래 기준: 실수 없는 거래 vs 실수 거래, 실수 유형별 손익, 가장 비싼 실수 거래."""
    closed = [t for t in trades if t.closed and start <= t.end_ts < end]
    clean = [t for t in closed if not t.kinds]
    dirty = [t for t in closed if t.kinds]
    by_kind = {k: _stats([t for t in closed if k in t.kinds]) for k in kinds}
    worst = sorted(dirty, key=lambda t: t.pnl)[:5]
    return {"all": _stats(closed), "clean": _stats(clean), "mistake": _stats(dirty), "by_kind": by_kind,
            "worst": [{"exchange": t.exchange, "coin": t.coin, "side": t.side, "start": t.start_ts,
                       "end": t.end_ts, "pnl": round(t.pnl, 2), "kinds": sorted(t.kinds)} for t in worst if t.pnl < 0]}
