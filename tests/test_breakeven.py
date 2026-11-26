"""본전 복귀 알림 테스트 (롱·숏)."""
from __future__ import annotations

from monitor.breakeven import BreakevenWatch
from monitor.defaults import DEFAULTS, deep_merge
from monitor.models import Position


def P(side, entry, mark, exch="bybit", coin="ZEC", size=10):
    upnl = (mark - entry) * size * (1 if side == "long" else -1)
    return Position(exchange=exch, coin=coin, side=side, size=size, entry_px=entry, mark_px=mark,
                    notional=size * mark, upnl=upnl, liq_px=None, margin_mode="cross",
                    leverage_setting=10, margin_used=0)


def W():
    return BreakevenWatch(deep_merge(DEFAULTS, {}))


def test_long_loss_then_back_to_entry():
    w = W()
    assert w.update([P("long", 100, 99.5)]) == []          # -0.5% : 아직 손실 구간 아님
    assert w.update([P("long", 100, 98.8)]) == []          # -1.2% : 손실 구간 진입(무장)
    assert w.update([P("long", 100, 98.0)]) == []          # 더 내려감 (최악 -2%)
    ev = w.update([P("long", 100, 99.96)])                  # 평단 -0.04% 이내 → 알림
    assert len(ev) == 1 and ev[0].side == "long" and ev[0].worst_pct == 2.0 and ev[0].worst_upnl == -20
    assert w.update([P("long", 100, 100.5)]) == []          # 한 번만
    assert w.update([P("long", 100, 99.95)]) == []          # 본전 근처 왕복은 알림 없음


def test_short_loss_then_back_and_rearm():
    w = W()
    w.update([P("short", 700, 712)])                        # 숏 -1.7% (가격 상승)
    ev = w.update([P("short", 700, 700.2)])                 # 평단 +0.03% → 알림
    assert len(ev) == 1 and ev[0].side == "short"
    w.update([P("short", 700, 710)])                        # 다시 -1.4% → 재무장
    assert len(w.update([P("short", 700, 699)])) == 1


def test_profit_only_never_alerts_and_averaging_uses_new_entry():
    w = W()
    assert w.update([P("long", 100, 105)]) == [] and w.update([P("long", 100, 100)]) == []
    w.update([P("long", 100, 97)])                          # -3% 무장
    assert w.update([P("long", 98.5, 98.0)]) == []          # 물타기로 평단 98.5, 아직 아래
    assert len(w.update([P("long", 98.5, 98.5)])) == 1      # 새 평단 복귀 시 알림


def test_state_kept_when_exchange_poll_fails_and_cleared_when_closed():
    w = W()
    w.update([P("long", 100, 98)], {"bybit"})
    w.update([], {"hyperliquid"})                            # bybit 조회 실패 → 상태 유지
    assert w.state["bybit:ZEC:long"]["armed"]
    w.update([], {"bybit"})                                  # bybit 조회 성공, 포지션 없음 → 삭제
    assert w.state == {}


def test_disabled():
    w = BreakevenWatch(deep_merge(DEFAULTS, {"rules": {"breakeven": {"enabled": False}}}))
    w.update([P("long", 100, 90)])
    assert w.update([P("long", 100, 100)]) == []
