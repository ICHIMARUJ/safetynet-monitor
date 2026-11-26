"""거래 단위 묶기 + 실수별 손익 테스트."""
from __future__ import annotations

from monitor.models import Fill
from monitor.store import Violation
from monitor.trades import build_trades, cost_summary, link_violations

_n = [0]


def f(ts, side, sz, start, pnl=0.0, fee=0.0, coin="ZEC", exch="bybit", action="open"):
    _n[0] += 1
    return Fill(exchange=exch, coin=coin, tid=str(_n[0]), oid=f"o{ts}", ts=ts, side=side, px=100, sz=sz,
                action=action, position_side=None, start_position=start, closed_pnl=pnl, fee=fee)


def test_same_ms_fills_out_of_order_form_one_trade():
    # 같은 ms 에 순서가 섞인 숏 진입 체결 (실제 Bybit ZEC 패턴), 이후 두 번에 나눠 청산
    fills = [f(1000, "sell", 0.32, -10.72), f(1000, "sell", 10.72, 0.0), f(1000, "sell", 2.61, -11.05),
             f(1000, "sell", 0.01, -11.04),
             f(5000, "buy", 5.0, -13.66, pnl=-30, action="close"),
             f(9000, "buy", 8.66, -8.66, pnl=-20, action="close")]
    ts = build_trades(fills)
    assert len(ts) == 1
    t = ts[0]
    assert t.side == "short" and t.start_ts == 1000 and t.end_ts == 9000 and t.pnl == -50


def test_flip_closes_and_opens():
    fills = [f(1, "buy", 1, 0), f(2, "sell", 3, 1, pnl=10, action="flip"), f(3, "buy", 2, -2, pnl=-5, action="close")]
    ts = build_trades(fills)
    assert [(t.side, t.pnl, t.end_ts) for t in ts] == [("long", 10, 2), ("short", -5, 3)]


def test_open_trade_and_fees():
    fills = [f(1, "buy", 1, 0, fee=0.5, exch="hyperliquid"), f(2, "sell", 1, 1, pnl=3, fee=0.5, exch="hyperliquid"),
             f(3, "buy", 2, 0, exch="hyperliquid")]
    ts = build_trades(fills)
    assert ts[0].pnl == 2.0 and ts[0].closed and not ts[1].closed


def test_link_and_cost():
    fills = [f(1_000, "buy", 1, 0), f(100_000, "sell", 1, 1, pnl=-80, action="close"),          # 실수 거래
             f(200_000, "buy", 1, 0), f(300_000, "sell", 1, 1, pnl=50, action="close"),         # 깨끗한 거래
             f(400_000, "buy", 1, 0, coin="SOL"), f(500_000, "sell", 1, 1, pnl=-20, coin="SOL", action="close")]
    viols = [Violation(rule="no_stop", message="", penalty=15, ts=60_000, exchange="bybit", coin="ZEC"),
             Violation(rule="leverage", message="", penalty=10, ts=50_000, exchange="bybit", coin="ZEC"),
             Violation(rule="revenge", message="", penalty=20, ts=400_000, exchange="bybit", coin="SOL")]
    ts = build_trades(fills)
    link_violations(ts, viols)
    c = cost_summary(ts, 0, 10**7, ["no_stop", "revenge", "oversize"])
    assert c["clean"] == {"n": 1, "pnl": 50.0, "avg": 50.0, "win_rate": 100.0}
    assert c["mistake"]["n"] == 2 and c["mistake"]["pnl"] == -100.0
    assert c["by_kind"]["no_stop"]["pnl"] == -80 and c["by_kind"]["oversize"]["pnl"] == -80
    assert c["by_kind"]["revenge"]["pnl"] == -20
    assert c["worst"][0]["coin"] == "ZEC" and c["worst"][0]["kinds"] == ["no_stop", "oversize"]
