"""Write a frozen session in a separate process so JSON work cannot block Qt."""

from __future__ import annotations

import pickle
from pathlib import Path
from time import perf_counter

from .session_store import save_session


def write_frozen_session(path: Path, frozen_snapshot: bytes) -> float:
    started = perf_counter()
    save_session(path, pickle.loads(frozen_snapshot))
    return (perf_counter() - started) * 1000
