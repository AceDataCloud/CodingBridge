from pathlib import Path

from coding_bridge_agent.config import Settings


def test_ws_url_from_https():
    settings = Settings(bridge_url="https://coding-bridge.acedata.cloud")
    assert settings.ws_node_url == "wss://coding-bridge.acedata.cloud/ws/node"
    assert settings.pair_start_url == "https://coding-bridge.acedata.cloud/pair/start"
    assert settings.pair_poll_url == "https://coding-bridge.acedata.cloud/pair/poll"


def test_ws_url_from_http():
    settings = Settings(bridge_url="http://localhost:3000/")
    assert settings.ws_node_url == "ws://localhost:3000/ws/node"


def test_defaults_are_filled():
    settings = Settings()
    assert settings.node_name
    assert settings.default_cwd
    assert settings.credentials_path.name == "credentials.json"


def test_default_cwd_falls_back_to_home_inside_system_dir(monkeypatch, tmp_path):
    system_root = tmp_path / "Windows"
    system32 = system_root / "System32"
    system32.mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SYSTEMROOT", str(system_root))
    monkeypatch.setattr("os.getcwd", lambda: str(system32))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    settings = Settings()
    assert Path(settings.default_cwd) == home


def test_default_cwd_keeps_normal_dir(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("SYSTEMROOT", str(tmp_path / "Windows"))
    monkeypatch.setattr("os.getcwd", lambda: str(project))
    settings = Settings()
    assert Path(settings.default_cwd) == project.resolve()


def test_from_env(monkeypatch):
    monkeypatch.setenv("CODING_BRIDGE_URL", "https://bridge.example")
    monkeypatch.setenv("CODING_BRIDGE_PERMISSION_TIMEOUT", "12.5")
    settings = Settings.from_env()
    assert settings.bridge_url == "https://bridge.example"
    assert settings.permission_timeout == 12.5
