"""규칙 엔진: 스냅샷과 새 체결을 보고 위반을 찾아 점수를 깎는다.

규칙 목록 (config.yaml 의 rules 항목과 대응)
  leverage / account_leverage : 실효 레버리지 한도 초과
  no_stop                     : 진입 후 일정 시간 SL 없음 (계획 손절가가 있으면 면제)
  stop_moved                  : SL 을 손실이 커지는 방향으로 이동
  stop_removed                : SL 을 취소하고 일정 시간 재설정하지 않음
  stop_breach                 : 계획 손절가를 지났는데 계속 보유
  averaging_down              : 손실 중 추가 진입 (계획 진입가 근처는 예외)
  liq_distance                : 청산가가 너무 가까움
  risk_per_trade              : 손절 시 예상 손실이 자본 대비 한도 초과
  revenge                     : 손실 청산 직후 재진입
  overtrading                 : 하루 진입 횟수가 평소보다 비정상적으로 많음
  size_spike                  : 신규 포지션 규모가 평소보다 비정상적으로 큼
  daily_loss_limit            : 일 손실 한도 도달 후 신규 진입
  liquidation                 : 강제 청산 발생
  fomo                        : 계획 없이 급등락 직후 그 방향으로 추격 진입
  event_entry                 : 주요 지표 발표(CPI·FOMC 등) 직전 진입
  overconfident               : 연속 수익 직후 평소보다 큰 진입
  rest_day_entry              : 강제 청산 후 "오늘은 쉰다"를 고른 날 신규 진입

같은 상황에서 경고가 반복되지 않도록, 지속형 조건(레버리지·청산가 등)은
조건이 풀렸다가 다시 생길 때만 새로 기록한다. 상태는 DB 에 저장해 재시작해도 유지된다.
"""
from __future__ import annotations

import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from monitor.config import max_leverage_for
from monitor.mistakes import REST_DAY_KEY
from monitor.models import AccountSnapshot, Fill, Position
from monitor.store import Store, Violation

DAY_MS = 86_400_000
STATE_KEY = "engine_state"
HYSTERESIS = 0.95          # 레버리지가 한도의 95% 아래로 내려가야 "해소"로 봄
LIQ_CLEAR_MARGIN = 2.0     # 청산가 거리가 기준 + 2%p 이상 회복돼야 "해소"로 봄
PRICE_EPS = 0.0005         # 가격 비교 시 0.05% 미만 차이는 같은 값으로 취급
MIN_SPIKE_SAMPLES = 5      # 사이즈 급증 판단에 필요한 과거 진입 건수


def day_start_ms(ts_ms: int) -> int:
    return ts_ms - ts_ms % DAY_MS


