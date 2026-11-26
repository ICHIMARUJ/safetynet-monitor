"""거래소별 응답을 하나의 공통 형태로 맞추는 데이터 모델.

규칙 엔진은 이 모델만 보고 판단하므로, 거래소가 늘어나도 규칙 코드는 그대로 둔다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Position:
    exchange: str
    coin: str
    side: str                    # "long" | "short"
    size: float                  # 수량 (항상 양수)
    entry_px: float
    mark_px: float
    notional: float              # 명목가치 (USD, 항상 양수)
    upnl: float                  # 미실현 손익 (USD)
    liq_px: Optional[float]      # 청산가 (없으면 None)
    margin_mode: str             # "cross" | "isolated"
    leverage_setting: float      # 거래소에 설정한 배율 (실효 레버리지와 다름)
    margin_used: float

    @property
    def key(self) -> str:
        """포지션 식별자. 같은 거래소·코인·방향이면 같은 포지션으로 본다."""
        return f"{self.exchange}:{self.coin}:{self.side}"

    @property
    def is_losing(self) -> bool:
        return self.upnl < 0

    def liq_distance_pct(self) -> Optional[float]:
        """현재가에서 청산가까지 남은 거리(%). 청산가가 없으면 None."""
        if self.liq_px is None or self.liq_px <= 0 or self.mark_px <= 0:
            return None
        return abs(self.mark_px - self.liq_px) / self.mark_px * 100


@dataclass
class Order:
    exchange: str
    coin: str
    oid: str
    side: str                    # "buy" | "sell"
    kind: str                    # "limit" | "stop" | "take_profit" | "other"
    price: float                 # 지정가 (시장가 트리거면 0)
    trigger_px: Optional[float]  # 트리거 가격 (트리거 주문이 아니면 None)
    size: float                  # 0 이면 "포지션 전체" (포지션 연동 TP/SL)
    reduce_only: bool
    is_position_tpsl: bool
    ts: int                      # 주문 생성 시각 (ms)

    def protects(self, pos: Position) -> bool:
        """이 주문이 해당 포지션의 손절(SL) 역할을 하는지."""
        if self.exchange != pos.exchange or self.coin != pos.coin:
            return False
        if self.kind != "stop" or self.trigger_px is None:
            return False
        closing_side = "sell" if pos.side == "long" else "buy"
        if self.side != closing_side:
            return False
        # 롱의 손절은 현재가 아래, 숏의 손절은 현재가 위에 있어야 의미가 있음
        if pos.side == "long":
            return self.trigger_px < pos.mark_px
        return self.trigger_px > pos.mark_px


@dataclass
class Fill:
    exchange: str
    coin: str
    tid: str                     # 체결 고유 ID (중복 저장 방지용)
    oid: str
    ts: int                      # 체결 시각 (ms)
    side: str                    # "buy" | "sell"
    px: float
    sz: float
    action: str                  # "open" | "close" | "flip" | "other"
    position_side: Optional[str] # 이 체결이 관련된 포지션 방향 ("long"/"short")
    start_position: float        # 체결 직전 포지션 수량 (부호 포함: 롱 +, 숏 -)
    closed_pnl: float            # 이 체결로 실현된 손익 (수수료 제외)
    fee: float
    is_liquidation: bool = False # 강제 청산 체결 여부

    @property
    def notional(self) -> float:
        return self.px * self.sz


@dataclass
class AccountSnapshot:
    exchange: str
    ts: int                      # 조회 시각 (ms)
    equity: float                # 계좌 자본 (USD)
    positions: list[Position] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)
    equity_note: str = ""        # 자본 계산에 한계가 있을 때의 설명 (예: 포트폴리오 마진 모드)

    @property
    def total_notional(self) -> float:
        return sum(p.notional for p in self.positions)

    def effective_leverage(self, pos: Optional[Position] = None) -> Optional[float]:
        """실효 레버리지. pos 를 주면 해당 포지션 기준, 없으면 계좌 전체 기준."""
        if self.equity <= 0:
            return None
        notional = pos.notional if pos is not None else self.total_notional
        return notional / self.equity

    def stop_orders_for(self, pos: Position) -> list[Order]:
        return [o for o in self.orders if o.protects(pos)]
