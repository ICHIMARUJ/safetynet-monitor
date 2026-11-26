"""계획 검증·저장소·실행 루프 통합 테스트."""
from __future__ import annotations

import sys

import pytest

import plan as plan_cli
import run as run_mod
from monitor.defaults import DEFAULTS, deep_merge
from monitor.store import Plan, Store
from tests.test_hyperliquid import FakeSession, WALLET
from tests.test_rules import fill


# ------------------------------------------------------------- 계획 검증
@pytest.mark.parametrize("p,ok", [
    (Plan("bybit", "BTC", "long", [100, 95], 90, [110]), True),
    (Plan("bybit", "BTC", "long", [100, 95], 96, [110]), False),   # 손절이 진입가 사이
    (Plan("bybit", "BTC", "long", [100], 90, [99]), False),        # 목표가가 진입가 아래
    (Plan("bybit", "BTC", "short", [100, 105], 110, [90]), True),
    (Plan("bybit", "BTC", "short", [100, 105], 104, [90]), False),
    (Plan("bybit", "BTC", "short", [100], 110, [101]), False),
    (Plan("bybit", "BTC", "long", [], 90), False),
])
def test_plan_validate(p, ok):
    assert (plan_cli.validate(p) == []) is ok


def test_plan_cli_add_replace_close(tmp_path, monkeypatch, capsys):
    cfg = deep_merge(DEFAULTS, {"data_dir": str(tmp_path)})
    monkeypatch.setattr(plan_cli, "load_config", lambda: cfg)
    for argv in (["add", "hyperliquid", "btc", "long", "--entries", "95000,93000", "--stop", "91000"],
                 ["add", "hyperliquid", "BTC", "long", "--entries", "94000", "--stop", "90000"]):
        monkeypatch.setattr(sys, "argv", ["plan.py", *argv])
        assert plan_cli.main() == 0
    store = Store(tmp_path / "safetynet.db")
    active = store.plans("active")
    assert [(p.id, p.coin, p.entries) for p in active] == [(2, "BTC", [94000.0])]
    assert store.plans(None)[0].status == "replaced"
    store.close()
    monkeypatch.setattr(sys, "argv", ["plan.py", "close", "2"])
    assert plan_cli.main() == 0
    assert "기존 계획 #1 는 새 계획으로 교체됨" in capsys.readouterr().out


def test_store_returns_only_new_fills(tmp_path):
    store = Store(tmp_path / "s.db")
    a, b = fill(), fill()
    assert len(store.add_fills([a, b])) == 2
    assert store.add_fills([a, b]) == []
    store.close()


# ------------------------------------------------------------- 실행 루프
def test_run_once_end_to_end(tmp_path, monkeypatch, capsys):
    """샘플 Hyperliquid 계좌로 run.py --once 를 돌려 ETH 레버리지·SL 위반을 감지하는지 확인."""
    cfg = deep_merge(DEFAULTS, {"data_dir": str(tmp_path),
                                "exchanges": {"hyperliquid": {"enabled": True}},
                                "rules": {"leverage": {"per_coin": {"BTC": 20}},
                                          "stop_loss_grace_min": 0}})
    cfg["secrets"] = {"hl_wallet": WALLET}
    monkeypatch.setattr(run_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(run_mod.HyperliquidClient, "__init__",
                        lambda self, wallet, dexes=None: (
                            setattr(self, "wallet", wallet), setattr(self, "dexes", [""]),
                            setattr(self, "session", FakeSession()), setattr(self, "timeout", 5))[0])
    monkeypatch.setattr(sys, "argv", ["run.py", "--once"])
    assert run_mod.main() == 0
    out = capsys.readouterr().out
    assert "ETH SHORT 실효 레버리지 5.2배" in out
    assert "BTC LONG 실효 레버리지" not in out          # BTC 9.8배 < 20배
    assert "오늘 규율 점수" in out


def test_run_fails_cleanly_without_wallet(tmp_path, monkeypatch, capsys):
    cfg = deep_merge(DEFAULTS, {"data_dir": str(tmp_path),
                                "exchanges": {"hyperliquid": {"enabled": True}}})
    cfg["secrets"] = {"hl_wallet": ""}
    monkeypatch.setattr(run_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(sys, "argv", ["run.py", "--once"])
    assert run_mod.main() == 1
    assert "HL_WALLET" in capsys.readouterr().out


def test_log_option_writes_utf8(tmp_path, monkeypatch, capsys):
    cfg = deep_merge(DEFAULTS, {"data_dir": str(tmp_path),
                                "exchanges": {"hyperliquid": {"enabled": True}}})
    cfg["secrets"] = {"hl_wallet": ""}
    monkeypatch.setattr(run_mod, "load_config", lambda: cfg)
    log = tmp_path / "out.txt"
    monkeypatch.setattr(sys, "argv", ["run.py", "--once", "--log", str(log)])
    monkeypatch.setattr(sys, "stdout", sys.stdout)   # 테스트 후 원래 stdout 복구
    assert run_mod.main() == 1
    assert "설정 오류" in log.read_text(encoding="utf-8")