def day_label(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _fmt(x: float) -> str:
    return f"{x:,.6g}"


class RuleEngine:
    def __init__(self, cfg: dict[str, Any], store: Store,
                 now_fn: Callable[[], int] = lambda: int(time.time() * 1000),
                 market: Optional[dict[str, Any]] = None,
                 events: Optional[list[tuple[int, str]]] = None,
                 state_key: Optional[str] = STATE_KEY, source: str = "live"):
        """market: 거래소 이름 → price_range(coin, start_ms, end_ms) 를 가진 객체 (FOMO 판단용)
        events: [(발표 시각 ms, 이름)] (뇌동매매 판단용)
        state_key: 상태 저장 키. None 이면 저장하지 않음 (소급 계산용)"""
        self.cfg = cfg
        self.rules = cfg["rules"]
        self.penalties = cfg["score"]["penalty"]
        self.store = store
        self.now_fn = now_fn
        self.market = market or {}
        self.events = events or []
        self.state_key = state_key
        self.source = source
        self.state: dict[str, Any] = (store.get_kv(state_key) if state_key else None) or {}
        self.state.setdefault("positions", {})   # 포지션별 추적 정보
        self.state.setdefault("active", {})      # 지속형 조건: key → 시작 시각
        self.state.setdefault("last_loss", None) # 마지막 손실 청산 {ts, coin, exchange, notional}
        self.state.setdefault("day", None)
        self.state.setdefault("day_start_equity", None)
        self.state.setdefault("fomo_checked", [])   # FOMO 를 이미 확인한 주문 (가격 조회 반복 방지)

    # ================================================================ 진입점
    def process(self, snapshots: list[AccountSnapshot], new_fills: list[Fill]) -> list[Violation]:
        now = self.now_fn()
        out: list[Violation] = []
        self._roll_day(now, snapshots)

        # 체결 기반 규칙은 직전 스냅샷의 포지션 정보(평단 등)가 필요하므로 상태 갱신 전에 실행
        fills = sorted(new_fills, key=lambda f: (f.ts, f.tid))
        out += self._fill_rules(fills)
        out += self._daily_loss_rule(now)
        out += self._overtrading_rule(now, bool(fills))
        out += self._overconfident_rule(fills)   # 과신이 먼저: 같은 진입은 사이즈 급증으로 두 번 세지 않음
        out += self._size_spike_rule(fills)
        out += self._event_rule(fills)
        out += self._fomo_rule(fills)

        for snap in snapshots:
            out += self._account_rules(snap, now)
            for pos in snap.positions:
                out += self._position_rules(snap, pos, now)
        self._update_positions(snapshots, now)

        if self.state_key:
            self.store.set_kv(self.state_key, self.state)
        return out

    def process_fills(self, new_fills: list[Fill]) -> list[Violation]:
        """체결만으로 판단 가능한 규칙 (과거 기록 소급 계산용). 포지션·주문 상태 규칙은 제외."""
        now = self.now_fn()
        fills = sorted(new_fills, key=lambda f: (f.ts, f.tid))
        out = self._fill_rules(fills)
        out += self._overtrading_rule(now, bool(fills))
        out += self._overconfident_rule(fills)
        out += self._size_spike_rule(fills)
        out += self._event_rule(fills)
        out += self._fomo_rule(fills)
        return out

    # ============================================================ 공통 도우미
    def _emit(self, rule: str, msg: str, ts: int, dedupe: Optional[str],
              exchange: Optional[str] = None, coin: Optional[str] = None) -> list[Violation]:
        v = Violation(rule=rule, message=msg, penalty=int(self.penalties.get(rule, 10)),
                      ts=ts, exchange=exchange, coin=coin, dedupe_key=dedupe, source=self.source)
        return [v] if self.store.add_violation(v) else []

    def _sustained(self, key: str, is_on: bool, is_clear: bool, now: int,
                   rule: str, msg: str, exchange: str, coin: Optional[str]) -> list[Violation]:
        """지속형 조건: 켜질 때 한 번 기록, 해소 조건을 만족하면 다시 대기."""
        active = self.state["active"]
        if is_on and key not in active:
            active[key] = now
            return self._emit(rule, msg, now, f"{key}:{now}", exchange, coin)
        if key in active and is_clear:
            del active[key]
        return []

    def _upto(self) -> int:
        """조회 상한. 소급 계산 중에는 '그 시점'보다 뒤의 체결을 보지 않는다.
        실시간에는 거래소 시계가 PC 보다 조금 빠를 수 있어 상한을 두지 않음."""
        return self.now_fn() + 1 if self.source == "retro" else 2**62

    def _roll_day(self, now: int, snapshots: list[AccountSnapshot]) -> None:
        """일 손실 한도의 기준 자본 = 거래소별 '오늘 확인된 최대 자본'의 합.
        한 거래소 조회가 실패하거나 일시적으로 잘못 읽혀도 기준이 0 으로 떨어지지 않는다."""
        label = day_label(now)
        if self.state["day"] != label:
            self.state["day"] = label
            self.state["day_equity"] = {}
        per = self.state.setdefault("day_equity", {})
        for s in snapshots:
            if s.equity > 0:
                per[s.exchange] = max(per.get(s.exchange, 0.0), s.equity)
        self.state["day_start_equity"] = sum(per.values()) or None

    def _stop_for(self, snap: AccountSnapshot, pos: Position) -> Optional[float]:
        """현재 가장 보호적인 SL 트리거가 (롱: 가장 높은 것, 숏: 가장 낮은 것)."""
        trig = [o.trigger_px for o in snap.stop_orders_for(pos) if o.trigger_px]
        if not trig:
            return None
        return max(trig) if pos.side == "long" else min(trig)

    # ============================================================ 계좌 규칙
    def _account_rules(self, snap: AccountSnapshot, now: int) -> list[Violation]:
        lev = snap.effective_leverage()
        if lev is None:
            return []
        limit = float(self.rules["leverage"]["account_max"])
        return self._sustained(
            f"acclev:{snap.exchange}", lev > limit, lev <= limit * HYSTERESIS, now,
            "account_leverage",
            f"[{snap.exchange}] 계좌 전체 실효 레버리지 {lev:.1f}배 — 한도 {limit:g}배 초과",
            snap.exchange, None)

    # ============================================================ 포지션 규칙
    def _position_rules(self, snap: AccountSnapshot, pos: Position, now: int) -> list[Violation]:
        out: list[Violation] = []
        pk = pos.key
        tag = f"[{pos.exchange}] {pos.coin} {pos.side.upper()}"
        st = self.state["positions"].get(pk)
        first_seen = st["first_seen"] if st else now
        plan = self.store.active_plan_for(pos.exchange, pos.coin, pos.side)
        stop = self._stop_for(snap, pos)

        # 1. 레버리지
        lev = snap.effective_leverage(pos)
        if lev is not None:
            limit = max_leverage_for(self.cfg, pos.coin)
            out += self._sustained(
                f"lev:{pk}", lev > limit, lev <= limit * HYSTERESIS, now, "leverage",
                f"{tag} 실효 레버리지 {lev:.1f}배 — 한도 {limit:g}배 초과", pos.exchange, pos.coin)

        # 2. SL 미설정 (계획 손절가가 있으면 그 가격을 감시하므로 면제)
        grace_ms = float(self.rules["stop_loss_grace_min"]) * 60_000
        has_plan_stop = plan is not None and plan.stop_px is not None
        if stop is None and not has_plan_stop and now - first_seen >= grace_ms:
            out += self._emit("no_stop",
                              f"{tag} 진입 후 {self.rules['stop_loss_grace_min']:g}분 넘게 손절 주문 없음",
                              now, f"nostop:{pk}:{first_seen}", pos.exchange, pos.coin)

        # 3a. SL 이동·취소
        if st is not None:
            last = st.get("last_stop")
            if stop is not None:
                st["stop_missing_since"] = None
                if last is not None:
                    widened = (stop < last * (1 - PRICE_EPS)) if pos.side == "long" \
                        else (stop > last * (1 + PRICE_EPS))
                    if widened:
                        out += self._emit("stop_moved",
                                          f"{tag} 손절가를 {_fmt(last)} → {_fmt(stop)} 로 손실 확대 방향으로 이동",
                                          now, f"stopmove:{pk}:{now}", pos.exchange, pos.coin)
            elif last is not None:
                since = st.get("stop_missing_since") or now
                st["stop_missing_since"] = since
                breach_ms = float(self.rules["stop_breach_grace_sec"]) * 1000
                if now - since >= breach_ms:
                    out += self._emit("stop_removed",
                                      f"{tag} 손절 주문({_fmt(last)})이 취소된 뒤 재설정되지 않음",
                                      now, f"stopremoved:{pk}:{since}", pos.exchange, pos.coin)

        # 3b. 계획 손절가 이탈 후 보유
        if has_plan_stop and st is not None:
            crossed = pos.mark_px <= plan.stop_px if pos.side == "long" else pos.mark_px >= plan.stop_px
            if crossed:
                since = st.get("breach_since") or now
                st["breach_since"] = since
                if now - since >= float(self.rules["stop_breach_grace_sec"]) * 1000:
                    out += self._emit("stop_breach",
                                      f"{tag} 계획 손절가 {_fmt(plan.stop_px)} 이탈 후에도 보유 중 "
                                      f"(현재 {_fmt(pos.mark_px)})",
                                      now, f"breach:{pk}:{since}", pos.exchange, pos.coin)
            else:
                st["breach_since"] = None

        # 5. 청산가 거리
        dist = pos.liq_distance_pct()
        if dist is not None:
            mn = float(self.rules["liq_distance_min_pct"])
            out += self._sustained(
                f"liq:{pk}", dist < mn, dist >= mn + LIQ_CLEAR_MARGIN, now, "liq_distance",
                f"{tag} 청산가 {_fmt(pos.liq_px)} 까지 {dist:.1f}% — 기준 {mn:g}% 미만",
                pos.exchange, pos.coin)

        # 6. 1회 리스크 (SL 주문 우선, 없으면 계획 손절가)
        risk_stop = stop if stop is not None else (plan.stop_px if has_plan_stop else None)
        if risk_stop is not None and snap.equity > 0:
            per_unit = (pos.entry_px - risk_stop) if pos.side == "long" else (risk_stop - pos.entry_px)
            loss = max(0.0, per_unit) * pos.size
            pct = loss / snap.equity * 100
            lim = float(self.rules["max_risk_per_trade_pct"])
            if pct > lim:
                out += self._emit("risk_per_trade",
                                  f"{tag} 손절 시 예상 손실 ${loss:,.0f} (자본의 {pct:.1f}%) — 한도 {lim:g}% 초과",
                                  now, f"risk:{pk}:{pos.size:.10g}:{risk_stop:.10g}",
                                  pos.exchange, pos.coin)
        return out

    def _update_positions(self, snapshots: list[AccountSnapshot], now: int) -> None:
        tracked = self.state["positions"]
        seen: set[str] = set()
        for snap in snapshots:
            for pos in snap.positions:
                pk = pos.key
                seen.add(pk)
                st = tracked.setdefault(pk, {"first_seen": now, "last_stop": None,
                                             "stop_missing_since": None, "breach_since": None})
                stop = self._stop_for(snap, pos)
                if stop is not None:
                    st["last_stop"] = stop
                st.update(size=pos.size, entry=pos.entry_px, upnl=pos.upnl, last_seen=now)
        # 조회에 성공한 거래소에서 사라진 포지션 = 청산됨 → 추적 종료, 연결된 계획 완료 처리
        live_exchanges = {s.exchange for s in snapshots}
        for pk in list(tracked):
            # 키 형식 "거래소:코인:방향". 코인에 ":" 가 들어갈 수 있음 (HIP-3 "xyz:TSLA")
            exch, rest = pk.split(":", 1)
            if pk in seen or exch not in live_exchanges:
                continue
            coin, side = rest.rsplit(":", 1)
            plan = self.store.active_plan_for(exch, coin, side)
            if plan is not None and plan.created_ts <= tracked[pk].get("last_seen", now):
                self.store.close_plan(plan.id, status="done")
            del tracked[pk]
            for key in [k for k in self.state["active"] if k.endswith(f":{pk}")]:
                del self.state["active"][key]

    # ============================================================ 체결 규칙
    def _fill_rules(self, fills: list[Fill]) -> list[Violation]:
        out: list[Violation] = []
        cooldown_ms = float(self.rules["revenge_cooldown_min"]) * 60_000
        tol = float(self.rules["plan_entry_tolerance_pct"]) / 100

        # 강제 청산: 한 청산 주문(oid)의 여러 체결을 묶어 손실 합계로 한 번 기록
        liq_loss: dict[tuple, float] = defaultdict(float)
        liq_first: dict[tuple, Fill] = {}
        for f in fills:
            if f.is_liquidation:
                k = (f.exchange, f.oid)
                liq_loss[k] += f.closed_pnl - f.fee
                liq_first.setdefault(k, f)
        for k, f in liq_first.items():
            out += self._emit("liquidation",
                              f"[{f.exchange}] {f.coin} {(f.position_side or '').upper()} 강제 청산 발생 "
                              f"— 실현손실 ${-liq_loss[k]:,.2f}",
                              f.ts, f"liquidated:{f.exchange}:{f.oid}", f.exchange, f.coin)

        rest_day = self.store.get_kv(REST_DAY_KEY)
        for f in fills:
            tag = f"[{f.exchange}] {f.coin}"
            opening = f.action in ("open", "flip")

            # 쉬기로 한 날의 신규 진입
            if opening and rest_day and day_label(f.ts) == rest_day:
                out += self._emit("rest_day_entry", f"{tag} 오늘은 쉬기로 했는데 신규 진입",
                                  f.ts, f"rest:{f.exchange}:{f.oid}", f.exchange, f.coin)

            # 7. 복수매매: 손실 청산 후 쿨다운 안에 진입
            last = self.state["last_loss"]
            if opening and last and 0 <= f.ts - last["ts"] <= cooldown_ms and f.oid != last.get("oid"):
                mins = (f.ts - last["ts"]) / 60_000
                bigger = f.notional > last["notional"] * 1.05
                msg = (f"{tag} 손실 청산({last['coin']}) {mins:.0f}분 만에 재진입"
                       + (" — 직전보다 큰 규모" if bigger else ""))
                out += self._emit("revenge", msg, f.ts, f"revenge:{f.exchange}:{f.oid}",
                                  f.exchange, f.coin)
            if f.action in ("close", "flip") and f.closed_pnl < 0:
                if f.action == "flip":  # 손실 상태에서 즉시 반대 방향 진입 = 복수매매
                    out += self._emit("revenge", f"{tag} 손실 청산과 동시에 반대 방향 진입(스위칭)",
                                      f.ts, f"revenge:{f.exchange}:{f.oid}", f.exchange, f.coin)
                self.state["last_loss"] = {"ts": f.ts, "coin": f.coin, "exchange": f.exchange,
                                           "oid": f.oid, "notional": f.notional}

            # 4. 물타기: 같은 방향 포지션에 손실 중 추가
            if (self.rules.get("averaging_down", True) and f.action == "open"
                    and f.start_position != 0 and f.position_side):
                pk = f"{f.exchange}:{f.coin}:{f.position_side}"
                st = self.state["positions"].get(pk)
                if st and st.get("entry"):
                    entry = st["entry"]
                    worse = (f.px < entry * (1 - PRICE_EPS)) if f.position_side == "long" \
                        else (f.px > entry * (1 + PRICE_EPS))
                    if worse:
                        plan = self.store.active_plan_for(f.exchange, f.coin, f.position_side)
                        planned = plan is not None and any(
                            abs(f.px - e) / e <= tol for e in plan.entries)
                        if not planned:
                            out += self._emit(
                                "averaging_down",
                                f"{tag} {f.position_side.upper()} 손실 중 추가 진입 "
                                f"(평단 {_fmt(entry)}, 추가 {_fmt(f.px)}) — 계획에 없는 물타기",
                                f.ts, f"avg:{f.exchange}:{f.oid}", f.exchange, f.coin)
        return out

    def _daily_loss_rule(self, now: int) -> list[Violation]:
        """10. 당일 실현손실이 한도에 닿은 뒤의 신규 진입."""
        eq = self.state.get("day_start_equity")
        if not eq or eq <= 0:
            return []
        limit = eq * float(self.rules["daily_loss_limit_pct"]) / 100
        cum = 0.0
        hit = False
        out: list[Violation] = []
        for f in self.store.fills_between(day_start_ms(now)):
            if hit and f.action in ("open", "flip"):
                out += self._emit("daily_loss_limit",
                                  f"[{f.exchange}] {f.coin} 일 손실 한도(${limit:,.0f}) 도달 후 신규 진입",
                                  f.ts, f"dailyloss:{f.exchange}:{f.oid}", f.exchange, f.coin)
            cum += f.closed_pnl - f.fee
            if cum <= -limit:
                hit = True
        return out

    def _open_orders_by_day(self, start_ms: int, end_ms: int) -> dict[int, set]:
        days: dict[int, set] = defaultdict(set)
        for f in self.store.fills_between(start_ms, end_ms):
            if f.action in ("open", "flip"):
                days[day_start_ms(f.ts)].add((f.exchange, f.oid))
        return days

    def _overtrading_rule(self, now: int, has_new: bool) -> list[Violation]:
        """8. 오늘 진입 횟수 > max(최소 기준, 평균 + k·표준편차)."""
        if not has_new:
            return []
        ot = self.rules["overtrading"]
        today = day_start_ms(now)
        lookback = int(ot["lookback_days"])
        history = self._open_orders_by_day(today - lookback * DAY_MS, today)
        today_count = len(self._open_orders_by_day(today, min(today + DAY_MS, self._upto())).get(today, ()))
        threshold = float(ot["min_trades_floor"])
        if history:
            first_day = min(history)
            counts = [len(history.get(d, ())) for d in range(first_day, today, DAY_MS)]
            if len(counts) >= 2:
                threshold = max(threshold,
                                statistics.mean(counts) + float(ot["sigma"]) * statistics.pstdev(counts))
        if today_count > threshold:
            return self._emit("overtrading",
                              f"오늘 진입 {today_count}회 — 평소 기준 {threshold:.0f}회 초과 (과매매)",
                              now, f"overtrade:{day_label(now)}")
        return []

    def _size_spike_rule(self, fills: list[Fill]) -> list[Violation]:
        """9. 신규 포지션 규모 > 과거 신규 진입 중앙값 × n."""
        if not any(f.action in ("open", "flip") for f in fills):
            return []
        book = self._opener_book(fills)
        notional, openers, coin_of = book["notional"], book["openers"], book["coin"]
        candidates = book["candidates"]
        mult = float(self.rules["size_spike_multiple"])
        out: list[Violation] = []
        for k in candidates:
            exch, oid = k
            coin = coin_of.get(k, "")
            past = [notional[o] for o in openers if o != k and notional[o] > 0]
            if len(past) < MIN_SPIKE_SAMPLES or self.store.has_violation(f"overconf:{exch}:{oid}"):
                continue
            med = statistics.median(past)
            if notional[k] > med * mult:
                out += self._emit("size_spike",
                                  f"[{exch}] {coin} 신규 진입 ${notional[k]:,.0f} — 평소(중앙값 ${med:,.0f})의 "
                                  f"{notional[k] / med:.1f}배",
                                  self.now_fn(), f"spike:{exch}:{oid}", exch, coin)
        return out

    # ------------------------------------------------------------ 신규 진입 집계
    def _opener_book(self, fills: list[Fill]) -> dict[str, Any]:
        """최근 lookback 기간의 신규 진입 주문별 규모. 현재 시각 이후 체결은 보지 않음 (소급 계산 대비).
        candidates = 이번 체결이 들어온 신규 진입 주문 (분할 체결분이 나중에 들어와도 다시 평가)"""
        ot = self.rules["overtrading"]
        since = min(f.ts for f in fills) - int(ot["lookback_days"]) * DAY_MS
        notional: dict[tuple, float] = defaultdict(float)
        openers: set[tuple] = set()   # 0 에서 새로 연 주문 (또는 방향 전환)
        coin_of: dict[tuple, str] = {}
        first_ts: dict[tuple, int] = {}
        side_of: dict[tuple, str] = {}
        closes: dict[tuple, list] = {}  # 청산 주문별 [마지막 시각, 손익 합계]
        for f in self.store.fills_between(since, self._upto()):
            k = (f.exchange, f.oid)
            if f.action == "flip" or (f.action == "open" and f.start_position == 0):
                openers.add(k)
            if f.action in ("open", "flip"):
                notional[k] += f.notional
                coin_of[k] = f.coin
                first_ts.setdefault(k, f.ts)
                side_of.setdefault(k, f.position_side or "")
            if f.action in ("close", "flip") and (f.closed_pnl or f.fee):
                c = closes.setdefault(k, [f.ts, 0.0])
                c[0] = max(c[0], f.ts)
                c[1] += f.closed_pnl - f.fee
        return {"notional": notional, "openers": openers, "coin": coin_of, "first_ts": first_ts,
                "side": side_of, "closes": closes,
                "candidates": {(f.exchange, f.oid) for f in fills} & openers}

    # ============================================================ 신규 규칙
    def _overconfident_rule(self, fills: list[Fill]) -> list[Violation]:
        """승리 후 과신: 직전 청산 N건이 모두 수익인 상태에서 평소의 M배 넘는 신규 진입."""
        if not any(f.action in ("open", "flip") for f in fills):
            return []
        oc = self.rules["overconfident"]
        streak, mult = int(oc["win_streak"]), float(oc["size_multiple"])
        book = self._opener_book(fills)
        out: list[Violation] = []
        for k in book["candidates"]:
            t0 = book["first_ts"][k]
            prior = sorted((c for o, c in book["closes"].items() if o != k and c[0] < t0),
                           key=lambda c: c[0])[-streak:]
            if len(prior) < streak or not all(c[1] > 0 for c in prior):
                continue
            past = [book["notional"][o] for o in book["openers"]
                    if o != k and book["notional"][o] > 0 and book["first_ts"].get(o, t0) < t0]
            if len(past) < MIN_SPIKE_SAMPLES:
                continue
            med = statistics.median(past)
            size = book["notional"][k]
            if size > med * mult:
                exch, oid = k
                out += self._emit("overconfident",
                                  f"[{exch}] {book['coin'][k]} {streak}연승 직후 진입 ${size:,.0f} "
                                  f"— 평소(중앙값 ${med:,.0f})의 {size / med:.1f}배",
                                  t0, f"overconf:{exch}:{oid}", exch, book["coin"][k])
        return out

    def _event_rule(self, fills: list[Fill]) -> list[Violation]:
        """뇌동매매: 지표 발표 before_min 분 전 ~ after_min 분 후 사이의 진입."""
        if not self.events:
            return []
        ev = self.rules["event_entry"]
        before, after = float(ev["before_min"]) * 60_000, float(ev["after_min"]) * 60_000
        out: list[Violation] = []
        for f in fills:
            if f.action not in ("open", "flip"):
                continue
            for at, name in self.events:
                if at - before <= f.ts <= at + after:
                    mins = (at - f.ts) / 60_000
                    when = f"발표 {mins:.0f}분 전" if mins >= 0.5 else (
                        f"발표 {-mins:.0f}분 후" if mins <= -0.5 else "발표 시각")
                    out += self._emit("event_entry",
                                      f"[{f.exchange}] {f.coin} {name} {when} 진입",
                                      f.ts, f"event:{f.exchange}:{f.oid}", f.exchange, f.coin)
                    break
        return out

    def _fomo_rule(self, fills: list[Fill]) -> list[Violation]:
        """FOMO: 계획 없이, 직전 window 분 동안 move% 이상 움직인 방향으로 신규 진입."""
        if not self.market:
            return []
        fc = self.rules["fomo"]
        window, need = float(fc["window_min"]) * 60_000, float(fc["move_pct"])
        checked: list = self.state["fomo_checked"]
        firsts: dict[tuple, Fill] = {}
        for f in fills:
            if f.action == "flip" or (f.action == "open" and f.start_position == 0):
                firsts.setdefault((f.exchange, f.oid), f)
        out: list[Violation] = []
        for (exch, oid), f in firsts.items():
            key = f"{exch}:{oid}"
            if key in checked or exch not in self.market or not f.position_side:
                continue
            checked.append(key)
            del checked[:-300]
            if self.store.active_plan_for(exch, f.coin, f.position_side) is not None:
                continue
            try:
                rng = self.market[exch].price_range(f.coin, int(f.ts - window), int(f.ts))
            except Exception:  # noqa: BLE001 - 가격 조회 실패 시 이번 판단만 건너뜀
                continue
            if not rng:
                continue
            hi, lo = rng
            if f.position_side == "long" and lo > 0:
                move, word = (f.px - lo) / lo * 100, "급등"
            elif f.position_side == "short" and hi > 0:
                move, word = (hi - f.px) / hi * 100, "급락"
            else:
                continue
            if move >= need:
                out += self._emit("fomo",
                                  f"[{exch}] {f.coin} {f.position_side.upper()} — 진입 전 "
                                  f"{fc['window_min']:g}분간 {move:.1f}% {word} 직후, 매매 계획 없음",
                                  f.ts, f"fomo:{exch}:{oid}", exch, f.coin)
        return out


# ================================================================ 점수
def score_for_day(cfg: dict[str, Any], store: Store, ts_ms: int) -> int:
    start = day_start_ms(ts_ms)
    lost = sum(v.penalty for v in store.violations_between(start, start + DAY_MS))
    return max(0, int(cfg["score"]["start"]) - lost)
