"""Move legacy scheduled entry points into the project's isolated runtime."""

from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".venv"
PYTHON = RUNTIME / "bin" / "python"


def ensure_isolated_runtime() -> None:
    """Re-exec under ``.venv`` when an old cron entry uses system Python."""
    if not PYTHON.is_file():
        return
    try:
        already_isolated = Path(sys.prefix).resolve() == RUNTIME.resolve()
    except OSError:
        already_isolated = False
    if not already_isolated:
        os.execv(str(PYTHON), [str(PYTHON), *sys.argv])
