"""Hyperliquid 수집기 테스트. 실제 API 대신 문서 기준 샘플 응답을 사용한다.

실행: python -m pytest tests -q
"""
from __future__ import annotations

import pytest

from monitor.config import max_leverage_for
from monitor.exchanges.hyperliquid import (
    FILLS_PAGE_LIMIT, HyperliquidClient, HyperliquidError,
    parse_fills, parse_orders, parse_positions,
)

WALLET = "0x" + "ab" * 20

STATE = {
    "marginSummary": {"accountValue": "1000.0", "totalNtlPos": "15000.0",
                      "totalRawUsd": "0", "totalMarginUsed": "800"},
    "crossMarginSummary": {"accountValue": "1000.0"},
    "withdrawable": "200.0",
    "assetPositions": [
        {"type": "oneWay", "position": {
            "coin": "BTC", "szi": "0.1", "entryPx": "100000.0",
            "positionValue": "9800.0", "unrealizedPnl": "-200.0",
            "liquidationPx": "92000.0", "marginUsed": "490.0",
            "leverage": {"type": "cross", "value": 20}, "maxLeverage": 40}},
        {"type": "oneWay", "position": {
            "coin": "ETH", "szi": "-2.0", "entryPx": "2600.0",
            "positionValue": "5200.0", "unrealizedPnl": "0.0",
            "liquidationPx": None, "marginUsed": "260.0",
            "leverage": {"type": "isolated", "value": 20}, "maxLeverage": 25}},
        {"type": "oneWay", "position": {"coin": "SOL", "szi": "0.0",
                                        "positionValue": "0"}},
    ],
    "time": 1733968369395,
}

ORDERS = [
    # BTC 롱 손절 (현재가 98000 아래)
    {"coin": "BTC", "isPositionTpsl": True, "isTrigger": True, "limitPx": "95000.0",
     "oid": 1, "orderType": "Stop Market", "origSz": "0.0", "reduceOnly": True,
     "side": "A", "sz": "0.0", "timestamp": 1, "triggerCondition": "Price below 95000",
     "triggerPx": "95000.0"},
    # BTC 익절
    {"coin": "BTC", "isPositionTpsl": True, "isTrigger": True, "limitPx": "110000.0",
     "oid": 2, "orderType": "Take Profit Market", "origSz": "0.0", "reduceOnly": True,
     "side": "A", "sz": "0.0", "timestamp": 2, "triggerCondition": "Price above 110000",
     "triggerPx": "110000.0"},
    # ETH 일반 지정가 (손절 아님)
    {"coin": "ETH", "isPositionTpsl": False, "isTrigger": False, "limitPx": "2500.0",
     "oid": 3, "orderType": "Limit", "origSz": "1.0", "reduceOnly": False,
     "side": "B", "sz": "1.0", "timestamp": 3, "triggerCondition": "N/A",
     "triggerPx": "0.0"},
]

FILLS = [
    {"closedPnl": "0.0", "coin": "BTC", "crossed": True, "dir": "Open Long",
     "hash": "0x1", "oid": 10, "px": "100000.0", "side": "B", "startPosition": "0.0",
     "sz": "0.1", "time": 1000, "fee": "4.0", "feeToken": "USDC", "tid": 101},
    {"closedPnl": "-50.0", "coin": "ETH", "crossed": True, "dir": "Close Short",
     "hash": "0x2", "oid": 11, "px": "2625.0", "side": "B", "startPosition": "-3.0",
     "sz": "1.0", "time": 2000, "fee": "1.0", "feeToken": "USDC", "tid": 102},
    {"closedPnl": "10.0", "coin": "SOL", "crossed": True, "dir": "Long > Short",
     "hash": "0x3", "oid": 12, "px": "150.0", "side": "A", "startPosition": "5.0",
     "sz": "8.0", "time": 3000, "fee": "0.5", "feeToken": "USDC", "tid": 103},
]


class FakeResp:
    def __init__(self, data, status=200):
        self._data, self.status_code, self.text = data, status, str(data)

    def json(self):
        return self._data


