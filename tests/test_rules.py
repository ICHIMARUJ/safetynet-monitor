"""규칙 엔진 테스트. 가짜 시계와 임시 DB 로 규칙별 동작을 확인한다."""
from __future__ import annotations

import pytest

from monitor.defaults import DEFAULTS, deep_merge
from monitor.models import AccountSnapshot, Fill, Order, Position
from monitor.rules import DAY_MS, RuleEngine, score_for_day
from monitor.store import Plan, Store

T0 = 1_800_000_000_000 - (1_800_000_000_000 % DAY_MS) + 3_600_000  # 어느 날 01:00 UTC
MIN = 60_000


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    cfg = deep_merge(DEFAULTS, {"rules": {"leverage": {"default_max": 5, "per_coin": {"BTC": 20}}}})
    store = Store(tmp_path / "t.db")
    clock = Clock(T0)
    eng = RuleEngine(cfg, store, now_fn=clock)
    yield cfg, store, clock, eng
    store.close()


def pos(coin="BTC", side="long", size=0.1, entry=100_000, mark=100_000, liq=None, upnl=0.0):
    return Position(exchange="hyperliquid", coin=coin, side=side, size=size, entry_px=entry,
                    mark_px=mark, notional=size * mark, upnl=upnl, liq_px=liq,
                    margin_mode="cross", leverage_setting=20, margin_used=0)


def stop(coin="BTC", side="sell", trig=95_000, oid="s1"):
    return Order(exchange="hyperliquid", coin=coin, oid=oid, side=side, kind="stop", price=0,
                 trigger_px=trig, size=0, reduce_only=True, is_position_tpsl=True, ts=0)


def snap(positions, orders=(), equity=10_000.0, ts=T0):
    return AccountSnapshot(exchange="hyperliquid", ts=ts, equity=equity,
                           positions=list(positions), orders=list(orders))


_tid = [0]


def fill(coin="BTC", action="open", side_pos="long", px=100_000, sz=0.1, start=0.0,
         pnl=0.0, ts=T0, oid=None, fee=0.0, liq=False):
    _tid[0] += 1
    side = ("buy" if side_pos == "long" else "sell") if action == "open" else \
        ("sell" if side_pos == "long" else "buy")
    return Fill(exchange="hyperliquid", coin=coin, tid=str(_tid[0]), oid=oid or f"o{_tid[0]}",
                ts=ts, side=side, px=px, sz=sz, action=action, position_side=side_pos,
                start_position=start, closed_pnl=pnl, fee=fee, is_liquidation=liq)


def run(eng, store, snaps, fills=()):
    store.add_fills(list(fills))
    return eng.process(list(snaps), list(fills))


def rules_of(vs, ignore=("risk_per_trade",)):
    """위반 규칙 이름 목록. 기본으로 1회 리스크 규칙은 제외 (테스트용 손절폭이 넓어서 함께 걸림)."""
    return [v.rule for v in vs if v.rule not in ignore]


# ------------------------------------------------------------- 1. 레버리지
def test_btc_20x_ok_alt_5x_flagged(env):
    cfg, store, clock, eng = env
    # 자본 1만: BTC 명목 15만 = 15배(한도 20) / ETH 명목 6만 = 6배(한도 5)
    s = snap([pos(size=1.5), pos(coin="ETH", size=20, entry=3000, mark=3000)],
             [stop(), stop(coin="ETH", trig=2900, oid="s2")])
    vs = run(eng, store, [s])
    assert rules_of(vs) == ["account_leverage", "leverage"]  # 계좌 21배 > 20, ETH 6배 > 5
    lev = [v for v in vs if v.rule == "leverage"]
    assert len(lev) == 1 and lev[0].coin == "ETH"            # BTC 15배는 한도 20배 이내
    # 같은 상태가 계속되면 반복 기록 없음
    clock.t += MIN
    assert rules_of(run(eng, store, [s])) == []


def test_leverage_rearms_after_clearing(env):
    cfg, store, clock, eng = env
    hi = snap([pos(coin="ETH", size=20, entry=3000, mark=3000)], [stop("ETH", trig=2900)])
    lo = snap([pos(coin="ETH", size=10, entry=3000, mark=3000)], [stop("ETH", trig=2900)])
    assert rules_of(run(eng, store, [hi])) == ["leverage"]
    clock.t += MIN
    assert rules_of(run(eng, store, [lo])) == []
    clock.t += MIN
    assert rules_of(run(eng, store, [hi])) == ["leverage"]


