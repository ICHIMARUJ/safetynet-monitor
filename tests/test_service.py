"""화면 API 테스트 (창 없이 Api 만)."""
from __future__ import annotations

import pytest

from monitor.defaults import DEFAULTS, deep_merge
from monitor.store import Store, Violation
from service import Api


@pytest.fixture
def api(tmp_path):
    cfg = deep_merge(DEFAULTS, {"data_dir": str(tmp_path), "exchanges": {"bybit": {"enabled": True}},
                                "ui": {"theme": "ocean"}})
    return Api(cfg)


def test_theme_default_from_config_then_saved(api):
    assert api.get_theme() == "ocean"            # 상대 PC 프리셋 기본값
    assert api.set_theme("lime") == "lime"
    assert api.get_theme() == "lime"
    assert api.set_theme("bad") == "lime"


def test_add_plan_validates(api):
    r = api.add_plan({"exchange": "bybit", "coin": "btc", "side": "long", "entries": "95,000", "stop": "96000"})
    assert not r["ok"]                           # "95,000" → 95 과 0 → 0 이하 가격
    r = api.add_plan({"exchange": "bybit", "coin": "btc", "side": "long", "entries": "95000, 93500",
                      "stop": "96000"})
    assert not r["ok"] and any("손절가" in e for e in r["errors"])
    r = api.add_plan({"exchange": "bybit", "coin": "btc", "side": "long", "entries": "95000, 93500",
                      "stop": "91800", "targets": "99000", "note": "지지선"})
    assert r["ok"]
    p = api.plans()
    assert p["active"][0]["coin"] == "BTC" and p["active"][0]["entries"] == [95000, 93500]
    assert api.close_plan(r["id"], "done") and api.plans()["recent"][0]["status"] == "done"


def test_add_plan_requires_fields(api):
    r = api.add_plan({"exchange": "", "coin": "", "side": "", "entries": "", "stop": ""})
    assert not r["ok"] and len(r["errors"]) >= 3


def test_alert_payload_and_image(api, tmp_path, monkeypatch):
    st = Store(tmp_path / "safetynet.db")
    v = Violation(rule="fomo", message="m", penalty=10, ts=1, dedupe_key="x")
    st.add_violation(v)
    import service
    monkeypatch.setattr(service, "ALERT_DIR", tmp_path)
    a = api.alert(v.id)
    assert a["kind"] == "fomo" and a["meta"]["t1"] == "FOMO" and a["image"] is None
    (tmp_path / "fomo.jpg").write_bytes(b"\xff\xd8fake")
    assert api.alert(v.id)["image"].startswith("data:image/jpeg;base64,")
    assert api.alert(9999) is None


def test_state_without_monitor(api):
    s = api.state()
    assert s["score"] == 100 and s["theme"] == "ocean" and len(s["week"]) == 7


def test_monitor_tick_survives_and_reports(tmp_path, monkeypatch):
    """감시 1주기: 위반을 콜백으로 넘기고, 화면용 요약을 만든다."""
    import service
    from monitor.models import AccountSnapshot, Position
    from monitor.rules import RuleEngine
    cfg = deep_merge(DEFAULTS, {"data_dir": str(tmp_path)})
    p = Position(exchange="bybit", coin="ZEC", side="short", size=12, entry_px=700, mark_px=712,
                 notional=8544, upnl=-144, liq_px=764, margin_mode="cross", leverage_setting=10, margin_used=0)
    snap = AccountSnapshot(exchange="bybit", ts=0, equity=1200, positions=[p])
    monkeypatch.setattr(service, "poll", lambda store, clients: ([snap], []))
    got = []
    mon = service.Monitor(cfg, on_violations=got.extend)
    st = Store(tmp_path / "safetynet.db")
    mon._tick(RuleEngine(cfg, st), st, {"bybit": object()})
    v = mon.snapshot_view()
    assert {x.rule for x in got} >= {"leverage", "liq_distance"}
    assert v["positions"][0]["lev_warn"] and v["positions"][0]["liq_warn"] and v["exchanges"]["bybit"]["ok"]
    assert round(v["total"]["leverage"], 2) == 7.12


def test_gameover_rest_choice_sets_rest_day(api, tmp_path):
    st = Store(tmp_path / "safetynet.db")
    v = Violation(rule="liquidation", message="m", penalty=30, ts=1, dedupe_key="liq")
    st.add_violation(v)
    api.popup_choice(v.id, 0)
    assert st.get_kv("rest_day") is None
    api.popup_choice(v.id, 1)
    assert st.get_kv("rest_day") is not None


def test_autostart_windows_only(api):
    a = api.get_autostart()
    assert set(a) == {"supported", "on"}


def test_monitor_breakeven_and_trade_close_prompt(tmp_path, monkeypatch):
    import service
    from monitor.models import AccountSnapshot, Fill, Position
    from monitor.rules import RuleEngine
    from monitor.breakeven import BreakevenWatch
    cfg = deep_merge(DEFAULTS, {"data_dir": str(tmp_path)})
    now = service.now_ms()

    def pos(mark):
        return Position(exchange="bybit", coin="ZEC", side="long", size=1, entry_px=100, mark_px=mark, notional=mark,
                        upnl=mark - 100, liq_px=None, margin_mode="cross", leverage_setting=5, margin_used=0)
    seq = [([AccountSnapshot(exchange="bybit", ts=now, equity=1000, positions=[pos(97)])], []),
           ([AccountSnapshot(exchange="bybit", ts=now, equity=1000, positions=[pos(100)])], []),
           ([AccountSnapshot(exchange="bybit", ts=now, equity=1000, positions=[])],
            [Fill(exchange="bybit", coin="ZEC", tid="c1", oid="c1", ts=now - 1000, side="sell", px=100, sz=1,
                  action="close", position_side="long", start_position=1, closed_pnl=-5, fee=0)])]
    st = Store(tmp_path / "safetynet.db")
    st.add_fills([Fill(exchange="bybit", coin="ZEC", tid="o1", oid="o1", ts=now - 600_000, side="buy", px=100, sz=1,
                       action="open", position_side="long", start_position=0, closed_pnl=0, fee=0)])

    def fake_poll(store, clients):
        snaps, fills = seq.pop(0)
        return snaps, store.add_fills(fills)
    monkeypatch.setattr(service, "poll", fake_poll)
    be, closed = [], []
    mon = service.Monitor(cfg, on_breakeven=be.append, on_trade_closed=closed.append)
    mon.breakeven = BreakevenWatch(cfg, {})
    eng = RuleEngine(cfg, st)
    for _ in range(3):
        mon._tick(eng, st, {"bybit": object()})
    assert len(be) == 1 and be[0]["side"] == "long" and be[0]["worst_pct"] == 3.0
    assert len(closed) == 1 and closed[0]["coin"] == "ZEC" and closed[0]["pnl"] == -5
    assert closed[0]["key"] in mon.pending_trades
    mon._closed_trades(st, [], [])                     # 같은 거래로 두 번 묻지 않음
    assert len(closed) == 1
    api = Api(cfg, mon)
    r = api.journal_submit(closed[0]["key"], "초조", "메모")
    assert not r["ok"] and "연결되지 않음" in r["error"]
