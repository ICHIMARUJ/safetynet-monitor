"""앱 뒤에서 도는 감시 서비스 + 화면(JS)이 부르는 API.

- Monitor: 별도 스레드에서 거래소를 주기적으로 조회 → 규칙 엔진 → 위반이 생기면 on_violations 호출
- Api: 화면이 부르는 메서드. 반환값은 모두 JSON 으로 바꿀 수 있는 dict/list.
  창(window) 같은 객체는 밑줄(_) 속성에만 둔다 (pywebview 가 공개 속성을 화면에 노출하므로).

DB 는 스레드마다 따로 연다 (SQLite 연결은 만든 스레드에서만 써야 함).
"""
from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

from datetime import date, timedelta

from monitor.breakeven import BreakevenWatch
from monitor.config import ROOT, data_dir, load_events, max_leverage_for
from monitor.history import summary
from monitor import journal as jn
from monitor.trades import Trade, build_trades, link_violations
from monitor.mistakes import KINDS, REST_DAY_KEY, RULE_LABEL, kind_of
from monitor.models import AccountSnapshot
from monitor.retro import run_retro
from monitor.rules import DAY_MS, RuleEngine, day_label, day_start_ms, score_for_day
from monitor.store import Plan, Store, Violation
from plan import validate
from run import backfill, build_clients, poll