# ------------------------------------------------------------- 2. SL 미설정
def test_no_stop_after_grace(env):
    cfg, store, clock, eng = env
    s = snap([pos()])
    assert rules_of(run(eng, store, [s])) == []
    clock.t += 2 * MIN
    assert rules_of(run(eng, store, [s])) == []
    clock.t += 2 * MIN
    assert rules_of(run(eng, store, [s])) == ["no_stop"]
    clock.t += MIN
    assert rules_of(run(eng, store, [s])) == []           # 한 포지션당 한 번


def test_plan_stop_exempts_no_stop(env):
    cfg, store, clock, eng = env
    store.add_plan(Plan("hyperliquid", "BTC", "long", [100_000], 97_000))
    s = snap([pos()])
    run(eng, store, [s])
    clock.t += 10 * MIN
    assert rules_of(run(eng, store, [s])) == []


# ------------------------------------------------------------- 3. 손절 회피
def test_stop_moved_away_flagged_trailing_ok(env):
    cfg, store, clock, eng = env
    run(eng, store, [snap([pos()], [stop(trig=95_000)])])
    clock.t += MIN
    assert rules_of(run(eng, store, [snap([pos()], [stop(trig=96_000)])])) == []   # 손절 올림 = OK
    clock.t += MIN
    vs = run(eng, store, [snap([pos()], [stop(trig=94_000)])])
    assert rules_of(vs) == ["stop_moved"]


def test_short_stop_moved_up_flagged(env):
    cfg, store, clock, eng = env
    p = pos(side="short")
    run(eng, store, [snap([p], [stop(side="buy", trig=103_000)])])
    clock.t += MIN
    assert rules_of(run(eng, store, [snap([p], [stop(side="buy", trig=105_000)])])) == ["stop_moved"]


def test_stop_removed_after_grace_and_replace_ok(env):
    cfg, store, clock, eng = env
    run(eng, store, [snap([pos()], [stop()])])
    clock.t += 15_000
    assert rules_of(run(eng, store, [snap([pos()])])) == []            # 잠깐 없는 건 교체 중일 수 있음
    clock.t += 15_000
    assert rules_of(run(eng, store, [snap([pos()], [stop()])])) == []  # 같은 가격으로 재설정
    clock.t += 15_000
    run(eng, store, [snap([pos()])])
    clock.t += 61_000
    assert rules_of(run(eng, store, [snap([pos()])])) == ["stop_removed"]


def test_plan_stop_breach(env):
    cfg, store, clock, eng = env
    store.add_plan(Plan("hyperliquid", "BTC", "long", [100_000], 97_000))
    run(eng, store, [snap([pos(mark=99_000)])])
    clock.t += MIN
    assert rules_of(run(eng, store, [snap([pos(mark=96_500)])])) == []
    clock.t += 61_000
    assert rules_of(run(eng, store, [snap([pos(mark=96_000)])])) == ["stop_breach"]


# ------------------------------------------------------------- 4. 물타기
def test_averaging_down_and_planned_exception(env):
    cfg, store, clock, eng = env
    run(eng, store, [snap([pos(entry=100_000, mark=97_000)], [stop(trig=90_000)])])
    clock.t += MIN
    f = fill(px=97_000, start=0.1, ts=clock.t)
    assert rules_of(run(eng, store, [snap([pos(size=0.2, entry=98_500, mark=97_000)],
                                          [stop(trig=90_000)])], [f])) == ["averaging_down"]
    # 계획 진입가(95,000) 근처 추가는 허용
    store.add_plan(Plan("hyperliquid", "BTC", "long", [98_500, 95_000], 90_000))
    clock.t += MIN
    f2 = fill(px=95_100, start=0.2, ts=clock.t)
    assert rules_of(run(eng, store, [snap([pos(size=0.3, entry=97_000, mark=95_100)], [stop(trig=90_000)])],
               [f2])) == []


def test_pyramiding_in_profit_ok(env):
    cfg, store, clock, eng = env
    run(eng, store, [snap([pos(entry=100_000, mark=103_000)], [stop(trig=99_000)])])
    clock.t += MIN
    f = fill(px=103_000, start=0.1, ts=clock.t)
    assert rules_of(run(eng, store, [snap([pos(size=0.2, entry=101_500, mark=103_000)], [stop(trig=99_000)])],
               [f])) == []


# ------------------------------------------------------------- 5. 청산가
def test_liq_distance(env):
    cfg, store, clock, eng = env
    vs = run(eng, store, [snap([pos(liq=92_000)], [stop()])])       # 8%
    assert rules_of(vs) == ["liq_distance"]
    clock.t += MIN
    assert rules_of(run(eng, store, [snap([pos(liq=89_500)], [stop()])])) == []  # 10.5%: 아직 해소 아님
    clock.t += MIN
    run(eng, store, [snap([pos(liq=85_000)], [stop()])])            # 15%: 해소
    clock.t += MIN
    assert rules_of(run(eng, store, [snap([pos(liq=92_000)], [stop()])])) == ["liq_distance"]