# 실계정에서 확인한 통합 계정 형태: perp accountValue $154 / 현물 USDC $937.79
UNIFIED_SPOT = {"balances": [
    {"coin": "USDC", "token": 0, "total": "937.787208", "hold": "500.561764", "entryNtl": "0.0"},
    {"coin": "HYPE", "token": 150, "total": "2.0", "hold": "0.0", "entryNtl": "80.0"}],
    "tokenToAvailableAfterMaintenance": [[0, "664.182062"]]}


class FakeSession:
    """payload 의 type 에 따라 샘플 응답을 돌려주는 가짜 세션."""

    def __init__(self, fills_pages=None, mode="default", spot=None):
        self.calls = []
        self.fills_pages = list(fills_pages or [FILLS])
        self.mode, self.spot = mode, spot or UNIFIED_SPOT

    def post(self, url, json, timeout):
        self.calls.append(json)
        t = json["type"]
        if t == "clearinghouseState":
            return FakeResp(STATE)
        if t == "frontendOpenOrders":
            return FakeResp(ORDERS)
        if t == "userFillsByTime":
            return FakeResp(self.fills_pages.pop(0) if self.fills_pages else [])
        if t == "userAbstraction":
            return FakeResp(self.mode)
        if t == "spotClearinghouseState":
            return FakeResp(self.spot)
        return FakeResp({"error": "unknown"}, status=400)


# ------------------------------------------------------------------ 파싱
def test_parse_positions():
    ps = parse_positions(STATE)
    assert [p.coin for p in ps] == ["BTC", "ETH"]          # 수량 0 인 SOL 제외
    btc, eth = ps
    assert btc.side == "long" and btc.size == 0.1
    assert btc.mark_px == pytest.approx(98000.0)
    assert btc.liq_px == 92000.0
    assert btc.liq_distance_pct() == pytest.approx(6000 / 98000 * 100)
    assert btc.is_losing
    assert eth.side == "short" and eth.size == 2.0
    assert eth.liq_px is None and eth.liq_distance_pct() is None
    assert eth.margin_mode == "isolated"


def test_parse_orders_kinds():
    os_ = parse_orders(ORDERS)
    assert [o.kind for o in os_] == ["stop", "take_profit", "limit"]
    assert os_[0].side == "sell" and os_[0].trigger_px == 95000.0 and os_[0].size == 0
    assert os_[2].trigger_px is None


def test_parse_fills_actions():
    fs = parse_fills(FILLS)
    assert [(f.action, f.position_side) for f in fs] == [
        ("open", "long"), ("close", "short"), ("flip", "short")]
    assert fs[1].closed_pnl == -50.0 and fs[1].start_position == -3.0


# -------------------------------------------------------------- 스냅샷
def test_snapshot_and_stop_detection():
    client = HyperliquidClient(WALLET, session=FakeSession())
    snap = client.snapshot()
    assert snap.equity == 1000.0
    btc, eth = snap.positions
    assert snap.effective_leverage(btc) == pytest.approx(9.8)
    assert snap.effective_leverage() == pytest.approx(15.0)
    # BTC 는 손절 1개(익절은 제외), ETH 숏은 매수 지정가만 있어 손절 없음
    assert [o.oid for o in snap.stop_orders_for(btc)] == ["1"]
    assert snap.stop_orders_for(eth) == []


def test_stop_on_wrong_side_not_counted():
    """현재가 위에 있는 매도 Stop 은 롱 포지션의 손절로 보지 않는다."""
    bad = dict(ORDERS[0], triggerPx="99000.0", oid=9)
    client = HyperliquidClient(WALLET, session=FakeSession())
    snap = client.snapshot()
    snap.orders = parse_orders([bad])
    assert snap.stop_orders_for(snap.positions[0]) == []


def test_multiple_dexes_sum_equity():
    session = FakeSession()
    client = HyperliquidClient(WALLET, dexes=["", "xyz"], session=session)
    snap = client.snapshot()
    assert snap.equity == 2000.0
    assert [c.get("dex") for c in session.calls
            if c["type"] not in ("userAbstraction", "perpDexs")] == ["", "", "xyz", "xyz"]


