"""연결 점검: 켜진 거래소를 한 번 조회해서 포지션·주문·최근 체결을 보여준다.

사용법:
  python check.py                       # 켜진 거래소 모두
  python check.py bybit                 # 특정 거래소만
  python check.py --hours 72            # 체결 조회 기간 변경
  python check.py --log check.txt       # 결과를 UTF-8 파일에도 저장
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

from monitor.config import load_config, max_leverage_for
from run import _Tee, build_clients

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def fmt_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%m-%d %H:%M:%S")


def show(cfg, name, client, hours: float) -> None:
    snap = client.snapshot()
    fills = client.fills_since(int((time.time() - hours * 3600) * 1000))
    acc_lev = snap.effective_leverage()
    print(f"\n=== {name} ({fmt_ts(snap.ts)} UTC) ===")
    print(f"자본: ${snap.equity:,.2f} | 총 노출: ${snap.total_notional:,.2f} | 계좌 실효 레버리지: "
          + (f"{acc_lev:.2f}x" if acc_lev is not None else "계산 불가(자본 0)"))
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
        print(f"{p.coin:>10} {p.side.upper():5} 수량 {p.size:g} | 진입 {p.entry_px:g} → 현재 {p.mark_px:.6g}"
              f" | 미실현 ${p.upnl:,.2f} | 실효 " + (f"{lev:.2f}x" if lev is not None else "-"))
        print(f"{'':>10} 청산가 " + (f"{p.liq_px:g} (거리 {liq_d:.1f}%)" if liq_d is not None else "-")
              + " | SL " + (", ".join(f"{o.trigger_px:g}" for o in stops) if stops else "-")
              + ("  ⚠ " + " / ".join(flags) if flags else ""))

    print(f"\n--- 미체결 주문 {len(snap.orders)}개 ---")
    for o in snap.orders:
        trig = f" 트리거 {o.trigger_px:g}" if o.trigger_px else ""
        size = "포지션 전체" if o.size == 0 else f"{o.size:g}"
        print(f"{o.coin:>10} {o.side:4} {o.kind:11} 가격 {o.price:g}{trig} 수량 {size}"
              f"{' reduceOnly' if o.reduce_only else ''}")

    print(f"\n--- 최근 {hours:g}시간 체결 {len(fills)}건 ---")
    for f in fills[-30:]:
        pnl = f" 실현 ${f.closed_pnl:,.2f}" if f.action in ("close", "flip") else ""
        liq = " [강제청산]" if f.is_liquidation else ""
        print(f"{fmt_ts(f.ts)} {f.coin:>10} {f.action:5} {f.position_side or '-':5} "
              f"{f.side:4} {f.sz:g} @ {f.px:g} (직전 수량 {f.start_position:g}){pnl}{liq}")
    if len(fills) > 30:
        print(f"... 앞의 {len(fills) - 30}건 생략")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("exchange", nargs="?", help="hyperliquid 또는 bybit (생략 시 전부)")
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--log")
    args = ap.parse_args()
    if args.log:
        sys.stdout = _Tee(sys.stdout, args.log)

    cfg = load_config()
    clients = build_clients(cfg)
    if args.exchange:
        clients = {k: v for k, v in clients.items() if k == args.exchange}
    if not clients:
        print("[설정 오류] 조회할 거래소가 없음 (config.yaml 의 enabled 와 .env 확인)")
        return 1
    ok = True
    for name, client in clients.items():
        try:
            show(cfg, name, client, args.hours)
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"\n[{name} 오류] {e}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
