"""Explicitly launch fixed, isolated Pi provisioning and OAuth flow."""
from __future__ import annotations
import os
import subprocess
from pathlib import Path
from .account_clipboard import ACCOUNT, HELPER_ROOT


def launch_account_onboarding(account: str) -> subprocess.Popen:
    if not isinstance(account, str) or not ACCOUNT.fullmatch(account):
        raise ValueError("invalid account slot")
    if os.name != "nt":
        raise OSError("Windows account setup unavailable")
    helper = HELPER_ROOT / "Connect-PiClineAccount.ps1"
    if not helper.is_file():
        raise FileNotFoundError("installed account onboarding helper unavailable")
    # No interpolation, shell mode or credential movement between profiles.
    return subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-NoExit", "-File", str(helper),
         "-Account", account],
        cwd=str(Path.home()), close_fds=True,
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
