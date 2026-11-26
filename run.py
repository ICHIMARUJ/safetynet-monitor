"""Safety Net 실행: 거래소를 주기적으로 조회하고 위반을 감지·기록한다.

사용법:
  python run.py                      # 계속 실행 (Ctrl+C 로 종료)
  python run.py --once               # 한 번만 점검하고 종료
  python run.py --once --log out.txt # 화면 출력을 UTF-8 파일에도 저장
                                     # (PowerShell 의 > 는 한글이 깨지므로 이 옵션 사용)

4단계에서 앱 창·Windows 알림이 붙기 전까지는 이 창에 경고가 출력된다.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from typing import Any

from monitor.config import data_dir, load_config, load_events
from monitor.exchanges.bybit import BybitClient, BybitError
from monitor.exchanges.hyperliquid import HyperliquidClient, HyperliquidError
from monitor.models import AccountSnapshot, Fill
from monitor.retro import run_retro
from monitor.rules import RuleEngine, score_for_day
from monitor.store import Store, Violation

if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def say(text: str) -> None:
    """화면 출력. 앱(창 모드)에서는 콘솔이 없거나 한글 인코딩이 달라도 오류 없이 넘어간다."""
    try:
        print(text, flush=True)
    except Exception:  # noqa: BLE001
        pass


class _Tee:
    """화면 출력을 그대로 UTF-8 파일에도 기록."""

    def __init__(self, stream: Any, path: str):
        self.stream = stream
        self.file = open(path, "a", encoding="utf-8", buffering=1)  # 줄 단위로 바로 기록

    def write(self, text: str) -> int:
        self.stream.write(text)
        self.file.write(text)
        return len(text)

    def flush(self) -> None:
        self.stream.flush()
        self.file.flush()


def now_ms() -> int:
    return int(time.time() * 1000)


def stamp(ms: int | None = None) -> str:
    ms = ms if ms is not None else now_ms()
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%m-%d %H:%M:%S")


def build_clients(cfg: dict[str, Any]) -> dict[str, Any]:
    """켜진 거래소 연결. 키·주소가 없는 거래소는 안내만 하고 건너뜀."""
    clients: dict[str, Any] = {}
    ex, sec = cfg["exchanges"], cfg["secrets"]
    if ex["hyperliquid"].get("enabled"):
        try:
            clients["hyperliquid"] = HyperliquidClient(sec.get("hl_wallet", ""),
                                                       dexes=ex["hyperliquid"].get("dexes"))
        except HyperliquidError as e:
            say(f"[설정 안내] hyperliquid 건너뜀 — {e}")
    if ex["bybit"].get("enabled"):
        try:
            clients["bybit"] = BybitClient(sec.get("bybit_key", ""), sec.get("bybit_secret", ""))
        except BybitError as e:
            say(f"[설정 안내] bybit 건너뜀 — {e}")
    return clients


def backfill(store: Store, clients: dict[str, Any]) -> None:
    """처음 실행 시 과거 체결을 받아 '평소 패턴' 기준과 통계에 쓴다. 과거 체결로는 경고하지 않음.
    Hyperliquid: 받을 수 있는 전부(최근 10,000건) / Bybit: 최근 90일"""
    for name, client in clients.items():
        if store.last_fill_ts(name) is not None:
            continue
        days = getattr(client, "backfill_days", None)
        start = 0 if days is None else now_ms() - int(days) * 86_400_000
        try:
            n = len(store.add_fills(client.fills_since(start)))
            say(f"[{stamp()}] {name}: 과거 체결 {n}건 불러옴 (기준 데이터)")
        except Exception as e:  # noqa: BLE001 - 거래소별 실패는 기록만 하고 계속
            say(f"[{stamp()}] {name}: 과거 체결 불러오기 실패 — {e}")


def poll(store: Store, clients: dict[str, Any]) -> tuple[list[AccountSnapshot], list[Fill]]:
    snaps: list[AccountSnapshot] = []
    new_fills: list[Fill] = []
    for name, client in clients.items():
        try:
            snaps.append(client.snapshot())
            last = store.last_fill_ts(name)
            since = last if last is not None else now_ms() - 60_000
            new_fills += store.add_fills(client.fills_since(since))
        except Exception as e:  # noqa: BLE001 - 한 거래소 실패가 전체를 멈추지 않게
            say(f"[{stamp()}] {name}: 조회 실패 — {e} (다음 주기에 재시도)")
    return snaps, new_fills


_shown_notes: set[str] = set()


def report(cfg: dict[str, Any], store: Store, snaps: list[AccountSnapshot],
           violations: list[Violation]) -> None:
    for s in snaps:  # 같은 안내는 한 번만 출력
        if s.equity_note and s.equity_note not in _shown_notes:
            _shown_notes.add(s.equity_note)
            print(f"[{stamp()}] ℹ {s.exchange}: {s.equity_note}")
    for v in violations:
        print(f"[{stamp(v.ts)}] ⚠ -{v.penalty}점 {v.message}")
    score = score_for_day(cfg, store, now_ms())
    n_pos = sum(len(s.positions) for s in snaps)
    equity = sum(s.equity for s in snaps)
    print(f"[{stamp()}] 오늘 규율 점수 {score}/{cfg['score']['start']} | 포지션 {n_pos}개 | "
          f"자본 ${equity:,.2f}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="한 번만 점검하고 종료")
    ap.add_argument("--log", help="출력을 UTF-8 파일에도 저장 (이어쓰기)")
    args = ap.parse_args()
    if args.log:
        sys.stdout = _Tee(sys.stdout, args.log)

    cfg = load_config()
    clients = build_clients(cfg)
    if not clients:
        print("[설정 오류] 연결된 거래소가 없음 (config.yaml 의 enabled 와 .env 의 키·주소 확인)")
        return 1

    store = Store(data_dir(cfg) / "safetynet.db")
    events = load_events(cfg)
    engine = RuleEngine(cfg, store, market=clients, events=events)
    interval = max(5, int(cfg["poll_interval_sec"]))
    print(f"Safety Net 시작 — 거래소: {', '.join(clients)} / 조회 주기 {interval}초")
    backfill(store, clients)
    found = run_retro(cfg, store, events)
    if found is not None:
        print(f"[{stamp()}] 과거 체결로 실수 {found}건 소급 계산 (앱의 실수 기록 탭에서 확인)")

    try:
        while True:
            snaps, new_fills = poll(store, clients)
            if snaps:
                report(cfg, store, snaps, engine.process(snaps, new_fills))
            if args.once:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n종료")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
