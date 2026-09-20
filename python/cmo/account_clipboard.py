"""Copy a fixed account-setup command to the host Windows text clipboard.

This module never executes the copied command or reads credentials.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ACCOUNT = re.compile(r"account-[1-5]\Z")
SCRIPTS = {"provision": "Provision-PiClineAccount.ps1",
           "signin": "Initialize-PiClineAccount.ps1"}
HELPER_ROOT = Path("C:/Workspace/repos/config/tools/pi")


def command_for(account: str, stage: str) -> str:
    if not isinstance(account, str) or not ACCOUNT.fullmatch(account):
        raise ValueError("invalid account alias")
    if stage not in SCRIPTS:
        raise ValueError("invalid setup stage")
    helper = HELPER_ROOT / SCRIPTS[stage]
    if not helper.is_file():
        raise FileNotFoundError("installed account setup helper unavailable")
    return f'powershell -NoProfile -File "{helper}" -Account {account}'


def copy_native_text(text: str) -> bool:
    """Use the interactive Windows session clipboard; verify in that session."""
    if os.name != "nt":
        return False
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$value=[Console]::In.ReadToEnd(); "
        "[System.Windows.Forms.Clipboard]::SetText($value,"
        "[System.Windows.Forms.TextDataFormat]::UnicodeText); "
        "if ([System.Windows.Forms.Clipboard]::GetText() -cne $value) {exit 3}"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Sta", "-Command", script],
            input=text, capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
