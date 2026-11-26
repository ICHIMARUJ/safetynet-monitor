"""구글 시트 일지: 기록 값 계산 + Apps Script 를 가짜 시트로 실행해 검증 (node 필요)."""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import date, datetime

import pytest

from monitor import journal as jn
from monitor.store import Store, Violation
from monitor.trades import Trade
from tests.test_trades import f

HEAD5 = ["No.", "날짜", "요일", "전일 예산", "당일 예산", "입출금\n(+입금/-출금)", "일당\n(손익)", "수익률", "목표\n달성",
         "총이익\n(USDT)", "총손실\n(USDT, 양수)", "누적 손익", "누적\n수익률", "비고 (전략 / 시장 상황 / 심리 / 반성)",
         "", "", "", "실수 태그\n(여러 개면 쉼표로 구분)", "실수\n수", "진입 시\n감정", "메모 (왜 들어갔나 / 무엇을 배웠나)"]

HARNESS = r"""
const fs = require('fs');
const [,, scriptPath, rowsPath, reqPath] = process.argv;
const rows = JSON.parse(fs.readFileSync(rowsPath, 'utf8')).map(r => r.map(v => (typeof v === 'string' && /^D:/.test(v)) ? new Date(v.slice(2) + 'T00:00:00') : v));
const W = Math.max(...rows.map(r => r.length));
rows.forEach(r => { while (r.length < W) r.push(''); });
const sheet = {
  getLastRow: () => rows.length, getLastColumn: () => W,
  getRange: (r, c, nr = 1, nc = 1) => ({
    getValues: () => rows.slice(r - 1, r - 1 + nr).map(x => x.slice(c - 1, c - 1 + nc)),
    getValue: () => rows[r - 1][c - 1], setValue: (v) => { rows[r - 1][c - 1] = v; } }) };
const TAB = process.env.TAB || '일지';
sheet.getName = () => TAB;
const blank = { getName: () => '시트1', getLastRow: () => 0, getLastColumn: () => 0 };
global.SpreadsheetApp = { getActive: () => ({ getName: () => '선물 일지', getSheetByName: (n) => n === TAB ? sheet : null,
                                              getSheets: () => process.env.NOJOURNAL ? [blank] : [blank, sheet],
                                              getSpreadsheetTimeZone: () => process.env.TZNULL ? null : 'Europe/London' }) };
global.ContentService = { createTextOutput: (s) => ({ s, setMimeType() { return this; } }), MimeType: { JSON: 'json' } };
global.Session = { getScriptTimeZone: () => 'Europe/London' };
global.Utilities = { formatDate: (d, tz) => { if (typeof tz !== 'string' || !tz) throw new Error('잘못된 인수(timeZone)'); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`; } };
eval(fs.readFileSync(scriptPath, 'utf8') + '\nglobal.doPost = doPost;');
const results = [];
for (const req of JSON.parse(fs.readFileSync(reqPath, 'utf8'))) results.push(JSON.parse(doPost({ postData: { contents: JSON.stringify(req) } }).s));
console.log(JSON.stringify({ out: results, rows }));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node 없음")
def test_apps_script_on_fake_sheet(tmp_path):
    tok = "T0K"
    (tmp_path / "s.gs").write_text(jn.script_for(tok), encoding="utf-8")
    (tmp_path / "h.js").write_text(HARNESS, encoding="utf-8")
    rows = [["선물 트레이딩 일지 (USDT)"], ["노란색 셀만 입력"], [""], ["기본"], HEAD5]
    rows += [["", f"D:2026-11-{d:02d}"] + [""] * 19 for d in range(25, 31)]
    rows[6][17] = "FOMO"                              # 11-26 줄: 사용자가 미리 적어둔 태그
    (tmp_path / "rows.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    reqs = [{"action": "ping", "token": tok},
            {"action": "ping", "token": "bad"},
            {"action": "daily", "token": tok, "date": "2026-11-26", "equity": 80123.5, "profit": 1200, "loss": 300,
             "tags": ["손절 미설정", "FOMO"]},
            {"action": "trade", "token": tok, "date": "2026-11-26", "emotion": "초조", "memo": "[01:10 ZEC 숏 −$1,236] 추격",
             "tags": ["복구 매매"]},
            {"action": "trade", "token": tok, "date": "2026-11-26", "emotion": "평온", "memo": "[02:00 BTC 롱 +$50] 계획대로", "tags": []},
            {"action": "daily", "token": tok, "date": "2027-05-01", "equity": 1}]
    (tmp_path / "req.json").write_text(json.dumps(reqs, ensure_ascii=False), encoding="utf-8")
    r = subprocess.run(["node", str(tmp_path / "h.js"), str(tmp_path / "s.gs"), str(tmp_path / "rows.json"),
                        str(tmp_path / "req.json")], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    res = json.loads(r.stdout)
    out, row = res["out"], res["rows"][6]                # 11-26 = 7번째 줄
    assert out[0]["ok"] and set(out[0]["columns"]) == {"date", "equity", "profit", "loss", "tags", "emotion", "memo"}
    assert not out[1]["ok"] and "토큰" in out[1]["error"]
    assert out[2] == {"ok": True, "row": 7}
    assert row[4] == 80123.5 and row[9] == 1200 and row[10] == 300
    assert row[17] == "FOMO, 손절 미설정, 복구 매매"        # 기존 태그 유지 + 중복 없이 추가
    assert row[19] == "평온"                                 # 감정은 마지막 거래 기준
    assert row[20] == "[01:10 ZEC 숏 −$1,236] 추격\n[02:00 BTC 롱 +$50] 계획대로"
    assert row[3] == "" and row[13] == ""                    # 다른 칸은 건드리지 않음
    assert not out[5]["ok"] and "날짜 행" in out[5]["error"]


def test_daily_payload_and_tags(tmp_path):
    st = Store(tmp_path / "j.db")
    d = date(2026, 11, 26)
    a, _ = jn.local_day_bounds(d)
    st.add_fills([f(a + 1000, "buy", 1, 0), f(a + 60_000, "sell", 1, 1, pnl=-80, action="close"),
                  f(a + 120_000, "buy", 1, 0), f(a + 180_000, "sell", 1, 1, pnl=50, action="close")])
    st.add_violation(Violation(rule="no_stop", message="", penalty=15, ts=a + 30_000, exchange="bybit", coin="ZEC",
                               dedupe_key="1"))
    st.add_violation(Violation(rule="liquidation", message="", penalty=30, ts=a + 40_000, exchange="bybit",
                               coin="ZEC", dedupe_key="2"))
    p = jn.daily_payload(st, d, 1234.567)
    assert p == {"action": "daily", "date": "2026-11-26", "tags": ["손절 미설정"], "profit": 50.0, "loss": 80.0,
                 "equity": 1234.57}
    assert "profit" not in jn.daily_payload(st, date(2026, 11, 27), None)


def test_trade_payload_and_config(tmp_path):
    t = Trade("hyperliquid", "xyz:NVDA", "short", 0, int(datetime(2026, 11, 26, 9, 5).timestamp() * 1000), -1236.4,
              {"revenge", "liquidation"})
    p = jn.trade_payload(t, "초조", "추격 진입", date(2026, 11, 26))
    assert p["memo"] == "[09:05 NVDA 숏 −$1,236] 추격 진입" and p["tags"] == ["복구 매매"] and p["emotion"] == "초조"
    assert jn.trade_payload(t, "아무거나", "", date(2026, 11, 26))["emotion"] == ""
    c = jn.JournalConfig(tmp_path / "journal.json")
    tok = c.token
    assert not c.ready
    c.url, c.enabled = "https://script.google.com/macros/s/x/exec", True
    c.save()
    c2 = jn.JournalConfig(tmp_path / "journal.json")
    assert c2.ready and c2.token == tok and tok in jn.script_for(tok)


def test_post_error_messages(monkeypatch):
    class R:
        def __init__(self, text, url="https://script.googleusercontent.com/x", code=200):
            self.text, self.url, self.status_code = text, url, code

        def json(self):
            return json.loads(self.text)
    pages = {"a": R("<html><title>오류</title><body>다음 스크립트 함수(doPost)를 찾을 수 없습니다.</body></html>"),
             "b": R("<html>login</html>", url="https://accounts.google.com/ServiceLogin"),
             "c": R("<html><body><style>x{}</style>알 수 없는 오류</body></html>"),
             "d": R('{"ok": true, "row": 3}')}
    monkeypatch.setattr(jn.requests, "post", lambda url, **k: pages[url])
    assert "새 버전" in jn.post("a", {})["error"]
    assert "모든 사용자" in jn.post("b", {})["error"]
    assert jn.post("c", {})["error"] == "구글 응답: 알 수 없는 오류"
    assert jn.post("d", {}) == {"ok": True, "row": 3}


@pytest.mark.skipif(shutil.which("node") is None, reason="node 없음")
def test_standalone_script_uses_sheet_url(tmp_path):
    """시트와 따로 만든 스크립트(getActive 가 null)는 시트 주소로 연다."""
    sid = "1AbCdEf_-GhIjK-LmNoPqRsTuVwXyZ-0123456789"          # 밑줄·하이픈 포함 (실제 주소 형태)
    url = f"https://docs.google.com/spreadsheets/d/{sid}/edit?gid=0#gid=0"
    assert jn.sheet_id(url) == sid and jn.sheet_id(sid) == sid and jn.sheet_id("x'; alert(1)//") == ""
    code = jn.script_for("T", url)
    assert f"const SHEET_ID = '{sid}';" in code
    harness = HARNESS.replace("getActive: () => ({", "getActive: () => null, openById: (u) => (u === '%s') && ({" % sid)
    (tmp_path / "s.gs").write_text(code, encoding="utf-8")
    (tmp_path / "h.js").write_text(harness, encoding="utf-8")
    rows = [["기본"], HEAD5, ["", "D:2026-11-26"] + [""] * 19]
    (tmp_path / "rows.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "req.json").write_text(json.dumps([{"action": "ping", "token": "T"},
                                                   {"action": "daily", "token": "T", "date": "2026-11-26", "equity": 5}]),
                                       encoding="utf-8")
    r = subprocess.run(["node", str(tmp_path / "h.js"), str(tmp_path / "s.gs"), str(tmp_path / "rows.json"),
                        str(tmp_path / "req.json")], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    res = json.loads(r.stdout)
    assert res["out"][0]["ok"] and res["out"][1] == {"ok": True, "row": 3} and res["rows"][2][4] == 5
    # 시트 주소 없이 따로 만든 스크립트 → 안내 메시지
    (tmp_path / "s.gs").write_text(jn.script_for("T", ""), encoding="utf-8")
    r = subprocess.run(["node", str(tmp_path / "h.js"), str(tmp_path / "s.gs"), str(tmp_path / "rows.json"),
                        str(tmp_path / "req.json")], capture_output=True, text=True, timeout=30)
    assert "시트 주소" in json.loads(r.stdout)["out"][0]["error"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node 없음")
def test_journal_tab_found_by_header_when_named_differently(tmp_path):
    import os
    (tmp_path / "s.gs").write_text(jn.script_for("T"), encoding="utf-8")
    (tmp_path / "h.js").write_text(HARNESS, encoding="utf-8")
    rows = [["기본"], HEAD5, ["", "D:2026-11-26"] + [""] * 19]
    (tmp_path / "rows.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "req.json").write_text(json.dumps([{"action": "ping", "token": "T"},
                                                   {"action": "daily", "token": "T", "date": "2026-11-26", "equity": 7}]),
                                       encoding="utf-8")
    args = ["node", str(tmp_path / "h.js"), str(tmp_path / "s.gs"), str(tmp_path / "rows.json"), str(tmp_path / "req.json")]
    r = subprocess.run(args, capture_output=True, text=True, timeout=30, env={**os.environ, "TAB": "매매일지"})
    res = json.loads(r.stdout)
    assert res["out"][0]["ok"] and res["out"][0]["sheet"] == "선물 일지 › 매매일지"
    assert res["out"][1] == {"ok": True, "row": 3} and res["rows"][2][4] == 7
    r = subprocess.run(args, capture_output=True, text=True, timeout=30, env={**os.environ, "NOJOURNAL": "1", "TAB": "매매일지"})
    err = json.loads(r.stdout)["out"][0]["error"]
    assert "이 파일의 탭: 시트1" in err


@pytest.mark.skipif(shutil.which("node") is None, reason="node 없음")
def test_sheet_without_timezone(tmp_path):
    """엑셀에서 올린 시트처럼 시트 시간대가 비어 있어도 날짜 줄을 찾음."""
    import os
    (tmp_path / "s.gs").write_text(jn.script_for("T"), encoding="utf-8")
    (tmp_path / "h.js").write_text(HARNESS, encoding="utf-8")
    rows = [["기본"], HEAD5, ["", "D:2026-11-26"] + [""] * 19]
    (tmp_path / "rows.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "req.json").write_text(json.dumps([{"action": "daily", "token": "T", "date": "2026-11-26", "equity": 9}]),
                                       encoding="utf-8")
    r = subprocess.run(["node", str(tmp_path / "h.js"), str(tmp_path / "s.gs"), str(tmp_path / "rows.json"),
                        str(tmp_path / "req.json")], capture_output=True, text=True, timeout=30,
                       env={**os.environ, "TZNULL": "1"})
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["out"][0] == {"ok": True, "row": 3}
