"""매매 계획 입력 (4단계에서 앱 화면으로 옮길 예정, 지금은 명령어로 입력).

사용 예:
  python plan.py add hyperliquid BTC long --entries 95000,93000 --stop 91000 --targets 100000,105000 --note "4h 지지 반등"
  python plan.py list
  python plan.py close 3

entries 에 적은 가격 근처(기본 ±0.5%)에서의 추가 진입은 물타기로 보지 않는다.
stop 은 SL 주문이 없어도 "계획 손절가"로 감시한다 (이탈 후 계속 보유하면 위반).
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from monitor.config import data_dir, load_config
from monitor.store import Plan, Store

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

EXCHANGES = ("hyperliquid", "bybit")


def _prices(text: str) -> list[float]:
    if not text:
        return []
    try:
        vals = [float(x) for x in text.replace(" ", "").split(",") if x]
    except ValueError:
        raise argparse.ArgumentTypeError(f"가격 목록 형식 오류: {text} (예: 95000,93000)")
    if any(v <= 0 for v in vals):
        raise argparse.ArgumentTypeError("가격은 0보다 커야 함")
    return vals


def validate(p: Plan) -> list[str]:
    """방향과 가격 위치가 말이 되는지 점검. 문제 목록 반환 (빈 목록이면 정상)."""
    errs = []
    if not p.entries:
        errs.append("진입가(--entries)를 1개 이상 입력해야 함")
    if p.stop_px is None:
        errs.append("손절가(--stop)를 입력해야 함")
    if errs:
        return errs
    if p.side == "long":
        if p.stop_px >= min(p.entries):
            errs.append("롱인데 손절가가 가장 낮은 진입가보다 높거나 같음")
        if p.targets and min(p.targets) <= max(p.entries):
            errs.append("롱인데 목표가가 진입가보다 낮거나 같음")
    else:
        if p.stop_px <= max(p.entries):
            errs.append("숏인데 손절가가 가장 높은 진입가보다 낮거나 같음")
        if p.targets and max(p.targets) >= min(p.entries):
            errs.append("숏인데 목표가가 진입가보다 높거나 같음")
    return errs


def main() -> int:
    ap = argparse.ArgumentParser(description="매매 계획 관리")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="계획 추가")
    a.add_argument("exchange", choices=EXCHANGES)
    a.add_argument("coin")
    a.add_argument("side", choices=("long", "short"))
    a.add_argument("--entries", type=_prices, required=True, help="진입가 목록 (쉼표 구분)")
    a.add_argument("--stop", type=float, required=True, help="손절가")
    a.add_argument("--targets", type=_prices, default=[], help="목표가 목록 (쉼표 구분)")
    a.add_argument("--note", default="", help="진입 근거 메모")

    sub.add_parser("list", help="활성 계획 보기")
    c = sub.add_parser("close", help="계획 종료")
    c.add_argument("plan_id", type=int)

    args = ap.parse_args()
    cfg = load_config()
    store = Store(data_dir(cfg) / "safetynet.db")
    try:
        if args.cmd == "add":
            p = Plan(exchange=args.exchange, coin=args.coin.upper(), side=args.side,
                     entries=args.entries, stop_px=args.stop, targets=args.targets, note=args.note)
            errs = validate(p)
            if errs:
                print("[계획 거부] " + " / ".join(errs))
                return 1
            old = store.active_plan_for(p.exchange, p.coin, p.side)
            pid = store.add_plan(p)
            if old:
                store.close_plan(old.id, status="replaced")
                print(f"기존 계획 #{old.id} 는 새 계획으로 교체됨")
            print(f"계획 #{pid} 저장: {p.exchange} {p.coin} {p.side.upper()} 진입 {p.entries} "
                  f"손절 {p.stop_px:g} 목표 {p.targets or '-'}")
        elif args.cmd == "list":
            plans = store.plans("active")
            if not plans:
                print("활성 계획 없음")
            for p in plans:
                t = datetime.fromtimestamp(p.created_ts / 1000, tz=timezone.utc).strftime("%m-%d %H:%M")
                print(f"#{p.id} [{t} UTC] {p.exchange} {p.coin} {p.side.upper()} 진입 {p.entries} "
                      f"손절 {p.stop_px:g} 목표 {p.targets or '-'} {p.note}")
        elif args.cmd == "close":
            ok = store.close_plan(args.plan_id)
            print(f"계획 #{args.plan_id} 종료" if ok else f"활성 계획 #{args.plan_id} 없음")
            return 0 if ok else 1
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
