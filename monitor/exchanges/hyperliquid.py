"""Hyperliquid 수집기 (읽기 전용).

지갑 주소만으로 공개 info API(POST https://api.hyperliquid.xyz/info)를 조회한다.
개인키·API 키를 쓰지 않으므로 주문을 넣거나 취소할 수 없다.

사용 요청:
  - clearinghouseState : 계좌 자본, 포지션
  - frontendOpenOrders : 미체결 주문 + TP/SL 트리거 정보
  - userFillsByTime    : 체결 내역 (응답당 최대 2000건, 최근 10000건까지만 조회 가능)
  - candleSnapshot     : 1분봉 (FOMO 판단용 직전 가격 범위)
"""
from __future__ import annotations

import re
import time
from typing import Any, Optional

import requests

from monitor.models import AccountSnapshot, Fill, Order, Position

API_URL = "https://api.hyperliquid.xyz/info"
EXCHANGE = "hyperliquid"
FILLS_PAGE_LIMIT = 2000
DEX_SCAN_SEC = 600
_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# 통합 계정(unifiedAccount)·포트폴리오 마진(portfolioMargin) 모드에서는 담보가 현물 잔고에 있고,
# perp 쪽 accountValue 는 "지금 perp 에 배정된 담보"만 보여준다. 실제 자본은 현물 스테이블 잔고
# (perp 미실현 손익 포함)로 계산해야 한다. 실계정 비교로 확인함 (perp $154 / USDC $937 사례).
UNIFIED_MODES = ("unifiedAccount", "portfolioMargin")
STABLES = ("USDC", "USDH", "USDT0", "USDE")


class HyperliquidError(RuntimeError):
    pass


def _f(value: Any, default: float = 0.0) -> float:
    """문자열 숫자 → float. 비어 있거나 이상하면 default."""
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


