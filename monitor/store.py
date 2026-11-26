"""SQLite 기록 저장소: 체결, 위반, 매매 계획, 내부 상태."""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from monitor.models import Fill

SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
    exchange TEXT NOT NULL, tid TEXT NOT NULL, coin TEXT NOT NULL, oid TEXT,
    ts INTEGER NOT NULL, side TEXT, px REAL, sz REAL, action TEXT,
    position_side TEXT, start_position REAL, closed_pnl REAL, fee REAL,
    is_liquidation INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (exchange, tid)
);
CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(ts);

CREATE TABLE IF NOT EXISTS violations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL, rule TEXT NOT NULL, exchange TEXT, coin TEXT,
    penalty INTEGER NOT NULL, message TEXT NOT NULL,
    dedupe_key TEXT UNIQUE,
    source TEXT NOT NULL DEFAULT 'live'
);
CREATE INDEX IF NOT EXISTS idx_viol_ts ON violations(ts);

CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts INTEGER NOT NULL, exchange TEXT NOT NULL, coin TEXT NOT NULL,
    side TEXT NOT NULL, entries TEXT NOT NULL, stop_px REAL, targets TEXT NOT NULL,
    note TEXT, status TEXT NOT NULL DEFAULT 'active', closed_ts INTEGER
);

CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


@dataclass
class Violation:
    rule: str
    message: str
    penalty: int
    ts: int
    exchange: Optional[str] = None
    coin: Optional[str] = None
    dedupe_key: Optional[str] = None   # 같은 키는 한 번만 기록됨
    id: Optional[int] = None
    source: str = "live"               # live = 실시간 감지, retro = 과거 체결로 소급 계산


@dataclass
class Plan:
    exchange: str
    coin: str
    side: str                          # "long" | "short"
    entries: list[float]
    stop_px: Optional[float]
    targets: list[float] = field(default_factory=list)
    note: str = ""
    created_ts: int = 0
    status: str = "active"
    closed_ts: Optional[int] = None
    id: Optional[int] = None


