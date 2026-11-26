"""Bybit 수집기 테스트. 문서 기준 응답 형태의 가짜 서버를 사용한다."""
from __future__ import annotations

import hashlib
import hmac
import sys
from urllib.parse import parse_qsl, urlsplit

import pytest

from monitor.exchanges.bybit import (
    DAY_MS, PNL_WAIT_MS, BybitClient, BybitError, build_fills, parse_orders,
    parse_position, position_stop_orders, symbol_to_coin,
)

NOW = 1_800_000_000_000
KEY, SECRET = "testkey", "testsecret"


def pos(symbol="BTCUSDT", side="Buy", size="0.3", avg="100000", mark="98000", liq="90000",
        sl="0", idx=0):
    return {"symbol": symbol, "side": side, "size": size, "avgPrice": avg, "markPrice": mark,
            "liqPrice": liq, "unrealisedPnl": "-600", "positionIM": "1500", "leverage": "20",
            "tradeMode": 0, "stopLoss": sl, "positionIdx": idx, "updatedTime": str(NOW)}


def order(symbol="BTCUSDT", side="Sell", stype="StopLoss", trig="95000", otype="Market",
          status="Untriggered", reduce=True, oid="o1"):
    return {"symbol": symbol, "orderId": oid, "side": side, "orderType": otype, "price": "0",
            "qty": "0.3", "triggerPrice": trig, "stopOrderType": stype, "orderStatus": status,
            "reduceOnly": reduce, "closeOnTrigger": False, "createdTime": str(NOW)}


def ex(eid, side, qty, t, oid=None, closed="0", etype="Trade", symbol="BTCUSDT", px="100000"):
    return {"symbol": symbol, "execId": eid, "orderId": oid or f"ord-{eid}", "side": side,
            "execQty": qty, "execPrice": px, "execTime": str(t), "execType": etype,
            "closedSize": closed, "seq": t}


# ------------------------------------------------------------- 파싱
def test_symbol_to_coin():
    assert symbol_to_coin("BTCUSDT") == "BTC"
    assert symbol_to_coin("1000PEPEUSDT") == "1000PEPE"
    assert symbol_to_coin("ETHPERP") == "ETH/USDC"
    assert symbol_to_coin("BTC-26DEC25") == "BTC-26DEC25"


def test_parse_position():
    p = parse_position(pos(liq=""))
    assert (p.coin, p.side, p.size, p.entry_px, p.mark_px) == ("BTC", "long", 0.3, 100000, 98000)
    assert p.notional == pytest.approx(29400) and p.liq_px is None and p.margin_used == 1500
    assert parse_position(pos(side="Sell")).side == "short"


def test_parse_orders_kinds_and_status():
    raw = [order(), order(stype="TakeProfit", trig="110000", oid="o2"),
           order(stype="Stop", trig="94000", oid="o3"),
           order(stype="Stop", trig="94000", reduce=False, side="Buy", oid="o4"),
           order(stype="", trig="0", otype="Limit", status="New", reduce=False, oid="o5"),
           order(status="Filled", oid="o6")]
    kinds = [(o.oid, o.kind) for o in parse_orders(raw)]
    assert kinds == [("o1", "stop"), ("o2", "take_profit"), ("o3", "stop"), ("o4", "other"),
                     ("o5", "limit")]


def test_position_level_stop_loss():
    raw = [pos(sl="96000")]
    extra = position_stop_orders(raw, [])
    assert len(extra) == 1 and extra[0].trigger_px == 96000 and extra[0].side == "sell"
    # 같은 가격의 손절 주문이 이미 있으면 중복으로 만들지 않음
    assert position_stop_orders(raw, parse_orders([order(trig="96000")])) == []


# ---------------------------------------------- 체결 → 직전 수량 거꾸로 계산
def test_build_fills_open_add_reconstruction():
    execs = [ex("e1", "Buy", "0.1", NOW - 3000), ex("e2", "Buy", "0.2", NOW - 2000)]
    fs = build_fills(execs, [], {"BTCUSDT": 0.3}, now_ms=NOW)
    assert [(f.action, f.position_side, f.start_position) for f in fs] == [
        ("open", "long", 0.0), ("open", "long", 0.1)]


def test_build_fills_flip_with_pnl():
    execs = [ex("e1", "Buy", "0.2", NOW - 600_000),
             ex("e2", "Sell", "0.3", NOW - 300_000, oid="close1", closed="0.2")]
    pnl = [{"orderId": "close1", "closedPnl": "-30", "closedSize": "0.2", "createdTime": "1"}]
    f1, f2 = build_fills(execs, pnl, {"BTCUSDT": -0.1}, now_ms=NOW)
    assert (f1.action, f1.start_position) == ("open", 0.0)
    assert (f2.action, f2.position_side, f2.start_position) == ("flip", "short", pytest.approx(0.2))
    assert f2.closed_pnl == pytest.approx(-30) and f2.fee == 0