class HyperliquidClient:
    def __init__(self, wallet: str, dexes: Optional[list[str]] = None,
                 session: Optional[requests.Session] = None, timeout: float = 10.0):
        if not _ADDR_RE.match(wallet or ""):
            raise HyperliquidError(
                "HL_WALLET 이 올바른 지갑 주소(0x + 40자리)가 아님. .env 를 확인할 것.")
        self.wallet = wallet.lower()
        self.dexes = list(dexes) if dexes else [""]
        self._dex_scan_at = 0.0       # 마지막 마켓 자동 감지 시각
        self.session = session or requests.Session()
        self.timeout = timeout

    # ------------------------------------------------------------------ HTTP
    def _post(self, payload: dict[str, Any]) -> Any:
        try:
            resp = self.session.post(API_URL, json=payload, timeout=self.timeout)
        except requests.RequestException as e:
            raise HyperliquidError(f"Hyperliquid 접속 실패: {e}") from e
        if resp.status_code != 200:
            raise HyperliquidError(
                f"Hyperliquid 응답 오류 {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as e:
            raise HyperliquidError("Hyperliquid 응답이 JSON 이 아님") from e

    # ------------------------------------------------------------ 시세
    def price_range(self, coin: str, start_ms: int, end_ms: int) -> Optional[tuple[float, float]]:
        """구간 1분봉의 (최고가, 최저가). 데이터가 없으면 None."""
        raw = self._post({"type": "candleSnapshot",
                          "req": {"coin": coin, "interval": "1m", "startTime": int(start_ms),
                                  "endTime": int(end_ms)}})
        return candle_range([(c.get("h"), c.get("l")) for c in raw or []])

    # ------------------------------------------------------------ 스냅샷
    def add_dex(self, dex: str) -> None:
        if dex not in self.dexes:
            self.dexes.append(dex)

    def discover_dexes(self) -> None:
        """HIP-3 마켓(xyz 등) 자동 감지: 10분마다 전체 마켓 목록을 보고
        포지션·담보·미체결 주문이 있는 마켓을 조회 대상에 추가한다 (설정에 적지 않아도 됨)."""
        if time.time() - getattr(self, "_dex_scan_at", 0.0) < DEX_SCAN_SEC:
            return
        self._dex_scan_at = time.time()
        try:
            raw = self._post({"type": "perpDexs"})
        except HyperliquidError:
            return
        for d in raw if isinstance(raw, list) else []:
            name = (d or {}).get("name") if isinstance(d, dict) else None
            if not name or name in self.dexes:
                continue
            try:
                st = self._post({"type": "clearinghouseState", "user": self.wallet, "dex": name})
                od = self._post({"type": "frontendOpenOrders", "user": self.wallet, "dex": name})
            except HyperliquidError:
                continue
            if st.get("assetPositions") or _f((st.get("marginSummary") or {}).get("accountValue")) > 0 or od:
                self.add_dex(name)

    def snapshot(self) -> AccountSnapshot:
        """계좌 자본 + 포지션 + 미체결 주문을 한 번에 조회."""
        self.discover_dexes()
        perp_value = 0.0
        positions: list[Position] = []
        orders: list[Order] = []
        for dex in self.dexes:
            state = self._post({"type": "clearinghouseState", "user": self.wallet, "dex": dex})
            perp_value += _f((state.get("marginSummary") or {}).get("accountValue"))
            positions.extend(parse_positions(state))
            raw_orders = self._post({"type": "frontendOpenOrders", "user": self.wallet, "dex": dex})
            orders.extend(parse_orders(raw_orders))

        mode = self._post({"type": "userAbstraction", "user": self.wallet})
        equity, note = perp_value, ""
        if mode in UNIFIED_MODES:
            spot = self._post({"type": "spotClearinghouseState", "user": self.wallet})
            equity, note = equity_from_spot(spot, mode)
        return AccountSnapshot(exchange=EXCHANGE, ts=int(time.time() * 1000), equity=equity,
                               positions=positions, orders=orders, equity_note=note)

    # -------------------------------------------------------------- 체결
    def fills_since(self, start_ms: int, end_ms: Optional[int] = None) -> list[Fill]:
        """start_ms 이후 체결 전체. 2000건 단위로 넘겨가며 모두 가져온다."""
        out: dict[str, Fill] = {}
        cursor = int(start_ms)
        for _ in range(50):  # 안전장치: 최대 100,000건
            payload: dict[str, Any] = {"type": "userFillsByTime", "user": self.wallet,
                                       "startTime": cursor}
            if end_ms is not None:
                payload["endTime"] = int(end_ms)
            raw = self._post(payload)
            if not isinstance(raw, list) or not raw:
                break
            page = parse_fills(raw, self.wallet)
            for fl in page:
                out[fl.tid] = fl
                if ":" in fl.coin:              # HIP-3 체결이 보이면 그 마켓도 바로 조회 대상에 추가
                    self.add_dex(fl.coin.split(":", 1)[0])
            if len(raw) < FILLS_PAGE_LIMIT:
                break
            last_ts = max(fl.ts for fl in page)
            # 같은 ms 에 체결이 몰려 있어도 빠지지 않게 last_ts 부터 다시 조회 (tid 로 중복 제거)
            cursor = last_ts if last_ts > cursor else last_ts + 1
        return sorted(out.values(), key=lambda x: (x.ts, x.tid))


# ======================================================== 응답 파싱 (순수 함수)
def candle_range(pairs: list[tuple[Any, Any]]) -> Optional[tuple[float, float]]:
    """[(고가, 저가)] → (최고가, 최저가). 거래소 공통."""
    hs = [_f(h) for h, _ in pairs if _f(h) > 0]
    ls = [_f(l) for _, l in pairs if _f(l) > 0]
    return (max(hs), min(ls)) if hs and ls else None


def equity_from_spot(spot: dict[str, Any], mode: str) -> tuple[float, str]:
    """통합 계정 자본 = 현물 스테이블 코인 잔고 합 (1달러로 계산)."""
    balances = spot.get("balances") or []
    equity = sum(_f(b.get("total")) for b in balances if b.get("coin") in STABLES)
    others = [b.get("coin") for b in balances
              if b.get("coin") not in STABLES and _f(b.get("total")) != 0]
    note = ""
    if mode == "portfolioMargin" and others:
        note = (f"포트폴리오 마진 모드: 스테이블이 아닌 담보({', '.join(map(str, others[:5]))})는 "
                "자본에 포함하지 않음 → 레버리지가 실제보다 높게 계산될 수 있음")
    return equity, note


def parse_positions(state: dict[str, Any]) -> list[Position]:
    out: list[Position] = []
    for item in state.get("assetPositions") or []:
        p = item.get("position") or {}
        szi = _f(p.get("szi"))
        if szi == 0:
            continue
        size = abs(szi)
        notional = abs(_f(p.get("positionValue")))
        mark = notional / size if size else 0.0
        lev = p.get("leverage") or {}
        liq = _f(p.get("liquidationPx"), default=-1.0)
        out.append(Position(
            exchange=EXCHANGE,
            coin=str(p.get("coin", "")),
            side="long" if szi > 0 else "short",
            size=size,
            entry_px=_f(p.get("entryPx")),
            mark_px=mark,
            notional=notional,
            upnl=_f(p.get("unrealizedPnl")),
            liq_px=liq if liq > 0 else None,
            margin_mode=str(lev.get("type", "cross")),
            leverage_setting=_f(lev.get("value")),
            margin_used=_f(p.get("marginUsed")),
        ))
    return out


def _order_kind(order_type: str, is_trigger: bool) -> str:
    t = (order_type or "").lower()
    if t.startswith("stop"):
        return "stop"
    if t.startswith("take profit"):
        return "take_profit"
    if not is_trigger and t.startswith("limit"):
        return "limit"
    return "other"


def parse_orders(raw: list[dict[str, Any]]) -> list[Order]:
    out: list[Order] = []
    for o in raw or []:
        is_trigger = bool(o.get("isTrigger"))
        trig = _f(o.get("triggerPx"))
        out.append(Order(
            exchange=EXCHANGE,
            coin=str(o.get("coin", "")),
            oid=str(o.get("oid", "")),
            side="buy" if o.get("side") == "B" else "sell",
            kind=_order_kind(str(o.get("orderType", "")), is_trigger),
            price=_f(o.get("limitPx")),
            trigger_px=trig if is_trigger and trig > 0 else None,
            size=_f(o.get("sz")),
            reduce_only=bool(o.get("reduceOnly")),
            is_position_tpsl=bool(o.get("isPositionTpsl")),
            ts=int(_f(o.get("timestamp"))),
        ))
    return out


def _fill_action(direction: str) -> tuple[str, Optional[str]]:
    """Hyperliquid 의 dir 값 → (action, position_side)."""
    d = (direction or "").strip()
    if ">" in d:  # "Long > Short", "Short > Long" : 반대 방향으로 뒤집힘
        new_side = d.split(">")[-1].strip().lower()
        return "flip", new_side if new_side in ("long", "short") else None
    low = d.lower()
    if low.startswith("liquidated"):  # "Liquidated Isolated Long", "Liquidated Cross Short"
        side = low.split()[-1]
        return "close", side if side in ("long", "short") else None
    for prefix, action in (("open", "open"), ("close", "close")):
        if low.startswith(prefix):
            side = low[len(prefix):].strip()
            return action, side if side in ("long", "short") else None
    return "other", None  # 청산(Liquidation), 현물 매매 등


def _is_own_liquidation(f: dict[str, Any], wallet: Optional[str]) -> bool:
    liq = f.get("liquidation")
    if not liq:
        return False
    who = str((liq or {}).get("liquidatedUser", "")).lower() if isinstance(liq, dict) else ""
    return wallet is None or not who or who == wallet.lower()


def parse_fills(raw: list[dict[str, Any]], wallet: Optional[str] = None) -> list[Fill]:
    out: list[Fill] = []
    for f in raw or []:
        action, pos_side = _fill_action(str(f.get("dir", "")))
        out.append(Fill(
            exchange=EXCHANGE,
            coin=str(f.get("coin", "")),
            tid=str(f.get("tid", "")),
            oid=str(f.get("oid", "")),
            ts=int(_f(f.get("time"))),
            side="buy" if f.get("side") == "B" else "sell",
            px=_f(f.get("px")),
            sz=_f(f.get("sz")),
            action=action,
            position_side=pos_side,
            start_position=_f(f.get("startPosition")),
            closed_pnl=_f(f.get("closedPnl")),
            fee=_f(f.get("fee")),
            is_liquidation=_is_own_liquidation(f, wallet),
        ))
    return out
