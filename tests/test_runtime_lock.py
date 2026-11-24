"""State overrides keep publication locking usable with a read-only HOME."""
import os
from pathlib import Path

import pytest

from stocktalk.modules.runtime_lock import AccountBusy, account_lock


@pytest.mark.parametrize('variable', ['STOCKTALK_STATE_DIR', 'XDG_STATE_HOME'])
def test_lock_override_with_readonly_home(tmp_path, monkeypatch, variable):
    home = tmp_path / 'home'
    home.mkdir(mode=0o500)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('STOCKTALK_STATE_DIR', raising=False)
    monkeypatch.delenv('XDG_STATE_HOME', raising=False)
    monkeypatch.setenv(variable, str(tmp_path / 'state'))
    try:
        # Verify that this environment really rejects writes, rather than mocking permissions.
        assert not os.access(home, os.W_OK)
        with pytest.raises(PermissionError):
            (home / 'probe').touch()
        with account_lock('bilibili', 'fixture'):
            with pytest.raises(AccountBusy):
                with account_lock('bilibili', 'fixture'):
                    pytest.fail('same account acquired twice')
            with account_lock('bilibili', 'other'):
                pass
        with account_lock('bilibili', 'fixture'):
            pass
        root = tmp_path / 'state'
        if variable == 'XDG_STATE_HOME':
            root /= 'stocktalk'
        assert len(list((root / 'account-locks').iterdir())) == 2
        assert not list(home.iterdir())
    finally:
        home.chmod(0o700)


def test_explicit_state_directory_takes_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv('STOCKTALK_STATE_DIR', str(tmp_path / 'explicit'))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'xdg'))
    with account_lock('bilibili', 'fixture'):
        assert (tmp_path / 'explicit/account-locks').is_dir()
        assert not (tmp_path / 'xdg').exists()


def test_default_lock_directory_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv('STOCKTALK_STATE_DIR', raising=False)
    monkeypatch.delenv('XDG_STATE_HOME', raising=False)
    monkeypatch.setenv('HOME', str(tmp_path))
    with account_lock('bilibili', 'fixture'):
        assert (Path.home() / '.local/state/stocktalk/account-locks').is_dir()


def test_state_override_preserves_cross_process_exclusion(tmp_path, monkeypatch):
    import subprocess
    import sys
    monkeypatch.setenv('STOCKTALK_STATE_DIR', str(tmp_path / 'state'))
    code = '''from stocktalk.modules.runtime_lock import account_lock, AccountBusy
try:
    with account_lock("bilibili", "fixture"):
        pass
except AccountBusy:
    raise SystemExit(23)
'''
    with account_lock('bilibili', 'fixture'):
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, timeout=10)
        assert result.returncode == 23, result.stderr
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
