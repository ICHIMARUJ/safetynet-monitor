"""Safety Net 앱 실행 파일 (창 + 트레이 + 경고 팝업).

  python app.py            # 앱 실행
  python app.py --hidden   # 창 없이 트레이로 시작 (자동 실행용)

창을 닫으면 트레이(작업 표시줄 오른쪽 아래)로 숨고 감시는 계속된다. 완전히 끄려면 트레이 아이콘 → 종료.
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import threading
import time
import traceback
from typing import Any, Optional

from monitor import updater
from monitor.config import ROOT, data_dir, load_config
from monitor.mistakes import KINDS
from monitor.store import Violation
from service import Api, Monitor, app_version

UI = str(ROOT / "ui" / "index.html")
ICON = ROOT / "ui" / "img" / "reaper.png"
LOCK_PORT = 51247          # 같은 PC 에서 두 번 실행되는 것 막기
UPDATE_FIRST_SEC = 60      # 시작 후 첫 업데이트 확인
UPDATE_EVERY_SEC = 6 * 3600


class App:
    def __init__(self, hidden: bool = False):
        import webview  # 무거운 모듈은 실행할 때만 불러옴

        self.webview = webview
        self.cfg = load_config()
        self.log_path = data_dir(self.cfg) / "app.log"
        self.monitor = Monitor(self.cfg, on_violations=self.on_violations, log=self.log,
                               on_breakeven=self.on_breakeven, on_trade_closed=self.on_trade_closed)
        self.api = Api(self.cfg, self.monitor)
        self.api._hooks.update(popup_choice=self.popup_choice, demo_alert=self.demo_alert,
                               close_info=self.close_info, demo_info=self.demo_info,
                               show_main=self.show_main, restart_monitor=self.restart_monitor,
                               update_state=lambda: self.update_state, update_check=self.update_check,
                               update_apply=self.update_apply)
        self.update_state: dict[str, Any] = {}
        self._update_lock = threading.Lock()
        self.info: Optional[Any] = None   # 본전 알림·일지 입력창 (경고 팝업과 따로)
        self.quitting = False
        self.popup: Optional[Any] = None
        self.tray: Optional[Any] = None
        self.main = webview.create_window(
            "Safety Net", UI, js_api=self.api, width=1320, height=860, min_size=(1040, 680),
            background_color="#0A0406", hidden=hidden)
        self.main.events.loaded += lambda: self.main.evaluate_js("window.SN && SN.startMain()")
        self.main.events.closing += self.on_main_closing

    # ------------------------------------------------------------ 기록
    def log(self, text: str) -> None:
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + text.rstrip() + "\n")
        except OSError:
            pass

    # ------------------------------------------------------------ 창
    def on_main_closing(self) -> bool:
        if self.quitting:
            return True
        self.main.hide()          # 닫기 = 트레이로 숨기기
        self.notify("Safety Net 은 트레이에서 계속 감시 중", "완전히 끄려면 트레이 아이콘 → 종료")
        return False

    def show_main(self) -> None:
        self.main.show()
        self.main.restore()

    def quit(self) -> None:
        self.quitting = True
        self.monitor.stop()
        for w in list(self.webview.windows):
            try:
                w.destroy()
            except Exception:  # noqa: BLE001
                pass
        if self.tray:
            self.tray.stop()

    # ------------------------------------------------------------ 경고
    def on_violations(self, vs: list[Violation]) -> None:
        """감시 스레드에서 호출. 가장 심각한 1건은 팝업, 나머지는 알림으로."""
        for v in vs:
            self.log(f"경고 -{v.penalty} {v.message}")
        top = sorted(vs, key=lambda v: (v.rule != "liquidation", -v.penalty))[0]
        more = f" 외 {len(vs) - 1}건" if len(vs) > 1 else ""
        self.notify(f"⚠ {KINDS[_kind(top.rule)]['label']} −{top.penalty}{more}", top.message)
        self.open_popup(f"SN.openPopup({int(top.id)})")
        try:
            self.main.evaluate_js("window.SN && SN.refresh()")
        except Exception:  # noqa: BLE001
            pass

    def demo_alert(self, kind: str) -> None:
        self.open_popup(f"SN.openDemo({json.dumps(kind)})")

    def open_popup(self, js: str) -> None:
        if self.popup is not None:
            try:
                self.popup.destroy()
            except Exception:  # noqa: BLE001
                pass
        win = self.webview.create_window("Safety Net 경고", UI, js_api=self.api, width=900, height=600,
                                         resizable=False, on_top=True, background_color="#07060B")
        win.events.loaded += lambda: win.evaluate_js(f"window.SN && {js}")
        self.popup = win

    def restart_monitor(self) -> None:
        """키 저장 후: 설정을 다시 읽고 감시를 새로 시작."""
        old = self.monitor
        old.stop()
        self.cfg = load_config()
        self.monitor = Monitor(self.cfg, on_violations=self.on_violations, log=self.log,
                               on_breakeven=self.on_breakeven, on_trade_closed=self.on_trade_closed)
        self.api._cfg, self.api._monitor = self.cfg, self.monitor
        self.monitor.start()
        self.log("거래소 키 저장 → 감시 다시 시작")

    # ------------------------------------------------------------ 본전 알림 · 일지 입력창
    def on_breakeven(self, e: dict) -> None:
        side = "롱" if e["side"] == "long" else "숏"
        self.notify(f"본전 도착 · {e['coin'].split(':')[-1]} {side}",
                    f"평단 {e['entry']:g} 복귀 (최대 -{e['worst_pct']}% 까지 밀렸었음)")
        self.open_info(f"SN.openBreakeven({json.dumps(e, ensure_ascii=False)})", 560, 520)

    def on_trade_closed(self, t: dict) -> None:
        self.open_info(f"SN.openJournal({json.dumps(t, ensure_ascii=False)})", 560, 640)

    def demo_info(self, which: str) -> None:
        if which == "breakeven":
            self.on_breakeven({"exchange": "bybit", "coin": "ZEC", "side": "long", "entry": 697.99,
                               "mark": 697.9, "worst_pct": 3.2, "worst_upnl": -812.4, "size": 36.4})
        else:
            self.on_trade_closed({"key": "demo", "exchange": "bybit", "coin": "ZEC", "side": "short",
                                  "pnl": -1236.14, "start": int(time.time() * 1000) - 3_600_000,
                                  "end": int(time.time() * 1000),
                                  "kinds": [{"kind": "no_stop", "label": "손절 미설정", "accent": "#FF55FF"}],
                                  "emotions": ["평온", "확신", "초조", "불안", "욕심", "복수심", "지루함", "과신", "피로"],
                                  "journal": self.monitor.journal.ready, "demo": True})

    def open_info(self, js: str, w: int, h: int) -> None:
        self.close_info()
        win = self.webview.create_window("Safety Net", UI, js_api=self.api, width=w, height=h,
                                         resizable=False, on_top=True, background_color="#050203")
        win.events.loaded += lambda: win.evaluate_js(f"window.SN && {js}")
        self.info = win

    def close_info(self) -> None:
        win, self.info = self.info, None
        if win is not None:
            try:
                win.destroy()
            except Exception:  # noqa: BLE001
                pass

    def popup_choice(self, vid: int, choice: int) -> None:
        win, self.popup = self.popup, None
        if win is not None:
            win.destroy()
        if choice == 0 and vid:   # 첫 번째 선택지 = 대시보드 열어서 바로 조치
            self.show_main()

    # ------------------------------------------------------------ 트레이
    def notify(self, title: str, msg: str) -> None:
        try:
            if self.tray:
                self.tray.notify(msg[:250], title[:60])
        except Exception:  # noqa: BLE001 - 알림이 안 떠도 감시는 계속
            self.log(traceback.format_exc())

    def start_tray(self) -> None:
        try:
            import pystray
            from PIL import Image
        except ImportError:
            self.log("pystray/Pillow 없음 — 트레이 없이 실행")
            return
        menu = pystray.Menu(
            pystray.MenuItem("열기", lambda: self.show_main(), default=True),
            pystray.MenuItem("종료", lambda: self.quit()))
        self.tray = pystray.Icon("SafetyNet", Image.open(ICON), "Safety Net", menu)
        self.tray.run_detached()

    # ------------------------------------------------------------ 자동 업데이트
    def update_check(self) -> dict:
        res = updater.check(updater.read_manifest(ROOT / "update.json"), app_version())
        self.update_state = {**res, "checked_at": int(time.time() * 1000)}
        return self.update_state

    def update_apply(self) -> dict:
        if not self._update_lock.acquire(blocking=False):
            return {"ok": False, "error": "이미 업데이트 중"}
        try:
            info = self.update_check()
            if not info.get("ok") or not info.get("available"):
                return {**info, "applied": False}
            self.log(f"업데이트 {info['current']} → {info['latest']} 설치 시작")
            res = updater.apply(info, ROOT, data_dir(self.cfg))
            self.log(f"업데이트 결과: {res}")
            if res.get("ok"):
                self.notify(f"Safety Net {info['latest']} 업데이트 완료", "잠시 후 자동으로 다시 시작함")
                threading.Timer(3.0, self.restart_app).start()
            return {**res, "applied": res.get("ok", False)}
        finally:
            self._update_lock.release()

    def update_loop(self) -> None:
        time.sleep(UPDATE_FIRST_SEC)
        while not self.quitting:
            try:
                if (self.cfg.get("update") or {}).get("auto", True):
                    self.update_apply()
                else:
                    self.update_check()
            except Exception:  # noqa: BLE001 - 업데이트 실패가 앱을 멈추지 않게
                self.log(traceback.format_exc())
            for _ in range(int(UPDATE_EVERY_SEC / 5)):
                if self.quitting:
                    return
                time.sleep(5)

    def restart_app(self) -> None:
        """새 프로세스를 띄우고 지금 앱을 끈다 (새 앱은 잠금이 풀릴 때까지 잠시 기다림)."""
        flags = 0x00000008 | 0x00000200 if sys.platform == "win32" else 0   # DETACHED | NEW_PROCESS_GROUP
        args = [sys.executable, str(ROOT / "app.py"), "--after-update"] + (["--hidden"] if not self.main_visible() else [])
        subprocess.Popen(args, cwd=str(ROOT), creationflags=flags, close_fds=True)
        self.quit()

    def main_visible(self) -> bool:
        try:
            return not getattr(self.main, "hidden", False)
        except Exception:  # noqa: BLE001
            return True

    def on_start(self) -> None:
        self.start_tray()
        self.monitor.start()
        threading.Thread(target=self.update_loop, daemon=True, name="safetynet-update").start()

    def run(self) -> None:
        self.webview.start(self.on_start, http_server=True, debug="--debug" in sys.argv)


def _kind(rule: str) -> str:
    from monitor.mistakes import kind_of
    return kind_of(rule)


def single_instance(wait_sec: float = 0) -> Optional[socket.socket]:
    """같은 PC 에서 두 번 실행 방지. 업데이트 재시작 때는 이전 앱이 꺼질 때까지 기다린다."""
    end = time.time() + wait_sec
    while True:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", LOCK_PORT))
            s.listen(1)
            return s
        except OSError:
            s.close()
            if time.time() >= end:
                return None
            time.sleep(0.5)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hidden", action="store_true", help="창 없이 트레이로 시작")
    ap.add_argument("--debug", action="store_true", help="화면 개발자 도구 켜기")
    ap.add_argument("--after-update", action="store_true", help="업데이트 후 재시작 (내부용)")
    args = ap.parse_args()
    lock = single_instance(20 if args.after_update else 0)
    if lock is None:
        print("Safety Net 이 이미 실행 중 (트레이 아이콘 확인)")
        return 1
    try:
        App(hidden=args.hidden).run()
    except Exception:  # noqa: BLE001
        err = traceback.format_exc()
        print(err)
        try:
            cfg = load_config()
            (data_dir(cfg) / "app.log").open("a", encoding="utf-8").write(err)
        except Exception:  # noqa: BLE001
            pass
        return 1
    finally:
        lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
