"""Cross-process locks shared by CLI, daemon and HTTP publication paths."""
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path


class AccountBusy(RuntimeError):
    pass


@contextmanager
def account_lock(platform: str, account: str):
    state = os.environ.get("STOCKTALK_STATE_DIR")
    if state:
        root = Path(state)
    else:
        root = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "stocktalk"
    root /= "account-locks"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256(f"{platform}\0{account}".encode()).hexdigest()
    with (root / key).open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AccountBusy("账号忙，请等待当前发布结束") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