# ------------------------------------------------------------- 6. 1회 리스크
def test_risk_per_trade(env):
    cfg, store, clock, eng = env
    # 0.1 BTC, 진입 100,000, SL 95,000 → 손실 $500 = 자본 1만의 5% > 2%
    assert rules_of(run(eng, store, [snap([pos()], [stop(trig=95_000)])]), ignore=()) == ["risk_per_trade"]
    clock.t += MIN
    assert rules_of(run(eng, store, [snap([pos()], [stop(trig=95_000)])]), ignore=()) == []
    clock.t += MIN
    # SL 을 98,500 으로 올리면 $150 = 1.5% → 위반 없음
    assert rules_of(run(eng, store, [snap([pos()], [stop(trig=98_500)])]), ignore=()) == []


# ------------------------------------------------------------- 7. 복수매매
def test_revenge_after_loss(env):
    cfg, store, clock, eng = env
    close = fill(action="close", px=98_000, start=0.1, pnl=-200, ts=clock.t)
    run(eng, store, [snap([])], [close])
    clock.t += 5 * MIN
    reopen = fill(coin="ETH", px=3000, sz=10, ts=clock.t)
    vs = run(eng, store, [snap([pos("ETH", size=10, entry=3000, mark=3000)],
                               [stop("ETH", trig=2950)])], [reopen])
    assert rules_of(vs) == ["revenge"]
    assert "큰 규모" in vs[0].message       # 재진입 $30,000 > 손실 청산 $9,800


def test_no_revenge_after_cooldown_or_profit(env):
    cfg, store, clock, eng = env
    run(eng, store, [snap([])], [fill(action="close", pnl=-100, start=0.1, ts=clock.t)])
    clock.t += 16 * MIN
    assert rules_of(run(eng, store, [snap([])], [fill(ts=clock.t)])) == []
    clock.t += MIN
    run(eng, store, [snap([])], [fill(action="close", pnl=+100, start=0.1, ts=clock.t)])
    clock.t += MIN
    assert rules_of(run(eng, store, [snap([])], [fill(ts=clock.t)])) == []


def test_flip_on_loss_is_revenge(env):
    cfg, store, clock, eng = env
    f = fill(action="flip", side_pos="short", pnl=-200, start=0.1, sz=0.2, ts=clock.t)
    assert rules_of(run(eng, store, [snap([])], [f])) == ["revenge"]


# ------------------------------------------------------------- 8. 과매매
def test_overtrading_floor(env):
    cfg, store, clock, eng = env
    fills = [fill(coin="SOL", px=150, sz=1, ts=clock.t + i) for i in range(11)]
    vs = run(eng, store, [snap([])], fills)
    assert rules_of(vs).count("overtrading") == 1


def test_overtrading_uses_history(env):
    cfg, store, clock, eng = env
    # 지난 10일간 매일 20회 → 기준 20 이상. 오늘 11회는 과매매 아님
    hist = [fill(coin="SOL", px=150, sz=1, ts=T0 - d * DAY_MS + i)
            for d in range(1, 11) for i in range(20)]
    store.add_fills(hist)
    today = [fill(coin="SOL", px=150, sz=1, ts=clock.t + i) for i in range(11)]
    assert "overtrading" not in rules_of(run(eng, store, [snap([])], today))


# ------------------------------------------------------------- 9. 사이즈 급증
def test_size_spike(env):
    cfg, store, clock, eng = env
    hist = [fill(coin="SOL", px=100, sz=10, ts=T0 - DAY_MS - i) for i in range(6)]  # 각 $1,000
    store.add_fills(hist)
    big = fill(coin="SOL", px=100, sz=30, ts=clock.t)                               # $3,000
    assert "size_spike" in rules_of(run(eng, store, [snap([])], [big]))
    clock.t += MIN
    normal = fill(coin="SOL", px=100, sz=15, ts=clock.t)                            # $1,500
    assert "size_spike" not in rules_of(run(eng, store, [snap([])], [normal]))


def test_size_spike_partial_fills(env):
    cfg, store, clock, eng = env
    store.add_fills([fill(coin="SOL", px=100, sz=10, ts=T0 - DAY_MS - i) for i in range(6)])
    p1 = fill(coin="SOL", px=100, sz=15, ts=clock.t, oid="big")            # 첫 체결 $1,500
    assert "size_spike" not in rules_of(run(eng, store, [snap([])], [p1]))
    clock.t += 1000
    p2 = fill(coin="SOL", px=100, sz=15, start=15, ts=clock.t, oid="big")  # 합계 $3,000
    assert "size_spike" in rules_of(run(eng, store, [snap([])], [p2]))