# ---------------------------------------------------------------- 체결
def test_fills_pagination_and_dedupe():
    # 1페이지: 2000건 꽉 참(마지막 time=5000) → 2페이지: 마지막 건 중복 + 신규 1건
    page1 = [dict(FILLS[0], tid=i, time=1000 + i * 2) for i in range(FILLS_PAGE_LIMIT - 1)]
    page1.append(dict(FILLS[0], tid=99999, time=5000))
    page2 = [dict(FILLS[0], tid=99999, time=5000), dict(FILLS[0], tid=100000, time=6000)]
    session = FakeSession(fills_pages=[page1, page2])
    client = HyperliquidClient(WALLET, session=session)
    fills = client.fills_since(0)
    assert len(fills) == FILLS_PAGE_LIMIT + 1
    fill_calls = [c for c in session.calls if c["type"] == "userFillsByTime"]
    assert fill_calls[1]["startTime"] == 5000


# ---------------------------------------------------------------- 기타
def test_bad_wallet_rejected():
    with pytest.raises(HyperliquidError):
        HyperliquidClient("0x123")


def test_http_error_raises():
    class ErrSession(FakeSession):
        def post(self, url, json, timeout):
            return FakeResp("rate limited", status=429)
    with pytest.raises(HyperliquidError):
        HyperliquidClient(WALLET, session=ErrSession()).snapshot()


def test_max_leverage_config():
    cfg = {"rules": {"leverage": {"default_max": 5, "per_coin": {"BTC": 20}}}}
    assert max_leverage_for(cfg, "BTC") == 20
    assert max_leverage_for(cfg, "btc") == 20
    assert max_leverage_for(cfg, "ETH") == 5
    assert max_leverage_for(cfg, "xyz:BTC") == 20


# ---------------------------------------------------- 통합 계정 / 강제 청산
def test_unified_account_equity_from_spot():
    client = HyperliquidClient(WALLET, session=FakeSession(mode="unifiedAccount"))
    snap = client.snapshot()
    assert snap.equity == pytest.approx(937.787208)       # perp accountValue(1000) 가 아님
    assert snap.equity_note == ""                         # 통합 계정은 USDC 로 정확히 계산


def test_portfolio_margin_warns_about_non_stable_collateral():
    client = HyperliquidClient(WALLET, session=FakeSession(mode="portfolioMargin"))
    snap = client.snapshot()
    assert snap.equity == pytest.approx(937.787208)
    assert "HYPE" in snap.equity_note


def test_standard_account_uses_perp_value():
    session = FakeSession(mode="default")
    snap = HyperliquidClient(WALLET, session=session).snapshot()
    assert snap.equity == 1000.0
    assert not any(c["type"] == "spotClearinghouseState" for c in session.calls)


# 실계정 강제 청산 체결 형태 (값은 예시)
LIQ_FILL = {"coin": "ETH", "px": "2721.4", "sz": "0.9484", "side": "B", "time": 1789982984687,
            "startPosition": "-17.3453", "dir": "Close Short", "closedPnl": "-208.83768",
            "hash": "0xca", "oid": 551571980620, "crossed": True, "fee": "1.161439",
            "tid": 988035220961204, "feeToken": "USDC", "twapId": None,
            "liquidation": {"liquidatedUser": WALLET, "markPx": "2720.6", "method": "market"}}


def test_liquidation_fill_parsed():
    f, = parse_fills([LIQ_FILL], WALLET)
    assert f.is_liquidation and f.action == "close" and f.position_side == "short"
    other = dict(LIQ_FILL, liquidation={"liquidatedUser": "0x" + "cd" * 20})
    assert parse_fills([other], WALLET)[0].is_liquidation is False   # 남이 청산된 체결
    assert parse_fills([FILLS[0]], WALLET)[0].is_liquidation is False


def test_liquidated_dir_variants():
    """실계정에서 확인된 dir 값 'Liquidated Isolated Long' 처리."""
    for d, side in (("Liquidated Isolated Long", "long"), ("Liquidated Cross Short", "short")):
        f, = parse_fills([dict(LIQ_FILL, dir=d)], WALLET)
        assert (f.action, f.position_side, f.is_liquidation) == ("close", side, True)
