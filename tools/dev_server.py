"""개발용: 브라우저에서 앱 화면 확인 (pywebview 없이).

  python tools/dev_server.py [DB 폴더]   → http://127.0.0.1:8765/index.html#dev
감시 스레드 대신 예시 포지션을 보여주고, 기록·계획은 지정한 DB 를 그대로 쓴다.
"""
from __future__ import annotations

import json
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from monitor.config import load_config  # noqa: E402
from service import Api  # noqa: E402


class FakeMonitor:
    def snapshot_view(self):
        return {"phase": "live", "updated": 1795474000000, "error": None, "notes": [],
                "exchanges": {"hyperliquid": {"ok": True, "msg": ""}, "bybit": {"ok": True, "msg": ""}},
                "accounts": [{"exchange": "hyperliquid", "equity": 1200.02, "notional": 1600, "leverage": 1.3},
                             {"exchange": "bybit", "equity": 1208.08, "notional": 21360, "leverage": 17.7}],
                "total": {"equity": 2408.1, "notional": 22960, "leverage": 9.53, "lev_limit": 20},
                "positions": [
                    {"exchange": "bybit", "coin": "BTC", "side": "long", "size": 0.05, "entry": 96500, "mark": 97200,
                     "upnl": 35, "notional": 4860, "leverage": 4.0, "lev_limit": 20, "lev_warn": False, "liq": 83400,
                     "liq_dist": 14.2, "liq_warn": False, "stop": 95100, "plan_stop": None, "plan_id": 3},
                    {"exchange": "bybit", "coin": "ZEC", "side": "short", "size": 12.4, "entry": 694.86, "mark": 712.4,
                     "upnl": -87.7, "notional": 8700, "leverage": 7.2, "lev_limit": 5, "lev_warn": True, "liq": 764.3,
                     "liq_dist": 7.3, "liq_warn": True, "stop": None, "plan_stop": None, "plan_id": None},
                    {"exchange": "hyperliquid", "coin": "HYPE", "side": "long", "size": 15, "entry": 41.2, "mark": 41.85,
                     "upnl": 9.75, "notional": 627.75, "leverage": 0.5, "lev_limit": 5, "lev_warn": False, "liq": None,
                     "liq_dist": None, "liq_warn": False, "stop": 39.5, "plan_stop": None, "plan_id": 4}]}


def main() -> None:
    cfg = load_config()
    cfg["exchanges"]["hyperliquid"]["enabled"] = True
    cfg["exchanges"]["bybit"]["enabled"] = True
    if len(sys.argv) > 1:
        cfg["data_dir"] = sys.argv[1]
    api = Api(cfg, FakeMonitor())

    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(ROOT / "ui"), **kw)

        def do_POST(self):
            name = self.path.split("/api/", 1)[-1]
            args = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"[]")
            fn = getattr(api, name, None) if not name.startswith("_") else None
            res = fn(*args) if fn else {"error": "no method"}
            body = json.dumps(res, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    print("http://127.0.0.1:8765/index.html#dev")
    ThreadingHTTPServer(("127.0.0.1", 8765), H).serve_forever()


if __name__ == "__main__":
    main()
