"""Launch only the registered account's interactive Pi login in a new Windows console.

No shell string, no dynamic script path, credentials, provider probe or Goal launch.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .account_clipboard import ACCOUNT, HELPER_ROOT


def launch_account_signin(account: str) -> subprocess.Popen:
    if not isinstance(account, str) or not ACCOUNT.fullmatch(account):
        raise ValueError("invalid account alias")
    if os.name != "nt":
        raise OSError("Windows sign-in terminal unavailable")
    helper = HELPER_ROOT / "Initialize-PiClineAccount.ps1"
    if not helper.is_file():
        raise FileNotFoundError("installed Pi sign-in helper unavailable")
    # Keep arguments separate: nothing from a web request is interpolated into a shell.
    return subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-NoExit", "-File", str(helper),
         "-Account", account, "-CurrentWindow"],
        cwd=str(Path.home()), close_fds=True,
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
