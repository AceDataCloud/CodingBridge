from importlib.metadata import version

import coding_bridge


def test_runtime_version_matches_distribution_metadata():
    assert coding_bridge.__version__ == version("coding-bridge")
