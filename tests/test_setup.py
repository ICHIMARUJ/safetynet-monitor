"""첫 실행 설정: 키 보관·읽기 전용 확인·설정 로딩."""
from __future__ import annotations

import pytest

from monitor import secretstore
from monitor.config import load_config
from monitor.defaults import DEFAULTS, deep_merge
from service import Api


def test_secretstore_roundtrip_and_mask(tmp_path):
    secretstore.save(tmp_path, {"bybit_key": " K123456 ", "bybit_secret": "S", "hl_wallet": "", "junk": "x"})
    assert secretstore.load(tmp_path) == {"bybit_key": "K123456", "bybit_secret": "S"}
    assert secretstore.mask("K123456") == "K123••••••••" and secretstore.mask("") == ""
    (tmp_path / "secrets.bin").write_text("broken", encoding="utf-8")
    assert secretstore.load(tmp_path) == {}


def test_config_fills_missing_from_store_env_wins(tmp_path, monkeypatch):
    for k in ("HL_WALLET", "BYBIT_API_KEY", "BYBIT_API_SECRET"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / "config.yaml").write_text(f"data_dir: '{tmp_path.as_posix()}/d'\n", encoding="utf-8")
    (tmp_path / "d").mkdir()
    secretstore.save(tmp_path / "d", {"bybit_key": "STORE", "bybit_secret": "SS", "hl_wallet": "0xW"})
    (tmp_path / ".env").write_text("BYBIT_API_KEY=ENV\n", encoding="utf-8")
    cfg = load_config(tmp_path / "config.yaml", tmp_path / ".env")
    assert cfg["secrets"]["bybit_key"] == "ENV" and cfg["secrets"]["bybit_secret"] == "SS"
    assert cfg["secrets"]["hl_wallet"] == "0xW"


def test_missing_config_uses_defaults(tmp_path):
    cfg = load_config(tmp_path / "none.yaml", tmp_path / "none.env")
    assert cfg["rules"]["leverage"]["default_max"] == 5


class FakeBybit:
    read_only = True

    def __init__(self, key, secret):
        if key != "GOOD":
            from monitor.exchanges.bybit import BybitError
            raise BybitError("API 키가 올바르지 않음", 10003)

    def key_info(self):
        return {"read_only": FakeBybit.read_only, "withdraw": False, "permissions": {}}

    def snapshot(self):
        class S:
            equity = 700.0
        return S()


@pytest.fixture
def api(tmp_path, monkeypatch):
    import monitor.exchanges.bybit as bb
    monkeypatch.setattr(bb, "BybitClient", FakeBybit)
    cfg = deep_merge(DEFAULTS, {"data_dir": str(tmp_path), "exchanges": {"bybit": {"enabled": True}},
                                "secrets": {"bybit_key": "", "bybit_secret": "", "hl_wallet": ""}})
    a = Api(cfg)
    restarted = []
    a._hooks["restart_monitor"] = lambda: restarted.append(1)
    return a, restarted, tmp_path


def test_setup_requires_read_only_key(api):
    a, restarted, d = api
    assert a.setup_status()["need"] == ["bybit"]
    r = a.setup_save({"bybit_key": "BAD", "bybit_secret": "x"})
    assert not r["saved"] and "올바르지" in r["bybit"]["error"]
    FakeBybit.read_only = False
    r = a.setup_save({"bybit_key": "GOOD", "bybit_secret": "x"})
    assert not r["saved"] and "읽기 전용" in r["bybit"]["error"] and secretstore.load(d) == {}
    FakeBybit.read_only = True
    r = a.setup_save({"bybit_key": "GOOD", "bybit_secret": "x"})
    assert r["saved"] and r["bybit"]["equity"] == 700.0 and restarted == [1]
    assert secretstore.load(d) == {"bybit_key": "GOOD", "bybit_secret": "x"}
