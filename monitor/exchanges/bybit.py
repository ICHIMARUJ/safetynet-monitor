"""Bybit 수집기 (V5 API, 읽기 전용 키).

사용 요청 (모두 GET, 서명 필요)
  - /v5/account/wallet-balance : 계좌 자본 (통합 계정 totalEquity)
  - /v5/position/list          : 포지션 (USDT·USDC 무기한)
  - /v5/order/realtime         : 미체결 주문 (손절·익절 조건부 주문 포함)
  - /v5/execution/list         : 체결 내역 (한 번에 최대 7일, 페이지당 100건)
  - /v5/position/closed-pnl    : 주문 단위 청산 손익 (체결에는 손익이 없어서 별도 조회)
  - /v5/market/kline           : 1분봉 (공개 시세, 서명 없음 — FOMO 판단용)

Bybit 체결에는 "체결 직전 포지션 수량"이 없으므로, 현재 포지션에서 체결을 거꾸로 되짚어 계산한다.
(단방향 모드 기준. 헤지 모드는 안내 문구를 띄움)
손익은 Bybit 청산손익(수수료 차감 후 금액)을 그대로 쓰고, 이중 차감을 막기 위해 fee 는 0 으로 둔다.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from collections import defaultdict
from typing import Any, Callable, Optional
from urllib.parse import urlencode

import requests

from monitor.exchanges.hyperliquid import candle_range
from monitor.models import AccountSnapshot, Fill, Order, Position

BASE_URL = "https://api.bybit.com"
EXCHANGE = "bybit"
RECV_WINDOW = "10000"
DAY_MS = 86_400_000
WINDOW_MS = 7 * DAY_MS - 1          # 체결·청산손익 조회 최대 범위 (7일)
MAX_HISTORY_MS = 729 * DAY_MS       # 통합 계정 조회 가능 기간 (730일)
SETTLE_COINS = ("USDT", "USDC")
TRADE_EXEC_TYPES = {"Trade", "BustTrade", "AdlTrade", "BlockTrade"}   # 펀딩비 등 제외
PNL_WAIT_MS = 120_000               # 청산손익 기록이 늦게 생길 때 기다리는 최대 시간
OPEN_STATUSES = {"New", "PartiallyFilled", "Untriggered", "Active", "Created", ""}
SL_TYPES = {"StopLoss", "PartialStopLoss", "TrailingStop"}
TP_TYPES = {"TakeProfit", "PartialTakeProfit"}

# 자주 나오는 오류 코드 안내
ERROR_HINTS = {
    10003: "API 키가 올바르지 않음 (.env 의 BYBIT_API_KEY 확인)",
    10004: "서명 오류 (.env 의 BYBIT_API_SECRET 확인)",
    10005: "권한 부족 (키 발급 시 읽기 권한 확인)",
    10010: "허용되지 않은 IP (키에 IP 제한을 걸었다면 이 PC 의 IP 를 추가)",
    33004: "API 키 만료",
}


class BybitError(RuntimeError):
    def __init__(self, msg: str, code: Optional[int] = None):
        super().__init__(msg)
        self.code = code


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def symbol_to_coin(symbol: str) -> str:
    """BTCUSDT → BTC, BTCPERP(USDC 무기한) → BTC/USDC, 그 외(기한물 등)는 그대로."""
    s = symbol.upper()
    if s.endswith("USDT") and len(s) > 4:
        return s[:-4]
    if s.endswith("PERP") and len(s) > 4:
        return s[:-4] + "/USDC"
    return s


def coin_to_symbol(coin: str) -> str:
    """symbol_to_coin 의 반대: BTC → BTCUSDT, BTC/USDC → BTCPERP."""
    c = coin.upper()
    return c[:-5] + "PERP" if c.endswith("/USDC") else c + "USDT"


class BybitClient:
    backfill_days = 90   # 처음 실행 시 불러올 과거 기간

    def __init__(self, api_key: str, api_secret: str, session: Optional[requests.Session] = None,
                 timeout: float = 10.0, now_fn: Callable[[], float] = time.time):
        if not api_key or not api_secret:
            raise BybitError(".env 에 BYBIT_API_KEY / BYBIT_API_SECRET 이 없음")
        self.api_key = api_key
        self.api_secret = api_secret.encode()
        self.session = session or requests.Session()
        self.timeout = timeout
        self.now_fn = now_fn
        self.offset_ms = 0   # PC 시계와 Bybit 서버 시계 차이

    # ------------------------------------------------------------------ HTTP
    def _now_ms(self) -> int:
        return int(self.now_fn() * 1000)

    def _sync_time(self) -> None:
        try:
            r = self.session.get(BASE_URL + "/v5/market/time", timeout=self.timeout)
            server_ms = int(r.json()["time"])
            self.offset_ms = server_ms - self._now_ms()
        except Exception as e:  # noqa: BLE001
            raise BybitError(f"Bybit 서버 시간 조회 실패: {e}") from e

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        query = urlencode({k: v for k, v in params.items() if v is not None and v != ""})
        for attempt in range(2):
            ts = str(self._now_ms() + self.offset_ms)
            sign = hmac.new(self.api_secret, (ts + self.api_key + RECV_WINDOW + query).encode(),
                            hashlib.sha256).hexdigest()
            headers = {"X-BAPI-API-KEY": self.api_key, "X-BAPI-TIMESTAMP": ts,
                       "X-BAPI-RECV-WINDOW": RECV_WINDOW, "X-BAPI-SIGN": sign}
            try:
                resp = self.session.get(f"{BASE_URL}{path}?{query}", headers=headers,
                                        timeout=self.timeout)
            except requests.RequestException as e:
                raise BybitError(f"Bybit 접속 실패: {e}") from e
            if resp.status_code != 200:
                raise BybitError(f"Bybit 응답 오류 {resp.status_code}: {resp.text[:200]}")
            try:
                data = resp.json()
            except ValueError as e:
                raise BybitError("Bybit 응답이 JSON 이 아님") from e
            code = data.get("retCode")
            if code == 0:
                return data.get("result") or {}
            if code == 10002 and attempt == 0:   # PC 시계 어긋남 → 서버 시간 맞추고 재시도
                self._sync_time()
                continue
            hint = ERROR_HINTS.get(code, "")
            raise BybitError(f"Bybit 오류 {code}: {data.get('retMsg')}" + (f" — {hint}" if hint else ""),
                             code)
        raise BybitError("Bybit 시간 동기화 후에도 요청 실패 (PC 시계 확인)")

    def key_info(self) -> dict[str, Any]:
        """API 키 권한 (읽기 전용 여부). readOnly: 1 = 읽기 전용, 0 = 읽기·쓰기."""
        r = self._get("/v5/user/query-api", {})
        perms = r.get("permissions") or {}
        return {"read_only": str(r.get("readOnly")) == "1",
                "withdraw": bool(perms.get("Wallet")) and any("Withdraw" in str(x) for x in perms.get("Wallet") or []),
                "permissions": {k: v for k, v in perms.items() if v}}

    def price_range(self, coin: str, start_ms: int, end_ms: int) -> Optional[tuple[float, float]]:
        """구간 1분봉의 (최고가, 최저가). 공개 시세라 서명하지 않음."""
        params = {"category": "linear", "symbol": coin_to_symbol(coin), "interval": "1",
                  "start": int(start_ms), "end": int(end_ms), "limit": 1000}
        try:
            resp = self.session.get(f"{BASE_URL}/v5/market/kline?{urlencode(params)}", timeout=self.timeout)
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            raise BybitError(f"Bybit 시세 조회 실패: {e}") from e
        if data.get("retCode") != 0:
            raise BybitError(f"Bybit 시세 오류 {data.get('retCode')}: {data.get('retMsg')}")
        rows = (data.get("result") or {}).get("list") or []
        return candle_range([(r[2], r[3]) for r in rows if len(r) > 3])

    def _paged(self, path: str, params: dict[str, Any], max_pages: int = 50) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor = ""
        for _ in range(max_pages):
            result = self._get(path, {**params, "cursor": cursor})
            out.extend(result.get("list") or [])
            cursor = result.get("nextPageCursor") or ""
            if not cursor:
                break
        return out

    # ------------------------------------------------------------ 스냅샷
    def _equity(self) -> tuple[float, str]:
        try:
            r = self._get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
            acct = (r.get("list") or [{}])[0]
            return _f(acct.get("totalEquity")), ""
        except BybitError as e:
            if e.code in ERROR_HINTS:
                raise
            unified_err = e
        # 통합 계정이 아닌 경우(클래식 계정) 선물 지갑 기준
        try:
            r = self._get("/v5/account/wallet-balance", {"accountType": "CONTRACT"})
        except BybitError as e:
            if e.code == 10001:      # 통합 계정 → 클래식 조회 불가. 원래 오류(일시적)를 그대로 알림
                raise unified_err from None
            raise
        acct = (r.get("list") or [{}])[0]
        eq = _f(acct.get("totalEquity"))
        if eq == 0:
            eq = sum(_f(c.get("usdValue")) for c in acct.get("coin") or [])
        return eq, "클래식 계정: 선물 지갑 자본만 계산함"

    def _raw_positions(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for sc in SETTLE_COINS:
            out += self._paged("/v5/position/list",
                               {"category": "linear", "settleCoin": sc, "limit": 200})
        return [p for p in out if _f(p.get("size")) > 0 and p.get("side") in ("Buy", "Sell")]

    def snapshot(self) -> AccountSnapshot:
        equity, note = self._equity()
        raw_pos = self._raw_positions()
        raw_orders: list[dict[str, Any]] = []
        for sc in SETTLE_COINS:
            raw_orders += self._paged("/v5/order/realtime",
                                      {"category": "linear", "settleCoin": sc, "openOnly": 0, "limit": 50})
        positions = [parse_position(p) for p in raw_pos]
        orders = parse_orders(raw_orders)
        orders += position_stop_orders(raw_pos, orders)
        if any(int(_f(p.get("positionIdx"))) in (1, 2) for p in raw_pos):
            hedge = "헤지 모드 포지션 감지: 물타기·신규 진입 판단이 부정확할 수 있음 (단방향 모드 권장)"
            note = f"{note} / {hedge}" if note else hedge
        return AccountSnapshot(exchange=EXCHANGE, ts=self._now_ms(), equity=equity,
                               positions=positions, orders=orders, equity_note=note)

    # -------------------------------------------------------------- 체결
    def _windows(self, start_ms: int, end_ms: int) -> list[tuple[int, int]]:
        """[start, end] 를 7일 이하 구간으로 나눔."""
        s = max(start_ms, end_ms - MAX_HISTORY_MS)
        out = []
        while True:
            e = min(s + WINDOW_MS, end_ms)
            out.append((s, e))
            if e >= end_ms:
                return out
            s = e + 1

    def fills_since(self, start_ms: int, end_ms: Optional[int] = None) -> list[Fill]:
        # 포지션을 먼저 조회하고, 그 시각까지의 체결만 받아야 거꾸로 계산이 맞는다
        end = int(end_ms) if end_ms is not None else self._now_ms() + self.offset_ms
        current = defaultdict(float)
        for p in self._raw_positions():
            current[p["symbol"]] += _f(p["size"]) * (1 if p["side"] == "Buy" else -1)

        execs: list[dict[str, Any]] = []
        pnl_rows: list[dict[str, Any]] = []
        for s, e in self._windows(int(start_ms), end):
            execs += self._paged("/v5/execution/list",
                                 {"category": "linear", "startTime": s, "endTime": e, "limit": 100})
            # 청산손익 기록이 체결보다 늦게 생기면 build_fills 가 해당 체결을 다음 조회로 미룸
            pnl_rows += self._paged("/v5/position/closed-pnl",
                                    {"category": "linear", "startTime": s, "endTime": e, "limit": 100})
        return build_fills(execs, pnl_rows, dict(current), now_ms=end)


# ======================================================== 응답 파싱 (순수 함수)
def parse_position(p: dict[str, Any]) -> Position:
    size = _f(p.get("size"))
    mark = _f(p.get("markPrice"))
    liq = _f(p.get("liqPrice"))
    return Position(
        exchange=EXCHANGE,
        coin=symbol_to_coin(str(p.get("symbol", ""))),
        side="long" if p.get("side") == "Buy" else "short",
        size=size,
        entry_px=_f(p.get("avgPrice")),
        mark_px=mark,
        notional=size * mark,
        upnl=_f(p.get("unrealisedPnl")),
        liq_px=liq if liq > 0 else None,
        margin_mode="isolated" if int(_f(p.get("tradeMode"))) == 1 else "cross",
        leverage_setting=_f(p.get("leverage")),
        margin_used=_f(p.get("positionIM")),
    )


def _order_kind(o: dict[str, Any]) -> str:
    sot = str(o.get("stopOrderType") or "")
    if sot in SL_TYPES:
        return "stop"
    if sot in TP_TYPES:
        return "take_profit"
    if sot == "Stop":   # 일반 조건부 주문: 포지션을 줄이는 주문이면 손절 후보 (방향은 Order.protects 가 판단)
        return "stop" if (o.get("reduceOnly") or o.get("closeOnTrigger")) else "other"
    if sot in ("", "UNKNOWN") and o.get("orderType") == "Limit":
        return "limit"
    return "other"


def parse_orders(raw: list[dict[str, Any]]) -> list[Order]:
    out: list[Order] = []
    for o in raw or []:
        if str(o.get("orderStatus", "")) not in OPEN_STATUSES:
            continue
        trig = _f(o.get("triggerPrice"))
        out.append(Order(
            exchange=EXCHANGE,
            coin=symbol_to_coin(str(o.get("symbol", ""))),
            oid=str(o.get("orderId", "")),
            side="buy" if o.get("side") == "Buy" else "sell",
            kind=_order_kind(o),
            price=_f(o.get("price")),
            trigger_px=trig if trig > 0 else None,
            size=_f(o.get("qty")),
            reduce_only=bool(o.get("reduceOnly")),
            is_position_tpsl=str(o.get("stopOrderType")) in SL_TYPES | TP_TYPES,
            ts=int(_f(o.get("createdTime"))),
        ))
    return out


def position_stop_orders(raw_pos: list[dict[str, Any]], orders: list[Order]) -> list[Order]:
    """포지션에 직접 걸린 손절가(stopLoss)를 손절 주문으로 취급. 같은 가격 주문이 이미 있으면 생략."""
    out: list[Order] = []
    for p in raw_pos:
        sl = _f(p.get("stopLoss"))
        if sl <= 0:
            continue
        coin = symbol_to_coin(str(p.get("symbol", "")))
        closing = "sell" if p.get("side") == "Buy" else "buy"
        if any(o.coin == coin and o.kind == "stop" and o.trigger_px and abs(o.trigger_px - sl) < 1e-9
               for o in orders):
            continue
        out.append(Order(exchange=EXCHANGE, coin=coin, oid=f"pos-sl:{p.get('symbol')}", side=closing,
                         kind="stop", price=0.0, trigger_px=sl, size=0.0, reduce_only=True,
                         is_position_tpsl=True, ts=int(_f(p.get("updatedTime")))))
    return out


def _sign(x: float) -> int:
    return (x > 1e-12) - (x < -1e-12)


def _classify(before: float, after: float) -> tuple[str, Optional[str]]:
    sb, sa = _sign(before), _sign(after)
    if sb == 0:
        return "open", ("long" if sa > 0 else "short") if sa else None
    if sa == 0:
        return "close", "long" if sb > 0 else "short"
    if sb != sa:
        return "flip", "long" if sa > 0 else "short"
    if abs(after) > abs(before):
        return "open", "long" if sa > 0 else "short"
    return "close", "long" if sb > 0 else "short"


def build_fills(execs: list[dict[str, Any]], pnl_rows: list[dict[str, Any]],
                current: dict[str, float], now_ms: int) -> list[Fill]:
    """체결 목록 → Fill. 현재 포지션(current, 부호 포함)에서 거꾸로 되짚어 직전 수량을 계산."""
    seen: set[str] = set()
    trades = []
    for e in execs:
        eid = str(e.get("execId", ""))
        if e.get("execType") not in TRADE_EXEC_TYPES or eid in seen:
            continue
        seen.add(eid)
        trades.append(e)
    trades.sort(key=lambda e: (int(_f(e.get("execTime"))), int(_f(e.get("seq"))), str(e.get("execId"))))

    # 1) 직전·직후 포지션 수량 (최신 체결부터 거꾸로)
    running = defaultdict(float, current)
    before_after: dict[str, tuple[float, float]] = {}
    for e in reversed(trades):
        sym = e["symbol"]
        q = _f(e.get("execQty")) * (1 if e.get("side") == "Buy" else -1)
        after = round(running[sym], 10)      # 부동소수 오차 제거 (거래소 수량은 소수 8자리 이내)
        before = round(after - q, 10)
        if abs(before) < 1e-12:
            before = 0.0
        before_after[e["execId"]] = (before, after)
        running[sym] = before

    # 2) 주문 단위 청산손익 → 청산 1단위당 손익. 분할 체결이 여러 조회에 걸쳐도 정확하게 배분됨
    pnl_sum: dict[str, float] = defaultdict(float)
    qty_sum: dict[str, float] = defaultdict(float)
    seen_rows: set[tuple] = set()
    for r in pnl_rows:
        key = (str(r.get("orderId")), str(r.get("createdTime")), str(r.get("closedPnl")))
        if key in seen_rows:
            continue
        seen_rows.add(key)
        oid = str(r.get("orderId"))
        pnl_sum[oid] += _f(r.get("closedPnl"))
        qty_sum[oid] += _f(r.get("closedSize")) or _f(r.get("qty"))
    pnl_per_unit = {oid: pnl_sum[oid] / qty_sum[oid] for oid in pnl_sum if qty_sum[oid] > 0}

    out: list[Fill] = []
    for e in trades:
        oid = str(e.get("orderId", ""))
        closed = _f(e.get("closedSize"))
        ts = int(_f(e.get("execTime")))
        if closed > 0 and oid not in pnl_per_unit and now_ms - ts < PNL_WAIT_MS:
            break   # 청산손익 기록이 아직 없음 → 이 체결부터는 다음 조회 때 처리
        before, after = before_after[e["execId"]]
        action, pos_side = _classify(before, after)
        pnl = pnl_per_unit.get(oid, 0.0) * closed if closed > 0 else 0.0
        out.append(Fill(
            exchange=EXCHANGE,
            coin=symbol_to_coin(str(e.get("symbol", ""))),
            tid=str(e.get("execId", "")),
            oid=oid,
            ts=ts,
            side="buy" if e.get("side") == "Buy" else "sell",
            px=_f(e.get("execPrice")),
            sz=_f(e.get("execQty")),
            action=action,
            position_side=pos_side,
            start_position=before,
            closed_pnl=pnl,
            fee=0.0,
            is_liquidation=e.get("execType") == "BustTrade",
        ))
    return out