def test_partial_close_across_polls_uses_per_unit_pnl():
    # 주문 c1 이 0.1 + 0.1 로 나눠 체결. 이번 조회에는 두 번째 체결만 있음 → 손익은 절반
    execs = [ex("e2", "Sell", "0.1", NOW - 300_000, oid="c1", closed="0.1")]
    pnl = [{"orderId": "c1", "closedPnl": "-40", "closedSize": "0.2", "createdTime": "1"}]
    f, = build_fills(execs, pnl, {"BTCUSDT": 0.0}, now_ms=NOW)
    assert f.closed_pnl == pytest.approx(-20)


def test_pending_pnl_waits_then_gives_up():
    execs = [ex("e1", "Buy", "0.1", NOW - 10_000),
             ex("e2", "Sell", "0.1", NOW - 5_000, oid="c1", closed="0.1"),
             ex("e3", "Buy", "0.1", NOW - 1_000)]
    # 청산손익 기록이 아직 없으면 e2 부터는 다음 조회로 미룸
    fs = build_fills(execs, [], {"BTCUSDT": 0.1}, now_ms=NOW)
    assert [f.tid for f in fs] == ["e1"]
    # 2분이 지나도 없으면 손익 0 으로 처리
    fs = build_fills(execs, [], {"BTCUSDT": 0.1}, now_ms=NOW + PNL_WAIT_MS)
    assert [f.tid for f in fs] == ["e1", "e2", "e3"] and fs[1].closed_pnl == 0


def test_liquidation_and_funding():
    execs = [ex("f1", "Sell", "0.3", NOW - 900_000, etype="Funding"),
             ex("b1", "Sell", "0.3", NOW - 600_000, oid="liq", closed="0.3", etype="BustTrade")]
    pnl = [{"orderId": "liq", "closedPnl": "-900", "closedSize": "0.3", "createdTime": "1"}]
    fs = build_fills(execs, pnl, {"BTCUSDT": 0.0}, now_ms=NOW)
    assert len(fs) == 1 and fs[0].is_liquidation and fs[0].action == "close"
    assert fs[0].position_side == "long" and fs[0].closed_pnl == pytest.approx(-900)


# ------------------------------------------------------------- 클라이언트
class Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code, self.text = data, status, str(data)

    def json(self):
        return self._d


class FakeBybit:
    def __init__(self, positions=(), orders=(), execs=(), pnl=(), equity="5000",
                 unified=True, fail_first=None):
        self.positions, self.orders = list(positions), list(orders)
        self.execs, self.pnl = list(execs), list(pnl)
        self.equity, self.unified, self.fail_first = equity, unified, fail_first
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        parts = urlsplit(url)
        q = dict(parse_qsl(parts.query))
        path = parts.path
        if path == "/v5/market/time":
            return Resp({"retCode": 0, "time": NOW + 5000})
        # 서명 검증: 헤더와 실제 쿼리로 다시 계산해서 일치해야 함
        expect = hmac.new(SECRET.encode(), (headers["X-BAPI-TIMESTAMP"] + KEY
                          + headers["X-BAPI-RECV-WINDOW"] + parts.query).encode(),
                          hashlib.sha256).hexdigest()
        assert headers["X-BAPI-SIGN"] == expect and headers["X-BAPI-API-KEY"] == KEY
        self.calls.append((path, q, headers["X-BAPI-TIMESTAMP"]))
        if self.fail_first is not None:
            code, self.fail_first = self.fail_first, None
            return Resp({"retCode": code, "retMsg": "err"})
        if path == "/v5/account/wallet-balance":
            if q["accountType"] == "UNIFIED" and not self.unified:
                return Resp({"retCode": 10001, "retMsg": "accountType only support CONTRACT"})
            return Resp({"retCode": 0, "result": {"list": [{"totalEquity": self.equity, "coin": []}]}})
        if path == "/v5/position/list":
            sc = q["settleCoin"]
            rows = [p for p in self.positions if (p["symbol"].endswith("PERP")) == (sc == "USDC")]
            return Resp({"retCode": 0, "result": {"list": rows, "nextPageCursor": ""}})
        if path == "/v5/order/realtime":
            rows = self.orders if q["settleCoin"] == "USDT" else []
            return Resp({"retCode": 0, "result": {"list": rows, "nextPageCursor": ""}})
        if path == "/v5/execution/list":
            s, e = int(q["startTime"]), int(q["endTime"])
            assert e - s <= 7 * DAY_MS
            rows = [x for x in self.execs if s <= int(x["execTime"]) <= e]
            # 2페이지로 나눠서 돌려줌 (페이지 넘김 확인)
            if q.get("cursor") == "p2":
                return Resp({"retCode": 0, "result": {"list": rows[1:], "nextPageCursor": ""}})
            more = "p2" if len(rows) > 1 else ""
            return Resp({"retCode": 0, "result": {"list": rows[:1], "nextPageCursor": more}})
        if path == "/v5/position/closed-pnl":
            return Resp({"retCode": 0, "result": {"list": self.pnl, "nextPageCursor": ""}})
        return Resp({"retCode": 10001, "retMsg": "unknown path"})


