"""본전(평단) 복귀 알림. 감점 없는 안내용.

포지션이 평단 대비 min_loss_pct % 이상 불리하게 움직였다가(=손실 구간 진입) 다시 평단으로 돌아오면 한 번 알린다.
  롱: 현재가 ≥ 평단 × (1 − tolerance)   숏: 현재가 ≤ 평단 × (1 + tolerance)
알린 뒤에는 다시 min_loss_pct 이상 손실 구간에 들어가야 다음 알림이 가능하다 (본전 근처에서 반복 알림 방지).
평단은 매번 현재 값을 쓴다 (물타기로 평단이 바뀌면 바뀐 평단 기준).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from monitor.models import Position


@dataclass
class BreakevenEvent:
    exchange: str
    coin: str
    side: str
    entry: float
    mark: float
    worst_pct: float        # 손실 구간에서 가장 불리했던 가격 거리 (%)
    worst_upnl: float       # 그때의 미실현 손익
    size: float


class BreakevenWatch:
    def __init__(self, cfg: dict[str, Any], state: dict[str, Any] | None = None):
        be = cfg["rules"]["breakeven"]
        self.enabled = bool(be.get("enabled", True))
        self.min_loss = float(be["min_loss_pct"])
        self.tol = float(be["tolerance_pct"]) / 100
        self.state: dict[str, dict] = state if state is not None else {}

    @staticmethod
    def adverse_pct(p: Position) -> float:
        """평단 대비 불리한 방향 가격 거리 (%). 이익이면 음수."""
        if p.entry_px <= 0:
            return 0.0
        d = (p.entry_px - p.mark_px) / p.entry_px * 100
        return d if p.side == "long" else -d

    def update(self, positions: list[Position], live_exchanges: set | None = None) -> list[BreakevenEvent]:
        """live_exchanges: 이번에 조회에 성공한 거래소. 조회 실패한 거래소의 포지션 상태는 지우지 않는다."""
        if not self.enabled:
            return []
        out: list[BreakevenEvent] = []
        seen = set()
        for p in positions:
            k = p.key
            seen.add(k)
            st = self.state.setdefault(k, {"armed": False, "worst": 0.0, "worst_upnl": 0.0})
            adv = self.adverse_pct(p)
            if adv >= self.min_loss:
                st["armed"] = True
                if adv > st["worst"]:
                    st["worst"], st["worst_upnl"] = adv, p.upnl
            back = (p.mark_px >= p.entry_px * (1 - self.tol)) if p.side == "long" \
                else (p.mark_px <= p.entry_px * (1 + self.tol))
            if st["armed"] and back:
                out.append(BreakevenEvent(p.exchange, p.coin, p.side, p.entry_px, p.mark_px,
                                          round(st["worst"], 2), st["worst_upnl"], p.size))
                st.update(armed=False, worst=0.0, worst_upnl=0.0)
        for k in [k for k in self.state if k not in seen
                  and (live_exchanges is None or k.split(":", 1)[0] in live_exchanges)]:   # 정리된 포지션은 상태 삭제
            del self.state[k]
        return out
