from __future__ import annotations

import sys
from importlib.util import find_spec
from pathlib import Path

__all__ = ["PACKAGE_ROOT", "SOURCE_ROOT", "ensure_source_path"]

PACKAGE_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = PACKAGE_ROOT.parent.parent / "src"


def ensure_source_path() -> None:
    if find_spec("wiki_ai") is not None:
        return
    if not SOURCE_ROOT.is_dir():
        return
    entry = str(SOURCE_ROOT)
    if entry not in sys.path:
        sys.path.insert(0, entry)


ensure_source_path()
