"""신규 규칙(FOMO·지표 발표 직전·승리 후 과신), 소급 계산, 기록 집계 테스트."""
from __future__ import annotations

import pytest

from monitor.config import load_events
from monitor.defaults import DEFAULTS, deep_merge
from monitor.history import summary
from monitor.mistakes import KINDS, RULE_KIND
from monitor.retro import run_retro
from monitor.rules import DAY_MS, RuleEngine
from monitor.store import Plan, Store
from tests.test_rules import MIN, T0, Clock, fill, rules_of, run, snap


class FakeMarket:
    def __init__(self, hi, lo):
        self.hi, self.lo, self.calls = hi, lo, 0

    def price_range(self, coin, start, end):
        self.calls += 1
        return (self.hi, self.lo)


@pytest.fixture
def base(tmp_path):
    cfg = deep_merge(DEFAULTS, {})
    store = Store(tmp_path / "n.db")
    yield cfg, store
    store.close()


# ------------------------------------------------------------- FOMO
def test_fomo_long_after_pump(base):
    cfg, store = base
    mk = FakeMarket(hi=106, lo=100)
    eng = RuleEngine(cfg, store, now_fn=Clock(T0), market={"hyperliquid": mk})
    f = fill(coin="SOL", px=106, sz=10, ts=T0)                     # 저점 100 → 106 진입 = +6%
    vs = run(eng, store, [snap([])], [f])
    assert "fomo" in rules_of(vs)
    # 같은 주문의 추가 체결이 와도 시세를 다시 조회하지 않음
    f2 = fill(coin="SOL", px=106, sz=5, ts=T0 + 1, oid=f.oid)
    run(eng, store, [snap([])], [f2])
    assert mk.calls == 1


def test_fomo_small_move_or_plan_is_ok(base):
    cfg, store = base
    eng = RuleEngine(cfg, store, now_fn=Clock(T0), market={"hyperliquid": FakeMarket(103, 100)})
    assert "fomo" not in rules_of(run(eng, store, [snap([])], [fill(coin="SOL", px=103, sz=10)]))
    # 큰 움직임이어도 매매 계획이 있으면 FOMO 아님
    store.add_plan(Plan(exchange="hyperliquid", coin="ETH", side="short", entries=[3000], stop_px=3100))
    eng2 = RuleEngine(cfg, store, now_fn=Clock(T0), market={"hyperliquid": FakeMarket(3300, 3000)},
                      state_key="other")
    f = fill(coin="ETH", side_pos="short", px=3000, sz=1)          # 고점 3300 → 3000 숏 = -9%
    assert "fomo" not in rules_of(run(eng2, store, [snap([])], [f]))


def test_fomo_short_after_dump(base):
    cfg, store = base
    eng = RuleEngine(cfg, store, now_fn=Clock(T0), market={"hyperliquid": FakeMarket(3300, 3000)})
    f = fill(coin="ETH", side_pos="short", px=3000, sz=1)
    vs = run(eng, store, [snap([])], [f])
    assert "fomo" in rules_of(vs) and "급락" in [v for v in vs if v.rule == "fomo"][0].message


def test_fomo_market_error_is_skipped(base):
    cfg, store = base

    class Broken:
        def price_range(self, *a):
            raise RuntimeError("down")
    eng = RuleEngine(cfg, store, now_fn=Clock(T0), market={"hyperliquid": Broken()})
    assert "fomo" not in rules_of(run(eng, store, [snap([])], [fill(coin="SOL", px=106, sz=10)]))


# ------------------------------------------------------------- 지표 발표 직전
def test_event_entry_window(base):
    cfg, store = base
    at = T0 + 60 * MIN
    eng = RuleEngine(cfg, store, now_fn=Clock(T0), events=[(at, "미국 CPI")])
    early = fill(coin="SOL", px=100, sz=1, ts=at - 45 * MIN)     # 45분 전 → 범위 밖
    near = fill(coin="SOL", px=100, sz=1, ts=at - 18 * MIN)      # 18분 전 → 감지
    after = fill(coin="SOL", px=100, sz=1, ts=at + 3 * MIN)      # 3분 후 → 감지
    late = fill(coin="SOL", px=100, sz=1, ts=at + 20 * MIN)      # 20분 후 → 범위 밖
    close = fill(coin="SOL", action="close", px=100, sz=1, start=1, ts=at - 5 * MIN)  # 청산은 제외
    vs = [v for v in run(eng, store, [snap([])], [early, near, after, late, close]) if v.rule == "event_entry"]
    assert len(vs) == 2
    assert "미국 CPI 발표 18분 전" in vs[0].message and "발표 3분 후" in vs[1].message


