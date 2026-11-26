"""구글 시트 매매 일지 자동 기록.

구글 시트에 붙인 Apps Script 웹앱으로 값을 보낸다 (구글 계정 정보·API 키를 앱에 두지 않음).
일지 구조: '일지' 시트, 하루 한 줄. 머리글 이름으로 열을 찾으므로 열 순서가 조금 달라도 동작.
  당일 예산 · 총이익 · 총손실(양수) · 실수 태그 · 진입 시 감정 · 메모
날짜는 이 PC 의 현지 날짜 기준 (시트 날짜와 맞추기 위함).
"""
from __future__ import annotations

import json
import re
import secrets
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Any, Optional

import requests

from monitor.mistakes import KINDS, kind_of
from monitor.store import Store
from monitor.trades import Trade, build_trades, link_violations

EMOTIONS = ["평온", "확신", "초조", "불안", "욕심", "복수심", "지루함", "과신", "피로"]
JOURNAL_TAGS = {k: d["label"] for k, d in KINDS.items() if k != "liquidation"}   # 일지 실수 태그와 같은 이름


# ------------------------------------------------------------ 설정 파일 (data/journal.json)
class JournalConfig:
    def __init__(self, path: Path):
        self.path = path
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        self.url: str = d.get("url", "")
        self.sheet_url: str = d.get("sheet_url", "")          # 구글 시트 주소 (시트와 따로 만든 스크립트용)
        self.token: str = d.get("token") or secrets.token_urlsafe(18)
        self.enabled: bool = bool(d.get("enabled", False))
        self.prompt: bool = bool(d.get("prompt", True))      # 포지션 정리 때 감정·메모 입력창
        if not path.exists():
            self.save()

    def save(self) -> None:
        self.path.write_text(json.dumps({"url": self.url, "token": self.token, "enabled": self.enabled,
                                         "prompt": self.prompt, "sheet_url": self.sheet_url},
                                        ensure_ascii=False), encoding="utf-8")

    @property
    def ready(self) -> bool:
        return self.enabled and self.url.startswith("https://script.google.com/")


def post(url: str, payload: dict[str, Any], timeout: float = 20.0) -> dict[str, Any]:
    """웹앱 호출. 구글은 302 로 결과 주소를 돌려주며 requests 가 따라가 결과(JSON)를 받는다."""
    try:
        r = requests.post(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          headers={"Content-Type": "text/plain;charset=utf-8"}, timeout=timeout)
    except requests.RequestException as e:
        return {"ok": False, "error": f"접속 실패: {e}"}
    try:
        return r.json()
    except ValueError:
        if "accounts.google" in r.url:
            return {"ok": False, "error": "구글 로그인 페이지가 돌아옴 — 배포 설정의 액세스 권한을 '모든 사용자'로"}
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style).*?</\1>", " ", r.text))).strip()
        if "doPost" in text:
            return {"ok": False, "error": "배포된 버전에 코드가 없음 — Apps Script 에서 저장 후 배포 → 배포 관리 → 수정(연필) → "
                                          "버전: 새 버전 → 배포 (URL 은 그대로)"}
        return {"ok": False, "error": f"구글 응답: {text[:160] or f'HTTP {r.status_code}'}"}


# ------------------------------------------------------------ 기록할 값 계산
def local_day_bounds(d: date) -> tuple[int, int]:
    start = datetime.combine(d, dtime.min).astimezone()
    end = datetime.combine(d, dtime.max).astimezone()
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000) + 1


def day_trades(store: Store, d: date) -> list[Trade]:
    """그날(현지) 끝난 거래 (경고 연결 포함)."""
    a, b = local_day_bounds(d)
    fills = store.fills_between(a - 30 * 86_400_000, b)
    trades = build_trades(fills)
    link_violations(trades, store.violations_between(a - 30 * 86_400_000, b))
    return [t for t in trades if t.closed and a <= t.end_ts < b]