THEMES = ("akira", "lime", "ocean")
ALERT_DIR = ROOT / "alerts"
IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def app_version() -> str:
    try:
        return (ROOT / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def now_ms() -> int:
    return int(time.time() * 1000)


def _vdict(v: Violation) -> dict[str, Any]:
    k = kind_of(v.rule)
    return {"id": v.id, "ts": v.ts, "rule": v.rule, "rule_label": RULE_LABEL.get(v.rule, v.rule),
            "kind": k, "kind_label": KINDS[k]["label"], "message": v.message, "penalty": v.penalty,
            "exchange": v.exchange, "coin": v.coin, "retro": v.source == "retro"}


def _plan_dict(p: Plan) -> dict[str, Any]:
    return {"id": p.id, "exchange": p.exchange, "coin": p.coin, "side": p.side, "entries": p.entries,
            "stop": p.stop_px, "targets": p.targets, "note": p.note, "created_ts": p.created_ts,
            "status": p.status, "closed_ts": p.closed_ts}


class Monitor(threading.Thread):
    def __init__(self, cfg: dict[str, Any],
                 on_violations: Callable[[list[Violation]], None] = lambda vs: None,
                 log: Callable[[str], None] = print,
                 on_breakeven: Callable[[dict], None] = lambda e: None,
                 on_trade_closed: Callable[[dict], None] = lambda t: None):
        super().__init__(daemon=True, name="safetynet-monitor")
        self.cfg = cfg
        self.on_violations = on_violations
        self.on_breakeven = on_breakeven
        self.on_trade_closed = on_trade_closed
        self.log = log
        self.journal = jn.JournalConfig(data_dir(cfg) / "journal.json")
        self.pending_trades: dict[str, Trade] = {}      # 감정·메모 입력을 기다리는 거래
        self._journal_sent: dict[str, str] = {}          # 날짜 → 마지막으로 보낸 내용 (같은 값 반복 전송 방지)
        self._journal_at = 0.0
        self.breakeven: Optional[BreakevenWatch] = None
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.view: dict[str, Any] = {"phase": "starting", "exchanges": {}, "positions": [],
                                     "accounts": [], "updated": None, "error": None, "notes": []}

    def _set(self, **kw: Any) -> None:
        with self.lock:
            self.view.update(kw)

    def snapshot_view(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self.view))

    def stop(self) -> None:
        self.stop_event.set()

    # ------------------------------------------------------------ 스레드 본체
    def run(self) -> None:
        try:
            self._run()
        except Exception as e:  # noqa: BLE001 - 화면에 원인을 보여주고 스레드 종료
            self._set(phase="error", error=f"감시 중단: {e}")
            self.log(traceback.format_exc())

    def _run(self) -> None:
        cfg = self.cfg
        clients = build_clients(cfg)
        enabled = [n for n, c in cfg["exchanges"].items() if c.get("enabled")]
        self._set(exchanges={n: {"ok": n in clients, "msg": "" if n in clients else "키·주소 설정 필요"}
                             for n in enabled})
        if not clients:
            self._set(phase="error",
                      error="연결된 거래소가 없음 — config.yaml 의 enabled 와 .env 의 키·주소를 확인")
            return
        store = Store(data_dir(cfg) / "safetynet.db")
        events = load_events(cfg)
        engine = RuleEngine(cfg, store, market=clients, events=events)
        self.breakeven = BreakevenWatch(cfg, store.get_kv("breakeven_state") or {})
        removed = store.cleanup_once()
        if removed:
            self.log(f"잘못 기록된 일 손실 한도 경고 {removed}건 정리")
        hl = clients.get("hyperliquid")
        if hl is not None and hasattr(hl, "add_dex"):   # 과거에 거래한 HIP-3 마켓을 바로 조회 대상에
            for coin in store.coins("hyperliquid"):
                if ":" in coin:
                    hl.add_dex(coin.split(":", 1)[0])
        self._set(phase="backfill")
        backfill(store, clients)
        self._set(phase="retro")
        found = run_retro(cfg, store, events)
        if found is not None:
            self.log(f"과거 체결로 실수 {found}건 소급 계산")
        self._set(phase="live")
        interval = max(5, int(cfg["poll_interval_sec"]))
        while not self.stop_event.is_set():
            try:
                self._tick(engine, store, clients)
            except Exception as e:  # noqa: BLE001 - 한 번 실패해도 다음 주기에 계속
                self._set(error=f"점검 중 오류 (다음 주기에 재시도): {e}")
                self.log(traceback.format_exc())
            self.stop_event.wait(interval)
        store.close()

    def _tick(self, engine: RuleEngine, store: Store, clients: dict[str, Any]) -> None:
        snaps, new_fills = poll(store, clients)
        ok = {s.exchange for s in snaps}
        status = {n: {"ok": n in ok, "msg": "" if n in ok else "조회 실패 — 다음 주기에 재시도"}
                  for n in clients}
        if not snaps:
            self._set(exchanges=status)
            return
        vs = engine.process(snaps, new_fills)
        view = self._build_view(engine, store, snaps)
        self._set(**view, exchanges=status, updated=now_ms(), error=None)
        if vs:
            self._safe(self.on_violations, vs)
        # 본전 복귀 알림
        if self.breakeven is not None:
            evs = self.breakeven.update([p for s in snaps for p in s.positions], ok)
            store.set_kv("breakeven_state", self.breakeven.state)
            for e in evs:
                self.log(f"본전 복귀 [{e.exchange}] {e.coin} {e.side} 평단 {e.entry} (최대 -{e.worst_pct}%)")
                self._safe(self.on_breakeven, e.__dict__)
        # 거래 종료 → 감정·메모 입력창
        self._closed_trades(store, snaps, new_fills)
        # 구글 시트 일지 (10분마다, 바뀐 값만)
        self._journal_sync(store, view["total"]["equity"])

    def _safe(self, fn: Callable, arg: Any) -> None:
        try:
            fn(arg)
        except Exception:  # noqa: BLE001 - 알림 실패가 감시를 멈추지 않게
            self.log(traceback.format_exc())

    def _closed_trades(self, store: Store, snaps: list[AccountSnapshot], new_fills: list) -> None:
        touched = {(f.exchange, f.coin) for f in new_fills if f.action in ("close", "flip")}
        if not touched:
            return
        done: list = store.get_kv("journal_prompted") or []
        now = now_ms()
        for exch, coin in touched:
            fills = [f for f in store.fills_between(now - 30 * DAY_MS) if f.exchange == exch and f.coin == coin]
            trades = build_trades(fills)
            link_violations(trades, [v for v in store.violations_between(now - 30 * DAY_MS)])
            for t in trades:
                key = f"{exch}:{coin}:{t.end_ts}"
                if not t.closed or t.end_ts < now - 15 * 60_000 or key in done:
                    continue
                done.append(key)
                self.pending_trades[key] = t
                self.log(f"거래 종료 {key} {t.pnl:+.2f}")
                if self.journal.prompt:
                    self._safe(self.on_trade_closed, {
                        "key": key, "exchange": exch, "coin": coin, "side": t.side, "pnl": round(t.pnl, 2),
                        "start": t.start_ts, "end": t.end_ts,
                        "kinds": [{"kind": k, "label": KINDS[k]["label"], "accent": KINDS[k]["accent"]}
                                  for k in sorted(t.kinds)],
                        "emotions": jn.EMOTIONS, "journal": self.journal.ready})
        store.set_kv("journal_prompted", done[-500:])

    def _journal_sync(self, store: Store, equity: Optional[float], force: bool = False) -> Optional[dict]:
        j = self.journal
        if not j.ready or (not force and time.time() - self._journal_at < 600):
            return None
        self._journal_at = time.time()
        today = date.today()
        res = None
        for d, eq in ((today - timedelta(days=1), None), (today, equity)):
            p = jn.daily_payload(store, d, eq)
            sig = json.dumps(p, sort_keys=True, ensure_ascii=False)
            if not force and self._journal_sent.get(d.isoformat()) == sig:
                continue
            res = jn.post(j.url, {**p, "token": j.token})
            if res.get("ok"):
                self._journal_sent[d.isoformat()] = sig
            else:
                self.log(f"일지 기록 실패 {d}: {res.get('error')}")
        return res

    def _build_view(self, engine: RuleEngine, store: Store, snaps: list[AccountSnapshot]) -> dict[str, Any]:
        cfg = self.cfg
        lim_acc = float(cfg["rules"]["leverage"]["account_max"])
        liq_min = float(cfg["rules"]["liq_distance_min_pct"])
        positions, accounts, notes = [], [], []
        for s in snaps:
            if s.equity_note:
                notes.append(f"{s.exchange}: {s.equity_note}")
            accounts.append({"exchange": s.exchange, "equity": s.equity, "notional": s.total_notional,
                             "leverage": s.effective_leverage()})
            for p in s.positions:
                lev = s.effective_leverage(p)
                lim = max_leverage_for(cfg, p.coin)
                dist = p.liq_distance_pct()
                stop = engine._stop_for(s, p)
                plan = store.active_plan_for(p.exchange, p.coin, p.side)
                positions.append({
                    "exchange": p.exchange, "coin": p.coin, "side": p.side, "size": p.size,
                    "entry": p.entry_px, "mark": p.mark_px, "upnl": p.upnl, "notional": p.notional,
                    "leverage": lev, "lev_limit": lim, "lev_warn": lev is not None and lev > lim,
                    "liq": p.liq_px, "liq_dist": dist, "liq_warn": dist is not None and dist < liq_min,
                    "stop": stop, "plan_stop": plan.stop_px if plan else None,
                    "plan_id": plan.id if plan else None})
        eq = sum(a["equity"] for a in accounts)
        tot = sum(a["notional"] for a in accounts)
        return {"positions": positions, "accounts": accounts, "notes": notes,
                "total": {"equity": eq, "notional": tot, "leverage": tot / eq if eq > 0 else None,
                          "lev_limit": lim_acc}}


