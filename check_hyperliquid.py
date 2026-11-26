"""1단계 점검용: Hyperliquid 계좌를 한 번 조회해서 화면에 출력한다.

사용법:
  python check_hyperliquid.py            # 현재 포지션·주문·최근 24시간 체결
  python check_hyperliquid.py --hours 72 # 체결 조회 기간 변경
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

from monitor.config import load_config, max_leverage_for
from monitor.exchanges.hyperliquid import HyperliquidClient, HyperliquidError

if hasattr(sys.stdout, "reconfigure"):  # Windows 콘솔 한글 깨짐 방지
    sys.stdout.reconfigure(encoding="utf-8")


def fmt_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%m-%d %H:%M:%S")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24, help="체결 조회 기간 (시간)")
    args = ap.parse_args()

    cfg = load_config()
    hl_cfg = cfg.get("exchanges", {}).get("hyperliquid", {})
    try:
        client = HyperliquidClient(cfg["secrets"]["hl_wallet"], dexes=hl_cfg.get("dexes"))
        snap = client.snapshot()
        start = int((time.time() - args.hours * 3600) * 1000)
        fills = client.fills_since(start)
    except HyperliquidError as e:
        print(f"[오류] {e}")
        return 1

    acc_lev = snap.effective_leverage()
    acc_lev_txt = f"{acc_lev:.2f}x" if acc_lev is not None else "계산 불가(자본 0)"
    print(f"\n=== Hyperliquid 계좌 ({fmt_ts(snap.ts)} UTC) ===")
    print(f"자본: ${snap.equity:,.2f} | 총 노출: ${snap.total_notional:,.2f} | "
          f"계좌 실효 레버리지: {acc_lev_txt}")
    if snap.equity_note:
        print(f"ℹ {snap.equity_note}")

    print(f"\n--- 포지션 {len(snap.positions)}개 ---")
    for p in snap.positions:
        lev = snap.effective_leverage(p)
        limit = max_leverage_for(cfg, p.coin)
        stops = snap.stop_orders_for(p)
        liq_d = p.liq_distance_pct()

        flags = []
        if lev is not None and lev > limit:
            flags.append(f"레버리지 초과(한도 {limit:g}x)")
        if not stops:
            flags.append("SL 없음")

        lev_txt = f"{lev:.2f}x" if lev is not None else "-"
        liq_txt = f"{p.liq_px:g} (거리 {liq_d:.1f}%)" if liq_d is not None else "-"
        sl_txt = ", ".join(f"{o.trigger_px:g}" for o in stops) if stops else "-"
        flag_txt = "  ⚠ " + " / ".join(flags) if flags else ""

        print(f"{p.coin:>10} {p.side.upper():5} 수량 {p.size:g} | 진입 {p.entry_px:g} → "
              f"현재 {p.mark_px:.6g} | 미실현 ${p.upnl:,.2f} | 실효 {lev_txt}")
        print(f"{'':>10} 청산가 {liq_txt} | SL {sl_txt}{flag_txt}")

    print(f"\n--- 미체결 주문 {len(snap.orders)}개 ---")
    for o in snap.orders:
        trig = f" 트리거 {o.trigger_px:g}" if o.trigger_px else ""
        size = "포지션 전체" if o.size == 0 else f"{o.size:g}"
        print(f"{o.coin:>10} {o.side:4} {o.kind:11} 가격 {o.price:g}{trig} 수량 {size}"
              f"{' reduceOnly' if o.reduce_only else ''}")

    print(f"\n--- 최근 {args.hours:g}시간 체결 {len(fills)}건 ---")
    for f in fills[-30:]:
        pnl = f" 실현 ${f.closed_pnl:,.2f}" if f.action in ("close", "flip") else ""
        print(f"{fmt_ts(f.ts)} {f.coin:>10} {f.action:5} {f.position_side or '-':5} "
              f"{f.side:4} {f.sz:g} @ {f.px:g}{pnl}")
    if len(fills) > 30:
        print(f"... 앞의 {len(fills) - 30}건 생략")
    return 0


if __name__ == "__main__":
    sys.exit(main())
