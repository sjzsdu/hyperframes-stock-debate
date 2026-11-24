"""Keep publication state isolated from the developer's HOME and other tests."""
import pytest


@pytest.fixture(autouse=True)
def isolated_state_directory(tmp_path, monkeypatch):
    monkeypatch.setenv('STOCKTALK_STATE_DIR', str(tmp_path / 'state'))