# ------------------------------------------------------------- 10. 일 손실 한도
def test_daily_loss_limit(env):
    cfg, store, clock, eng = env
    run(eng, store, [snap([])])                          # 시작 자본 1만 기록 → 한도 $500
    clock.t += MIN
    loss = fill(action="close", pnl=-520, start=0.1, ts=clock.t)
    run(eng, store, [snap([], equity=9480)], [loss])
    clock.t += 30 * MIN                                  # 복수매매 쿨다운은 지남
    again = fill(coin="ETH", px=3000, sz=1, ts=clock.t)
    assert "daily_loss_limit" in rules_of(run(eng, store, [snap([], equity=9480)], [again]))


# ------------------------------------------------------------- 점수·상태
def test_score_and_state_persists(env, tmp_path):
    cfg, store, clock, eng = env
    run(eng, store, [snap([pos(coin="ETH", size=20, entry=3000, mark=3000)],
                          [stop("ETH", trig=2990)])])
    assert score_for_day(cfg, store, clock.t) == 100 - cfg["score"]["penalty"]["leverage"]
    # 재시작해도 같은 지속 조건을 다시 기록하지 않음
    eng2 = RuleEngine(cfg, store, now_fn=clock)
    clock.t += MIN
    assert eng2.process([snap([pos(coin="ETH", size=20, entry=3000, mark=3000)],
                              [stop("ETH", trig=2990)])], []) == []


def test_closed_position_closes_plan(env):
    cfg, store, clock, eng = env
    pid = store.add_plan(Plan("hyperliquid", "xyz:TSLA", "long", [300], 280))
    run(eng, store, [snap([pos(coin="xyz:TSLA", size=1, entry=300, mark=300)])])
    clock.t += MIN
    run(eng, store, [snap([])])
    assert store.plans("active") == []
    assert store.plans(None)[0].id == pid and store.plans(None)[0].status == "done"


# ------------------------------------------------------------- 설정 병합
def test_user_config_kept_new_keys_defaulted():
    user = {"rules": {"liq_distance_min_pct": 10, "leverage": {"per_coin": {"BTC": 10}}}}
    cfg = deep_merge(DEFAULTS, user)
    assert cfg["rules"]["liq_distance_min_pct"] == 10
    assert cfg["rules"]["leverage"]["per_coin"] == {"BTC": 10}
    assert cfg["rules"]["leverage"]["default_max"] == DEFAULTS["rules"]["leverage"]["default_max"]
    assert cfg["rules"]["plan_entry_tolerance_pct"] == 0.5   # 사용자 파일에 없는 새 항목
    assert DEFAULTS["rules"]["leverage"]["per_coin"] == {}    # 기본값 원본은 오염되지 않음


# ------------------------------------------------------------- 강제 청산
def test_liquidation_recorded_once_per_order(env):
    cfg, store, clock, eng = env
    fs = [fill(coin="ETH", action="close", side_pos="short", px=2721, sz=1, start=-3,
               pnl=-300, fee=2, ts=clock.t, oid="L1", liq=True),
          fill(coin="ETH", action="close", side_pos="short", px=2721, sz=2, start=-2,
               pnl=-600, fee=3, ts=clock.t, oid="L1", liq=True)]
    vs = [v for v in run(eng, store, [snap([], equity=100_000)], fs) if v.rule == "liquidation"]
    assert len(vs) == 1 and "$905.00" in vs[0].message
    assert vs[0].penalty == cfg["score"]["penalty"]["liquidation"]


def test_store_migrates_old_db(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE fills (exchange TEXT NOT NULL, tid TEXT NOT NULL, coin TEXT NOT NULL,"
                " oid TEXT, ts INTEGER NOT NULL, side TEXT, px REAL, sz REAL, action TEXT,"
                " position_side TEXT, start_position REAL, closed_pnl REAL, fee REAL,"
                " PRIMARY KEY (exchange, tid))")
    con.execute("INSERT INTO fills VALUES ('hyperliquid','1','BTC','o',1,'buy',1,1,'open','long',0,0,0)")
    con.commit()
    con.close()
    st = Store(db)
    old, = st.fills_between(0)
    assert old.is_liquidation is False                     # 기존 기록 유지
    st.add_fills([fill(liq=True, ts=5)])
    assert [f.is_liquidation for f in st.fills_between(0)] == [False, True]
    st.close()