def test_load_events(tmp_path):
    p = tmp_path / "e.yaml"
    p.write_text('events:\n  - {at: "2026-12-10 13:30", name: "미국 CPI"}\n  - {at: "bad", name: x}\n',
                 encoding="utf-8")
    cfg = deep_merge(DEFAULTS, {"rules": {"event_entry": {"extra": [{"at": "2026-12-01 15:00", "name": "고용"}]}}})
    ev = load_events(cfg, p)
    assert [n for _, n in ev] == ["고용", "미국 CPI"]
    assert ev[1][0] == 1796909400000   # 2026-12-10 13:30 UTC


def test_shipped_events_file_parses():
    ev = load_events()
    assert len(ev) >= 20 and all(n for _, n in ev)


# ------------------------------------------------------------- 승리 후 과신
def _history(store, n=6, notional_sz=10):
    """과거 신규 진입 n건 (각 $1,000)."""
    store.add_fills([fill(coin="SOL", px=100, sz=notional_sz, ts=T0 - 2 * DAY_MS - i * MIN) for i in range(n)])


def _closes(store, pnls, base_ts):
    store.add_fills([fill(coin="SOL", action="close", px=100, sz=1, start=1, pnl=p,
                          ts=base_ts + i * MIN) for i, p in enumerate(pnls)])


def test_overconfident_after_streak(base):
    cfg, store = base
    _history(store)
    _closes(store, [50, 30, 80], T0 - 30 * MIN)                   # 3연승
    eng = RuleEngine(cfg, store, now_fn=Clock(T0))
    big = fill(coin="SOL", px=100, sz=17, ts=T0)                  # $1,700 = 1.7배
    vs = run(eng, store, [snap([])], [big])
    oc = [v for v in vs if v.rule == "overconfident"]
    assert len(oc) == 1 and "3연승" in oc[0].message and "1.7배" in oc[0].message
    assert "size_spike" not in rules_of(vs)                       # 2배 미만이라 사이즈 급증은 아님


def test_overconfident_not_double_counted_as_spike(base):
    cfg, store = base
    _history(store)
    _closes(store, [50, 30, 80], T0 - 30 * MIN)
    eng = RuleEngine(cfg, store, now_fn=Clock(T0))
    vs = rules_of(run(eng, store, [snap([])], [fill(coin="SOL", px=100, sz=30, ts=T0)]))  # 3배
    assert vs.count("overconfident") == 1 and "size_spike" not in vs


def test_overconfident_needs_full_streak_and_size(base):
    cfg, store = base
    _history(store)
    _closes(store, [50, -10, 80], T0 - 30 * MIN)                  # 연승 아님
    eng = RuleEngine(cfg, store, now_fn=Clock(T0))
    assert "overconfident" not in rules_of(run(eng, store, [snap([])], [fill(coin="SOL", px=100, sz=17)]))
    _closes(store, [10, 10, 10], T0 + MIN)                        # 이제 3연승
    eng.now_fn = Clock(T0 + 10 * MIN)
    normal = fill(coin="SOL", px=100, sz=12, ts=T0 + 10 * MIN)    # 1.2배 → 정상
    assert "overconfident" not in rules_of(run(eng, store, [snap([])], [normal]))


# ------------------------------------------------------------- 소급 계산
def test_retro_finds_past_mistakes_once(base):
    cfg, store = base
    d = T0 - 40 * DAY_MS
    store.add_fills([
        fill(coin="SOL", action="close", px=90, sz=1, start=1, pnl=-100, ts=d),
        fill(coin="SOL", px=90, sz=1, ts=d + 5 * MIN),                     # 5분 만에 재진입 → 복구 매매
        fill(coin="BTC", action="close", px=1, sz=1, start=1, pnl=-500, ts=d + DAY_MS, liq=True, oid="L1"),
        fill(coin="SOL", px=90, sz=1, ts=d + 2 * DAY_MS + 20 * MIN),       # 쿨다운 밖 → 정상
    ])
    found = run_retro(cfg, store)
    vs = store.violations_between(0)
    assert found == 2 and sorted(v.rule for v in vs) == ["liquidation", "revenge"]
    assert all(v.source == "retro" for v in vs)
    assert vs[0].ts == d + 5 * MIN                                       # 그 당시 시각으로 기록
    assert run_retro(cfg, store) is None                                 # 한 번만 실행
    assert len(store.violations_between(0)) == 2


