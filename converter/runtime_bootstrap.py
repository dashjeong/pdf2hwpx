"""Load app-local Windows C++ runtime DLLs without a system-wide installer."""
from __future__ import annotations

import os
import sys
from pathlib import Path

_DLL_HANDLES = []


def configure_native_runtime() -> None:
    if sys.platform != "win32" or _DLL_HANDLES:
        return
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    directory = root / "runtime" / "msvc"
    if directory.is_dir():
        # Keep the directory handle alive as long as native extensions may load.
        _DLL_HANDLES.append(os.add_dll_directory(str(directory.resolve())))


configure_native_runtime()