def day_tags(store: Store, d: date) -> list[str]:
    a, b = local_day_bounds(d)
    kinds = {kind_of(v.rule) for v in store.violations_between(a, b)}
    return [JOURNAL_TAGS[k] for k in JOURNAL_TAGS if k in kinds]


def daily_payload(store: Store, d: date, equity: Optional[float]) -> dict[str, Any]:
    ts = day_trades(store, d)
    profit = round(sum(t.pnl for t in ts if t.pnl > 0), 2)
    loss = round(-sum(t.pnl for t in ts if t.pnl < 0), 2)
    p: dict[str, Any] = {"action": "daily", "date": d.isoformat(), "tags": day_tags(store, d)}
    if ts:
        p.update(profit=profit, loss=loss)
    if equity is not None and equity > 0:
        p["equity"] = round(equity, 2)
    return p


def trade_payload(t: Trade, emotion: str, memo: str, d: date) -> dict[str, Any]:
    side = "롱" if t.side == "long" else "숏"
    pnl = f"{'+' if t.pnl >= 0 else '−'}${abs(t.pnl):,.0f}"
    clock = datetime.fromtimestamp(t.end_ts / 1000).strftime("%H:%M")
    line = f"[{clock} {t.coin.split(':')[-1]} {side} {pnl}] {memo}".strip()
    return {"action": "trade", "date": d.isoformat(), "emotion": emotion if emotion in EMOTIONS else "",
            "memo": line, "tags": [JOURNAL_TAGS[k] for k in JOURNAL_TAGS if k in t.kinds]}