def test_retro_does_not_peek_future(base):
    """소급 계산은 그 시점 이후 체결로 '평소 규모'를 계산하지 않는다."""
    cfg, store = base
    store.add_fills([fill(coin="SOL", px=100, sz=10, ts=T0 - DAY_MS + i * MIN) for i in range(6)])  # $1,000 ×6
    store.add_fills([fill(coin="SOL", px=100, sz=30, ts=T0)])                                       # $3,000
    store.add_fills([fill(coin="SOL", px=100, sz=100, ts=T0 + DAY_MS + i * MIN) for i in range(10)])  # 이후 큰 진입들
    run_retro(cfg, store)
    spikes = [v for v in store.violations_between(0) if v.rule == "size_spike"]
    assert any("$3,000" in v.message for v in spikes)


def test_live_after_retro_no_double_count(base):
    cfg, store = base
    d = T0 - 3 * MIN
    loss = fill(coin="SOL", action="close", px=90, sz=1, start=1, pnl=-100, ts=d)
    again = fill(coin="SOL", px=90, sz=1, ts=d + MIN)
    store.add_fills([loss, again])
    run_retro(cfg, store)
    eng = RuleEngine(cfg, store, now_fn=Clock(T0))
    eng.process([snap([])], [loss, again])     # 같은 체결이 다시 들어와도
    assert len([v for v in store.violations_between(0) if v.rule == "revenge"]) == 1


# ------------------------------------------------------------- 기록 집계
def test_every_rule_has_kind():
    for rule in DEFAULTS["score"]["penalty"]:
        assert RULE_KIND.get(rule) in KINDS, rule


def test_history_periods_and_rates(base):
    cfg, store = base
    now = T0
    # 최근 30일: 진입 10회, 실수 2건 / 1달 전: 진입 5회, 실수 3건
    store.add_fills([fill(coin="SOL", px=100, sz=1, ts=now - 5 * DAY_MS + i * MIN) for i in range(10)])
    store.add_fills([fill(coin="SOL", px=100, sz=1, ts=now - 40 * DAY_MS + i * MIN) for i in range(5)])
    from monitor.store import Violation
    for i, (rule, ts) in enumerate([("revenge", now - 5 * DAY_MS), ("no_stop", now - 5 * DAY_MS),
                                    ("leverage", now - 40 * DAY_MS), ("leverage", now - 40 * DAY_MS),
                                    ("liquidation", now - 41 * DAY_MS)]):
        store.add_violation(Violation(rule=rule, message="m", penalty=10, ts=ts, dedupe_key=str(i)))
    h = summary(cfg, store, now)
    cur, prev = h["periods"][0], h["periods"][1]
    assert cur["total"] == 2 and cur["entries"] == 10 and cur["per10"] == 2.0
    assert cur["by_kind"]["revenge"] == 1 and cur["by_kind"]["no_stop"] == 1
    assert prev["total"] == 3 and prev["by_kind"]["oversize"] == 2 and prev["per10"] == 6.0
    assert cur["avg_score"] == 80.0                                 # 하루에 −20
    assert len(h["monthly"]) == 12 and h["monthly"][-1]["month"] == "2027-01"
    assert h["log"][0]["kind"] in KINDS and len(h["log"]) == 5


def test_rest_day_entry(base):
    cfg, store = base
    from monitor.rules import day_label
    store.set_kv("rest_day", day_label(T0))
    eng = RuleEngine(cfg, store, now_fn=Clock(T0))
    vs = rules_of(run(eng, store, [snap([])], [fill(coin="SOL", px=100, sz=1, ts=T0)]))
    assert "rest_day_entry" in vs
    tomorrow = fill(coin="SOL", px=100, sz=1, ts=T0 + DAY_MS)
    eng.now_fn = Clock(T0 + DAY_MS)
    assert "rest_day_entry" not in rules_of(run(eng, store, [snap([])], [tomorrow]))


# ------------------------------------------------------------- PC 실사용 중 발견한 문제
def test_daily_loss_base_survives_partial_or_bad_reads(base):
    """한쪽 거래소 조회 실패·순간적인 자본 0 값에도 일 손실 기준이 0 으로 떨어지지 않음."""
    cfg, store = base
    from monitor.models import AccountSnapshot
    eng = RuleEngine(cfg, store, now_fn=Clock(T0))
    hl = AccountSnapshot(exchange="hyperliquid", ts=T0, equity=0.0)        # 잘못 읽힌 값
    eng.process([hl], [])
    by = AccountSnapshot(exchange="bybit", ts=T0, equity=80_000.0)
    eng.process([by], [])
    eng.process([AccountSnapshot(exchange="hyperliquid", ts=T0, equity=20_000.0), by], [])
    assert eng.state["day_start_equity"] == 100_000.0
    # 작은 손실(-$100) 뒤 진입은 한도(5% = $5,000) 위반 아님
    store.add_fills([fill(coin="SOL", action="close", px=90, sz=1, start=1, pnl=-100, ts=T0 + MIN)])
    vs = run(eng, store, [by], [fill(coin="SOL", px=90, sz=1, ts=T0 + 2 * MIN)])
    assert "daily_loss_limit" not in rules_of(vs)