class Api:
    """화면(JS)에서 window.pywebview.api.<메서드>() 로 부른다."""

    def __init__(self, cfg: dict[str, Any], monitor: Optional[Monitor] = None):
        self._cfg = cfg
        self._monitor = monitor
        self._local = threading.local()
        self._dir = data_dir(cfg)
        self._ui_path = self._dir / "ui.json"
        self._hooks: dict[str, Callable[..., Any]] = {}   # 앱 쪽 동작 (팝업 닫기, 창 열기)

    def _store(self) -> Store:
        st = getattr(self._local, "store", None)
        if st is None:
            st = Store(self._dir / "safetynet.db")
            self._local.store = st
        return st

    def _ui(self) -> dict[str, Any]:
        try:
            return json.loads(self._ui_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    # ------------------------------------------------------------ 대시보드
    def state(self) -> dict[str, Any]:
        cfg, st, now = self._cfg, self._store(), now_ms()
        view = self._monitor.snapshot_view() if self._monitor else {"phase": "offline"}
        today = day_start_ms(now)
        week = []
        for i in range(6, -1, -1):
            d = today - i * DAY_MS
            week.append({"day": time.strftime("%m-%d", time.gmtime(d / 1000)),
                         "score": score_for_day(cfg, st, d)})
        return {
            **view,
            "score": score_for_day(cfg, st, now), "score_start": int(cfg["score"]["start"]),
            "today": [_vdict(v) for v in reversed(st.violations_between(today, today + DAY_MS))],
            "week": week,
            "theme": self.get_theme(),
            "limits": {"account": cfg["rules"]["leverage"]["account_max"],
                       "liq": cfg["rules"]["liq_distance_min_pct"],
                       "risk": cfg["rules"]["max_risk_per_trade_pct"],
                       "daily_loss": cfg["rules"]["daily_loss_limit_pct"]},
            "exchanges_enabled": [n for n, c in cfg["exchanges"].items() if c.get("enabled")],
            "need_setup": self._need_setup(),
            "version": app_version(),
        }

    def history(self) -> dict[str, Any]:
        return summary(self._cfg, self._store(), now_ms())

    # ------------------------------------------------------------ 테마
    def get_theme(self) -> str:
        t = self._ui().get("theme") or self._cfg.get("ui", {}).get("theme") or "akira"
        return t if t in THEMES else "akira"

    def set_theme(self, name: str) -> str:
        if name in THEMES:
            ui = self._ui()
            ui["theme"] = name
            self._ui_path.write_text(json.dumps(ui, ensure_ascii=False), encoding="utf-8")
        return self.get_theme()

    # ------------------------------------------------------------ 매매 계획
    def plans(self) -> dict[str, Any]:
        st = self._store()
        done = [p for p in st.plans(None) if p.status != "active"][-20:]
        return {"active": [_plan_dict(p) for p in st.plans("active")],
                "recent": [_plan_dict(p) for p in reversed(done)],
                "exchanges": [n for n, c in self._cfg["exchanges"].items() if c.get("enabled")]}

    def add_plan(self, data: dict[str, Any]) -> dict[str, Any]:
        def prices(v: Any) -> list[float]:
            if isinstance(v, (int, float)):
                return [float(v)]
            out = []
            for part in str(v or "").replace(" ", "").split(","):
                if part:
                    out.append(float(part))
            return out
        try:
            entries, targets = prices(data.get("entries")), prices(data.get("targets"))
            stop_raw = str(data.get("stop") or "").replace(",", "").strip()
            stop = float(stop_raw) if stop_raw else None
        except ValueError:
            return {"ok": False, "errors": ["가격은 숫자로 입력 (여러 개는 쉼표로 구분)"]}
        if any(x <= 0 for x in entries + targets) or (stop is not None and stop <= 0):
            return {"ok": False, "errors": ["가격은 0보다 커야 함"]}
        exch = str(data.get("exchange") or "").strip()
        coin = str(data.get("coin") or "").strip().upper()
        side = str(data.get("side") or "").strip()
        errs = []
        if exch not in self._cfg["exchanges"]:
            errs.append("거래소를 선택해야 함")
        if not coin:
            errs.append("코인을 입력해야 함")
        if side not in ("long", "short"):
            errs.append("방향(롱/숏)을 선택해야 함")
        p = Plan(exchange=exch, coin=coin, side=side or "long", entries=entries, stop_px=stop,
                 targets=targets, note=str(data.get("note") or "").strip())
        errs += [e.replace("(--entries)", "").replace("(--stop)", "") for e in validate(p)]
        if errs:
            return {"ok": False, "errors": errs}
        pid = self._store().add_plan(p)
        return {"ok": True, "id": pid}

    def close_plan(self, plan_id: int, status: str = "cancelled") -> bool:
        return self._store().close_plan(int(plan_id), status="done" if status == "done" else "cancelled")

    # ------------------------------------------------------------ 경고 팝업
    def alert(self, vid: int) -> Optional[dict[str, Any]]:
        st = self._store()
        v = st.violation(int(vid))
        if v is None:
            return None
        d = _vdict(v)
        meta = dict(KINDS[d["kind"]])
        d.update(meta=meta, image=self._image(meta.get("image", "")),
                 score=score_for_day(self._cfg, st, now_ms()), score_start=int(self._cfg["score"]["start"]),
                 theme=self.get_theme())
        return d

    def _image(self, name: str) -> Optional[str]:
        """alerts 폴더의 그림을 data URI 로 (png 외 jpg 등 같은 이름이면 허용)."""
        if not name:
            return None
        stem = Path(name).stem
        for ext in IMG_EXT:
            p = ALERT_DIR / (stem + ext)
            if p.is_file() and p.stat().st_size < 8_000_000:
                mime = "image/jpeg" if ext in (".jpg", ".jpeg") else f"image/{ext[1:]}"
                return f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode()
        return None

    def popup_choice(self, vid: int, choice: int) -> None:
        if int(vid) and int(choice) == 1:   # GAME OVER → "오늘은 쉰다": 오늘 신규 진입은 경고
            v = self._store().violation(int(vid))
            if v is not None and v.rule == "liquidation":
                self._store().set_kv(REST_DAY_KEY, day_label(now_ms()))
        hook = self._hooks.get("popup_choice")
        if hook:
            hook(int(vid), int(choice))

    def demo_alert(self, kind: str) -> Optional[int]:
        """설정 확인용: 실제 기록 없이 팝업 모양만 보기 (점수에는 반영 안 함)."""
        hook = self._hooks.get("demo_alert")
        if hook and kind in KINDS:
            hook(kind)
            return 1
        return None

    def kinds(self) -> dict[str, Any]:
        return {k: {"label": d["label"], "accent": d["accent"], "image_name": d["image_name"],
                    "has_image": self._image(d["image"]) is not None if d["image"] else True}
                for k, d in KINDS.items()}

    def demo_info(self, which: str) -> None:
        """설정 확인용: 본전 알림 / 일지 입력창 미리보기."""
        hook = self._hooks.get("demo_info")
        if hook:
            hook(which)

    def demo_payload(self, kind: str) -> dict[str, Any]:
        meta = dict(KINDS[kind])
        return {"id": 0, "ts": now_ms(), "rule": kind, "rule_label": "미리보기", "kind": kind,
                "kind_label": meta["label"], "message": f"미리보기 — {meta['label']} 경고 팝업",
                "penalty": 0, "exchange": None, "coin": None, "retro": False, "meta": meta,
                "image": self._image(meta.get("image", "")), "score": score_for_day(self._cfg, self._store(), now_ms()),
                "score_start": int(self._cfg["score"]["start"]), "theme": self.get_theme(), "demo": True}

    # ------------------------------------------------------------ 시작 시 자동 실행 (Windows)
    def _startup_file(self) -> Optional[Path]:
        appdata = os.environ.get("APPDATA")
        if sys.platform != "win32" or not appdata:
            return None
        return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "SafetyNet.bat"

    def get_autostart(self) -> dict[str, Any]:
        f = self._startup_file()
        return {"supported": f is not None, "on": bool(f and f.exists())}

    def set_autostart(self, on: bool) -> dict[str, Any]:
        f = self._startup_file()
        if f is None:
            return self.get_autostart()
        if on:
            exe = Path(sys.executable)
            w = exe.with_name("pythonw.exe")
            py = w if w.exists() else exe
            f.write_text(f'@echo off\r\ncd /d "{ROOT}"\r\nstart "" "{py}" app.py --hidden\r\n', encoding="utf-8")
        elif f.exists():
            f.unlink()
        return self.get_autostart()

    # ------------------------------------------------------------ 구글 시트 일지
    def _j(self) -> jn.JournalConfig:
        j = getattr(self._monitor, "journal", None)
        return j if j is not None else jn.JournalConfig(self._dir / "journal.json")

    def journal_get(self) -> dict[str, Any]:
        j = self._j()
        return {"url": j.url, "sheet_url": j.sheet_url, "enabled": j.enabled, "prompt": j.prompt, "ready": j.ready,
                "script": jn.script_for(j.token, j.sheet_url)}

    def journal_set_sheet(self, sheet_url: str) -> dict[str, Any]:
        j = self._j()
        u = str(sheet_url or "").strip()
        if u and not jn.sheet_id(u):
            return {**self.journal_get(), "error": "구글 시트 주소에서 ID 를 찾지 못함 — docs.google.com/spreadsheets/d/… 주소 전체를 붙여넣기"}
        j.sheet_url = u
        j.save()
        return self.journal_get()

    def journal_set(self, url: str, enabled: bool, prompt: bool) -> dict[str, Any]:
        j = self._j()
        j.url, j.enabled, j.prompt = str(url or "").strip(), bool(enabled), bool(prompt)
        j.save()
        return self.journal_get()

    def journal_test(self, url: str = "") -> dict[str, Any]:
        j = self._j()
        u = str(url or j.url).strip()
        if not u.startswith("https://script.google.com/"):
            return {"ok": False, "error": "웹 앱 URL 은 https://script.google.com/ 으로 시작해야 함"}
        return jn.post(u, {"action": "ping", "token": j.token})

    def journal_sync_now(self) -> dict[str, Any]:
        if not hasattr(self._monitor, "_journal_sync"):
            return {"ok": False, "error": "감시 중이 아님"}
        view = self._monitor.snapshot_view()
        eq = (view.get("total") or {}).get("equity")
        res = self._monitor._journal_sync(self._store(), eq, force=True)
        return res or {"ok": False, "error": "일지 연결이 꺼져 있음"}

    def journal_submit(self, key: str, emotion: str, memo: str) -> dict[str, Any]:
        """포지션 정리 입력창의 감정·메모를 그날 일지 줄에 기록."""
        t = getattr(self._monitor, "pending_trades", {}).get(key)
        if t is None:
            return {"ok": False, "error": "거래 정보를 찾을 수 없음 (앱 재시작 후에는 기록 불가)"}
        j = self._j()
        if not j.ready:
            return {"ok": False, "error": "구글 시트 일지가 연결되지 않음 — ⚙ 설정에서 연결"}
        d = date.fromtimestamp(t.end_ts / 1000)
        res = jn.post(j.url, {**jn.trade_payload(t, emotion, str(memo or "")[:500], d), "token": j.token})
        if res.get("ok"):
            self._monitor.pending_trades.pop(key, None)
        return res

    def show_main(self) -> None:
        hook = self._hooks.get("show_main")
        if hook:
            hook()

    def close_info(self) -> None:
        hook = self._hooks.get("close_info")
        if hook:
            hook()

    # ------------------------------------------------------------ 거래소 키 설정 (첫 실행 화면)
    def _need_setup(self) -> list[str]:
        ex, sec = self._cfg["exchanges"], self._cfg.get("secrets", {})
        need = []
        if ex["bybit"].get("enabled") and not (sec.get("bybit_key") and sec.get("bybit_secret")):
            need.append("bybit")
        if ex["hyperliquid"].get("enabled") and not sec.get("hl_wallet"):
            need.append("hyperliquid")
        return need

    def setup_status(self) -> dict[str, Any]:
        from monitor import secretstore
        sec = self._cfg.get("secrets", {})
        return {"enabled": [n for n, c in self._cfg["exchanges"].items() if c.get("enabled")],
                "need": self._need_setup(),
                "saved": {"bybit_key": secretstore.mask(sec.get("bybit_key", "")),
                          "hl_wallet": secretstore.mask(sec.get("hl_wallet", ""))}}

    def setup_test(self, data: dict[str, Any]) -> dict[str, Any]:
        """입력한 키로 실제 조회. Bybit 는 읽기 전용 키인지 확인 (아니면 저장 불가)."""
        from monitor.exchanges.bybit import BybitClient, BybitError
        from monitor.exchanges.hyperliquid import HyperliquidClient, HyperliquidError
        sec = self._cfg.get("secrets", {})
        out: dict[str, Any] = {}
        en = [n for n, c in self._cfg["exchanges"].items() if c.get("enabled")]
        if "bybit" in en:
            key = str(data.get("bybit_key") or sec.get("bybit_key") or "").strip()
            secret = str(data.get("bybit_secret") or sec.get("bybit_secret") or "").strip()
            try:
                c = BybitClient(key, secret)
                info = c.key_info()
                snap = c.snapshot()
                ok = info["read_only"]
                out["bybit"] = {"ok": ok, "equity": round(snap.equity, 2), "read_only": info["read_only"],
                                "error": "" if ok else "읽기·쓰기 권한 키 — 안전을 위해 '읽기 전용'으로 새로 발급한 키만 저장 가능"}
            except (BybitError, Exception) as e:  # noqa: BLE001
                out["bybit"] = {"ok": False, "error": str(e)}
        if "hyperliquid" in en:
            w = str(data.get("hl_wallet") or sec.get("hl_wallet") or "").strip()
            try:
                snap = HyperliquidClient(w).snapshot()
                out["hyperliquid"] = {"ok": True, "equity": round(snap.equity, 2)}
            except (HyperliquidError, Exception) as e:  # noqa: BLE001
                msg = "지갑 주소 형식이 아님 (0x 로 시작하는 42자리)" if "HL_WALLET" in str(e) else str(e)
                out["hyperliquid"] = {"ok": False, "error": msg}
        out["ok"] = bool(out) and all(v["ok"] for v in out.values() if isinstance(v, dict))
        return out

    def setup_save(self, data: dict[str, Any]) -> dict[str, Any]:
        from monitor import secretstore
        test = self.setup_test(data)
        if not test.get("ok"):
            return {**test, "saved": False}
        cur = secretstore.load(self._dir)
        for k in secretstore.FIELDS:
            v = str(data.get(k) or "").strip()
            if v:
                cur[k] = v
        secretstore.save(self._dir, cur)
        hook = self._hooks.get("restart_monitor")
        if hook:
            hook()
        return {**test, "saved": True}

    # ------------------------------------------------------------ 자동 업데이트
    def update_status(self) -> dict[str, Any]:
        st = dict(self._hooks.get("update_state", lambda: {})())
        return {"version": app_version(), **st}

    def update_check(self) -> dict[str, Any]:
        hook = self._hooks.get("update_check")
        return hook() if hook else {"ok": False, "error": "업데이트 기능 없음"}

    def update_apply(self) -> dict[str, Any]:
        hook = self._hooks.get("update_apply")
        return hook() if hook else {"ok": False, "error": "업데이트 기능 없음"}