# ------------------------------------------------------------ 구글 시트에 붙일 스크립트
SCRIPT = r"""/**
 * Safety Net → 매매 일지 자동 기록 (Apps Script 웹앱)
 * 붙이는 곳: 구글 시트 → 확장 프로그램 → Apps Script → 이 내용으로 전체 교체 → 저장
 * 배포: 배포 → 새 배포 → 유형 '웹 앱' → 실행: 나 / 액세스 권한: 모든 사용자 → 배포 → 웹 앱 URL 복사
 * 코드를 바꾼 뒤에는: 저장 → 배포 → 배포 관리 → 수정(연필) → 버전: 새 버전 → 배포 (URL 은 그대로)
 * 확인: 웹 앱 URL 을 브라우저로 열면 "동작 중" 이 보여야 함
 * 이 스크립트는 아래 TOKEN 을 가진 요청만 받는다 (Safety Net 앱이 만든 값).
 */
const TOKEN = '__TOKEN__';
const SHEET_ID = '__SHEET__';    // 구글 시트 ID (시트 메뉴에서 만든 스크립트면 비어 있어도 됨)
const SHEET = '일지';

function book() {
  const a = SpreadsheetApp.getActive();
  if (a) return a;
  return SHEET_ID ? SpreadsheetApp.openById(SHEET_ID) : null;
}
const COLS = { date: '날짜', equity: '당일 예산', profit: '총이익', loss: '총손실', tags: '실수 태그', emotion: '감정', memo: '메모' };

function doGet() {
  const ss = book();
  return out({ ok: true, msg: 'Safety Net 일지 스크립트 동작 중', sheet: ss ? ss.getName() : '시트 연결 안 됨 — 앱에 구글 시트 주소를 넣고 코드를 다시 복사' });
}

function doPost(e) {
  try {
    const d = JSON.parse(e.postData.contents);
    if (d.token !== TOKEN) return out({ ok: false, error: '토큰 불일치 — 앱 설정의 스크립트를 다시 붙여넣기' });
    const ss = book();
    if (!ss) return out({ ok: false, error: '시트를 찾을 수 없음 — 앱에 구글 시트 주소를 넣고 코드를 다시 복사해 붙여넣기' });
    const found = journalTab(ss);
    if (!found) return out({ ok: false, error: "일지 탭을 찾을 수 없음 ('날짜'·'당일 예산' 머리글이 있는 탭) — 이 파일의 탭: "
                                               + ss.getSheets().map(x => x.getName()).join(', ') + ' / 파일: ' + ss.getName() });
    const sh = found.sheet, h = found.head;
    if (d.action === 'ping') return out({ ok: true, sheet: ss.getName() + ' › ' + sh.getName(), columns: Object.keys(h.cols) });
    const row = findRow(sh, h, d.date);
    if (!row) return out({ ok: false, error: d.date + ' 날짜 행이 없음 — 일지에 날짜를 이어서 입력해 두기' });
    const set = (k, v) => { if (h.cols[k] && v !== undefined && v !== null && v !== '') sh.getRange(row, h.cols[k]).setValue(v); };
    if (d.action === 'daily') { set('equity', d.equity); set('profit', d.profit); set('loss', d.loss); }
    if (d.action === 'trade') {
      set('emotion', d.emotion);
      if (h.cols.memo && d.memo) {
        const c = sh.getRange(row, h.cols.memo), old = String(c.getValue() || '');
        if (old.indexOf(d.memo) < 0) c.setValue(old ? old + '\n' + d.memo : d.memo);
      }
    }
    if (h.cols.tags && d.tags && d.tags.length) {
      const c = sh.getRange(row, h.cols.tags);
      const cur = String(c.getValue() || '').split(',').map(s => s.trim()).filter(String);
      d.tags.forEach(t => { if (cur.indexOf(t) < 0) cur.push(t); });
      c.setValue(cur.join(', '));
    }
    return out({ ok: true, row: row });
  } catch (err) {
    return out({ ok: false, error: String(err) });
  }
}

// '일지' 탭이 있으면 그 탭, 없으면 '날짜'와 '당일 예산' 머리글이 있는 첫 탭
function journalTab(ss) {
  const named = ss.getSheetByName(SHEET);
  const tabs = (named ? [named] : []).concat(ss.getSheets().filter(x => x !== named));
  for (const sh of tabs) {
    if (sh.getLastRow() < 1 || sh.getLastColumn() < 1) continue;
    const h = header(sh);
    if (h && h.cols.equity) return { sheet: sh, head: h };
  }
  return null;
}

function header(sh) {
  const vals = sh.getRange(1, 1, Math.min(12, sh.getLastRow()), Math.min(60, sh.getLastColumn())).getValues();
  for (let r = 0; r < vals.length; r++) {
    if (vals[r].some(v => String(v).trim() === COLS.date)) {
      const cols = {};
      vals[r].forEach((v, i) => {
        const s = String(v).replace(/\s+/g, ' ');
        for (const k in COLS) if (!cols[k] && s.indexOf(COLS[k]) >= 0) cols[k] = i + 1;
      });
      return { row: r + 1, cols: cols };
    }
  }
  return null;
}

function findRow(sh, h, iso) {
  // 엑셀에서 올린 시트는 시간대가 비어 있을 수 있음 → 스크립트 시간대 → UTC 순으로 대체
  const tz = book().getSpreadsheetTimeZone() || Session.getScriptTimeZone() || 'Etc/UTC';
  const n = sh.getLastRow() - h.row;
  if (n <= 0) return 0;
  const vals = sh.getRange(h.row + 1, h.cols.date, n, 1).getValues();
  for (let i = 0; i < vals.length; i++) {
    const v = vals[i][0];
    const s = v instanceof Date ? Utilities.formatDate(v, tz, 'yyyy-MM-dd') : String(v).slice(0, 10);
    if (s === iso) return h.row + 1 + i;
  }
  return 0;
}

function out(o) {
  return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON);
}
"""


def sheet_id(sheet_url: str) -> str:
    """구글 시트 주소 → ID (docs.google.com/spreadsheets/d/<ID>/edit…). ID 만 넣어도 됨."""
    s = (sheet_url or "").strip()
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", s)
    if m:
        return m.group(1)
    return s if re.fullmatch(r"[A-Za-z0-9_-]{20,}", s) else ""


def script_for(token: str, sheet_url: str = "") -> str:
    return SCRIPT.replace("__TOKEN__", token).replace("__SHEET__", sheet_id(sheet_url))