def test_hl_discovers_hip3_dex_from_fills_and_scan():
    from monitor.exchanges.hyperliquid import HyperliquidClient
    from tests.test_hyperliquid import WALLET

    class S:
        def __init__(self):
            self.calls = []

        def post(self, url, json=None, timeout=None):
            self.calls.append(json)
            t = json["type"]

            class R:
                status_code = 200

                def __init__(self, d):
                    self.d = d

                def json(self):
                    return self.d
            if t == "perpDexs":
                return R([None, {"name": "xyz"}, {"name": "flx"}])
            if t == "clearinghouseState":
                has = json.get("dex") == "xyz"
                return R({"marginSummary": {"accountValue": "0"},
                          "assetPositions": [{"position": {"coin": "xyz:NVDA", "szi": "1", "entryPx": "180",
                                                           "positionValue": "181", "unrealizedPnl": "1",
                                                           "liquidationPx": None, "leverage": {"type": "isolated", "value": 5},
                                                           "marginUsed": "36"}}] if has else []})
            if t == "frontendOpenOrders":
                return R([])
            if t == "userAbstraction":
                return R("default")
            if t == "userFillsByTime":
                return R([{"coin": "abc:TSLA", "px": "1", "sz": "1", "side": "B", "time": 1, "startPosition": "0",
                           "dir": "Open Long", "closedPnl": "0", "hash": "h", "oid": 1, "crossed": True,
                           "fee": "0", "tid": 7, "feeToken": "USDC"}])
            return R({})
    s = S()
    c = HyperliquidClient(WALLET, dexes=[""], session=s)
    snap = c.snapshot()
    assert c.dexes == ["", "xyz"]                                  # 포지션 있는 마켓만 추가
    assert [p.coin for p in snap.positions] == ["xyz:NVDA"]
    c.fills_since(0)
    assert "abc" in c.dexes                                         # 체결로 본 마켓도 추가
    n = len(s.calls)
    c.snapshot()                                                    # 10분 안에는 재탐색 안 함
    assert not any(x["type"] == "perpDexs" for x in s.calls[n:])


def test_bybit_unified_transient_error_not_masked():
    from monitor.exchanges.bybit import BybitClient, BybitError

    class R:
        status_code = 200

        def __init__(self, d):
            self.d = d

        def json(self):
            return self.d

    class S:
        def get(self, url, headers=None, timeout=None):
            if "UNIFIED" in url:
                return R({"retCode": 10006, "retMsg": "Too many visits"})
            return R({"retCode": 10001, "retMsg": "accountType only support UNIFIED."})
    c = BybitClient("k", "s", session=S())
    with pytest.raises(BybitError) as e:
        c._equity()
    assert e.value.code == 10006


def test_poll_survives_print_failure(monkeypatch, tmp_path):
    import builtins
    import run as run_mod

    class Bad:
        def snapshot(self):
            raise RuntimeError("down — 접속 실패")

        def fills_since(self, s):
            return []

    def boom(*a, **k):
        raise UnicodeEncodeError("cp949", "—", 0, 1, "x")
    monkeypatch.setattr(builtins, "print", boom)
    st = Store(tmp_path / "p.db")
    snaps, fills = run_mod.poll(st, {"hyperliquid": Bad()})
    assert snaps == [] and fills == []


def test_cleanup_zero_daily_limit_once(base):
    cfg, store = base
    from monitor.store import Violation
    store.add_violation(Violation(rule="daily_loss_limit", message="[bybit] ZEC 일 손실 한도($0) 도달 후 신규 진입",
                                  penalty=25, ts=1, dedupe_key="a"))
    store.add_violation(Violation(rule="daily_loss_limit", message="[bybit] ZEC 일 손실 한도($4,000) 도달 후 신규 진입",
                                  penalty=25, ts=1, dedupe_key="b"))
    assert store.cleanup_once() == 1 and store.cleanup_once() == 0
    assert len(store.violations_between(0)) == 1
