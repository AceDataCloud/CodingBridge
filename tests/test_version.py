from importlib.metadata import version

import pytest

import coding_bridge
from coding_bridge.cli import main


def test_runtime_version_matches_distribution_metadata():
    assert coding_bridge.__version__ == version("coding-bridge")


def test_version_flag_prints_installed_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"coding-bridge {coding_bridge.__version__}"


def test_status_reports_version(capsys, tmp_path):
    main(["status", "--config-dir", str(tmp_path)])
    assert f"Version    : {coding_bridge.__version__}" in capsys.readouterr().out