def client(fake):
    return BybitClient(KEY, SECRET, session=fake, now_fn=lambda: NOW / 1000)


def test_missing_keys():
    with pytest.raises(BybitError):
        BybitClient("", "")


def test_snapshot_unified():
    fake = FakeBybit(positions=[pos(), pos(symbol="ETHPERP", side="Sell", size="2", avg="3000",
                                           mark="3000", liq="3300")],
                     orders=[order()])
    snap = client(fake).snapshot()
    assert snap.equity == 5000 and snap.equity_note == ""
    btc, eth = snap.positions
    assert (btc.coin, eth.coin, eth.side) == ("BTC", "ETH/USDC", "short")
    assert [o.oid for o in snap.stop_orders_for(btc)] == ["o1"]
    assert snap.stop_orders_for(eth) == []


def test_classic_account_fallback_and_hedge_note():
    fake = FakeBybit(positions=[pos(idx=1)], unified=False, equity="1200")
    snap = client(fake).snapshot()
    assert snap.equity == 1200 and "클래식" in snap.equity_note and "헤지" in snap.equity_note


def test_error_hint_and_time_resync():
    with pytest.raises(BybitError, match="API 키가 올바르지 않음"):
        client(FakeBybit(fail_first=10003)).snapshot()
    fake = FakeBybit(fail_first=10002)          # 시계 어긋남 → 서버 시간 맞추고 재시도
    c = client(fake)
    c.snapshot()
    assert c.offset_ms == 5000
    assert int(fake.calls[1][2]) == NOW + 5000  # 재시도 요청은 보정된 시각으로 서명


def test_fills_since_windows_pages_and_reconstruction():
    execs = [ex("e1", "Buy", "0.1", NOW - 20 * DAY_MS),     # 20일 전 (3번째 창)
             ex("e2", "Buy", "0.1", NOW - 2 * DAY_MS),
             ex("e3", "Buy", "0.1", NOW - DAY_MS)]
    fake = FakeBybit(positions=[pos(size="0.3")], execs=execs)
    fs = client(fake).fills_since(NOW - 21 * DAY_MS + 1)    # 정확히 21일 = 7일 창 3개
    assert [f.tid for f in fs] == ["e1", "e2", "e3"]
    assert [f.start_position for f in fs] == [0.0, pytest.approx(0.1), pytest.approx(0.2)]
    windows = [c for c in fake.calls if c[0] == "/v5/execution/list" and "cursor" not in c[1]]
    assert len(windows) == 3                      # 21일 → 7일 창 3개


# ------------------------------------------------------------- 실행 루프 연결
def test_run_once_bybit_only(tmp_path, monkeypatch, capsys):
    import run as run_mod
    from monitor.defaults import DEFAULTS, deep_merge
    cfg = deep_merge(DEFAULTS, {"data_dir": str(tmp_path),
                                "exchanges": {"bybit": {"enabled": True}},
                                "rules": {"leverage": {"per_coin": {"BTC": 10}}}})
    cfg["secrets"] = {"bybit_key": KEY, "bybit_secret": SECRET}
    fake = FakeBybit(positions=[pos(size="0.6", mark="98000")], orders=[order()])
    monkeypatch.setattr(run_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(run_mod, "BybitClient", lambda k, s: client(fake))
    monkeypatch.setattr(sys, "argv", ["run.py", "--once"])
    assert run_mod.main() == 0
    out = capsys.readouterr().out
    assert "[bybit] BTC LONG 실효 레버리지 11.8배 — 한도 10배 초과" in out   # 58,800 / 5,000


def test_run_skips_exchange_without_keys(tmp_path, monkeypatch, capsys):
    import run as run_mod
    from monitor.defaults import DEFAULTS, deep_merge
    cfg = deep_merge(DEFAULTS, {"data_dir": str(tmp_path), "exchanges": {"bybit": {"enabled": True}}})
    cfg["secrets"] = {"bybit_key": "", "bybit_secret": ""}
    monkeypatch.setattr(run_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(sys, "argv", ["run.py", "--once"])
    assert run_mod.main() == 1
    out = capsys.readouterr().out
    assert "bybit 건너뜀" in out and "연결된 거래소가 없음" in out
