"""자동 업데이트: 각자의 설정·키·기록·그림은 그대로, 코드만 교체되는지."""
from __future__ import annotations

import hashlib
import io
import zipfile

from monitor import updater as up


def make_zip(files: dict[str, bytes]) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
    return b.getvalue()


class Resp:
    def __init__(self, content=b"", js=None, code=200):
        self.content, self._js, self.status_code = content, js, code

    def json(self):
        return self._js


class Sess:
    def __init__(self, routes):
        self.routes = routes

    def get(self, url, **k):
        return self.routes[url]


def setup_root(tmp_path):
    root = tmp_path / "app"
    (root / "monitor").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "alerts").mkdir()
    (root / "VERSION").write_text("1.0.0")
    (root / "app.py").write_text("old app")
    (root / "monitor" / "rules.py").write_text("old rules")
    (root / "config.yaml").write_text("mine: 20x")
    (root / ".env").write_text("KEY=secret")
    (root / "requirements.txt").write_text("requests\n")
    (root / "alerts" / "fomo.png").write_bytes(b"my snake")
    (root / "data" / "safetynet.db").write_bytes(b"history")
    return root


def test_version_compare_and_paths():
    assert up.parse_ver("v1.2.10") > up.parse_ver("1.2.9") and up.parse_ver("1.0") == (1, 0, 0)
    for bad in ["../x.py", "/etc/passwd", "C:\\x", "config.yaml", ".env", "data/safetynet.db", "alerts/fomo.png",
                "monitor/__pycache__/a.pyc", "random.exe"]:
        assert not up.allowed(bad), bad
    for ok in ["app.py", "monitor/rules.py", "ui/index.html", "presets/other/config.yaml", "alerts/README.txt"]:
        assert up.allowed(ok), ok


def test_check(tmp_path):
    s = Sess({"M": Resp(js={"version": "1.1.0", "url": "Z", "sha256": "AB", "notes": "본전 알림"})})
    c = up.check("M", "1.0.0", s)
    assert c["available"] and c["latest"] == "1.1.0" and c["sha256"] == "ab"
    assert not up.check("M", "1.1.0", s)["available"]
    assert not up.check("", "1.0.0", s)["ok"]
    assert not up.check("M", "1.0.0", Sess({"M": Resp(js={"version": "2"})}))["ok"]


def test_apply_replaces_code_only(tmp_path):
    root = setup_root(tmp_path)
    z = make_zip({"VERSION": b"1.1.0", "app.py": b"new app", "monitor/rules.py": b"new rules",
                  "monitor/new.py": b"added", "config.yaml": b"OVERWRITE!", ".env": b"STEAL",
                  "data/safetynet.db": b"wipe", "alerts/fomo.png": b"other", "../evil.py": b"x",
                  "requirements.txt": b"requests\n"})
    info = {"url": "Z", "sha256": hashlib.sha256(z).hexdigest(), "latest": "1.1.0"}
    pip_calls = []
    r = up.apply(info, root, root / "data", Sess({"Z": Resp(content=z)}), pip=lambda p: pip_calls.append(p) or (True, ""))
    assert r["ok"] and r["version"] == "1.1.0"
    assert (root / "app.py").read_text() == "new app" and (root / "monitor" / "new.py").read_text() == "added"
    assert (root / "config.yaml").read_text() == "mine: 20x" and (root / ".env").read_text() == "KEY=secret"
    assert (root / "data" / "safetynet.db").read_bytes() == b"history"
    assert (root / "alerts" / "fomo.png").read_bytes() == b"my snake"
    assert not (tmp_path / "evil.py").exists()
    assert (root / "data" / "backup" / "1.0.0" / "app.py").read_text() == "old app"
    assert pip_calls == []                                   # requirements 그대로 → 설치 안 함
    assert set(r["skipped"]) >= {"config.yaml", ".env", "data/safetynet.db", "alerts/fomo.png", "../evil.py"}


def test_apply_runs_pip_when_requirements_change(tmp_path):
    root = setup_root(tmp_path)
    z = make_zip({"VERSION": b"1.2.0", "app.py": b"a", "requirements.txt": b"requests\nnewlib\n"})
    info = {"url": "Z", "sha256": hashlib.sha256(z).hexdigest(), "latest": "1.2.0"}
    r = up.apply(info, root, root / "data", Sess({"Z": Resp(content=z)}), pip=lambda p: (True, "ok"))
    assert r["deps_installed"] is True


def test_apply_rejects_bad_hash_and_bad_zip(tmp_path):
    root = setup_root(tmp_path)
    z = make_zip({"VERSION": b"9", "app.py": b"evil"})
    r = up.apply({"url": "Z", "sha256": "0" * 64}, root, root / "data", Sess({"Z": Resp(content=z)}))
    assert not r["ok"] and "sha256" in r["error"] and (root / "app.py").read_text() == "old app"
    junk = b"not a zip"
    r = up.apply({"url": "Z", "sha256": hashlib.sha256(junk).hexdigest()}, root, root / "data", Sess({"Z": Resp(content=junk)}))
    assert not r["ok"]
    nov = make_zip({"app.py": b"x"})
    r = up.apply({"url": "Z", "sha256": hashlib.sha256(nov).hexdigest()}, root, root / "data", Sess({"Z": Resp(content=nov)}))
    assert not r["ok"] and (root / "app.py").read_text() == "old app"


def test_apply_rolls_back_on_failure(tmp_path, monkeypatch):
    root = setup_root(tmp_path)
    z = make_zip({"VERSION": b"1.1.0", "app.py": b"new app", "monitor/rules.py": b"new rules"})
    info = {"url": "Z", "sha256": hashlib.sha256(z).hexdigest()}
    real = up.Path.write_bytes
    n = {"c": 0}

    def flaky(self, data):
        n["c"] += 1
        if n["c"] == 3:
            raise OSError("disk full")
        return real(self, data)
    monkeypatch.setattr(up.Path, "write_bytes", flaky)
    r = up.apply(info, root, root / "data", Sess({"Z": Resp(content=z)}))
    monkeypatch.setattr(up.Path, "write_bytes", real)
    assert not r["ok"] and "되돌림" in r["error"]
    assert (root / "app.py").read_text() == "old app" and (root / "VERSION").read_text() == "1.0.0"
    assert (root / "monitor" / "rules.py").read_text() == "old rules"