def _now_ms() -> int:
    return int(time.time() * 1000)


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        # 감시 스레드와 앱 화면이 같은 DB 를 동시에 읽으므로 WAL + 대기 시간
        self.db = sqlite3.connect(self.path, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self) -> None:
        """이전 버전 DB 에 새 컬럼 추가 (업데이트해도 기존 기록 유지)."""
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(fills)")}
        if "is_liquidation" not in cols:
            self.db.execute("ALTER TABLE fills ADD COLUMN is_liquidation INTEGER NOT NULL DEFAULT 0")
        vcols = {r[1] for r in self.db.execute("PRAGMA table_info(violations)")}
        if "source" not in vcols:
            self.db.execute("ALTER TABLE violations ADD COLUMN source TEXT NOT NULL DEFAULT 'live'")

    def close(self) -> None:
        self.db.close()

    # ---------------------------------------------------------------- 체결
    def add_fills(self, fills: list[Fill]) -> list[Fill]:
        """새 체결 저장. 이미 있는 체결(같은 거래소·tid)은 무시. 새로 들어간 체결만 반환."""
        new: list[Fill] = []
        for f in fills:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO fills (exchange, tid, coin, oid, ts, side, px, sz, action,"
                " position_side, start_position, closed_pnl, fee, is_liquidation)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f.exchange, f.tid, f.coin, f.oid, f.ts, f.side, f.px, f.sz, f.action,
                 f.position_side, f.start_position, f.closed_pnl, f.fee, int(f.is_liquidation)))
            if cur.rowcount == 1:
                new.append(f)
        self.db.commit()
        return new

    def fills_between(self, start_ms: int, end_ms: Optional[int] = None) -> list[Fill]:
        end = end_ms if end_ms is not None else 2**62
        rows = self.db.execute(
            "SELECT * FROM fills WHERE ts >= ? AND ts < ? ORDER BY ts, tid", (start_ms, end))
        out = []
        for r in rows:
            d = {k: r[k] for k in r.keys()}
            d["is_liquidation"] = bool(d.get("is_liquidation"))
            out.append(Fill(**d))
        return out

    def last_fill_ts(self, exchange: str) -> Optional[int]:
        r = self.db.execute("SELECT MAX(ts) FROM fills WHERE exchange = ?", (exchange,)).fetchone()
        return r[0] if r and r[0] is not None else None

    # ---------------------------------------------------------------- 위반
    def add_violation(self, v: Violation) -> bool:
        """위반 저장. dedupe_key 가 이미 있으면 저장하지 않고 False."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO violations (ts, rule, exchange, coin, penalty, message, dedupe_key, source)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (v.ts, v.rule, v.exchange, v.coin, v.penalty, v.message, v.dedupe_key, v.source))
        self.db.commit()
        if cur.rowcount == 1:
            v.id = cur.lastrowid
            return True
        return False

    def violations_between(self, start_ms: int, end_ms: Optional[int] = None) -> list[Violation]:
        end = end_ms if end_ms is not None else 2**62
        rows = self.db.execute(
            "SELECT * FROM violations WHERE ts >= ? AND ts < ? ORDER BY ts", (start_ms, end))
        return [Violation(**{k: r[k] for k in r.keys()}) for r in rows]

    def has_violation(self, dedupe_key: str) -> bool:
        return self.db.execute("SELECT 1 FROM violations WHERE dedupe_key=?", (dedupe_key,)).fetchone() is not None

    def violation(self, vid: int) -> Optional[Violation]:
        r = self.db.execute("SELECT * FROM violations WHERE id=?", (vid,)).fetchone()
        return Violation(**{k: r[k] for k in r.keys()}) if r else None

    def coins(self, exchange: str) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT DISTINCT coin FROM fills WHERE exchange=?", (exchange,))]

    def cleanup_once(self) -> int:
        """버그로 잘못 기록된 경고 정리 (한 번만). 일 손실 한도 기준이 $0 으로 잡혀 생긴 경고."""
        if self.get_kv("cleanup_daily0_v1"):
            return 0
        cur = self.db.execute("DELETE FROM violations WHERE rule='daily_loss_limit' AND message LIKE '%한도($0)%'")
        self.set_kv("cleanup_daily0_v1", True)
        return cur.rowcount

    def first_fill_ts(self) -> dict[str, int]:
        """거래소별 가장 오래된 체결 시각 (기록이 어디서부터인지 표시용)."""
        rows = self.db.execute("SELECT exchange, MIN(ts) FROM fills GROUP BY exchange")
        return {r[0]: r[1] for r in rows}

    # ---------------------------------------------------------------- 계획
    def add_plan(self, p: Plan) -> int:
        p.created_ts = p.created_ts or _now_ms()
        cur = self.db.execute(
            "INSERT INTO plans (created_ts, exchange, coin, side, entries, stop_px, targets, note, status)"
            " VALUES (?,?,?,?,?,?,?,?, 'active')",
            (p.created_ts, p.exchange, p.coin, p.side, json.dumps(p.entries), p.stop_px,
             json.dumps(p.targets), p.note))
        self.db.commit()
        p.id = cur.lastrowid
        return p.id

    def _row_to_plan(self, r: sqlite3.Row) -> Plan:
        d = {k: r[k] for k in r.keys()}
        d["entries"] = json.loads(d["entries"])
        d["targets"] = json.loads(d["targets"])
        return Plan(**d)

    def plans(self, status: Optional[str] = "active") -> list[Plan]:
        if status is None:
            rows = self.db.execute("SELECT * FROM plans ORDER BY id")
        else:
            rows = self.db.execute("SELECT * FROM plans WHERE status = ? ORDER BY id", (status,))
        return [self._row_to_plan(r) for r in rows]

    def active_plan_for(self, exchange: str, coin: str, side: str) -> Optional[Plan]:
        """해당 포지션에 걸린 가장 최근의 활성 계획."""
        r = self.db.execute(
            "SELECT * FROM plans WHERE status='active' AND exchange=? AND UPPER(coin)=UPPER(?) AND side=?"
            " ORDER BY id DESC LIMIT 1", (exchange, coin, side)).fetchone()
        return self._row_to_plan(r) if r else None

    def close_plan(self, plan_id: int, status: str = "done") -> bool:
        cur = self.db.execute(
            "UPDATE plans SET status=?, closed_ts=? WHERE id=? AND status='active'",
            (status, _now_ms(), plan_id))
        self.db.commit()
        return cur.rowcount == 1

    # ---------------------------------------------------------- 내부 상태
    def get_kv(self, key: str, default: Any = None) -> Any:
        r = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_kv(self, key: str, value: Any) -> None:
        self.db.execute("INSERT INTO kv (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, json.dumps(value)))
        self.db.commit()
