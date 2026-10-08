"""Paths anchored to the install, with lossless legacy-log compatibility."""
import os
import threading
from pathlib import Path

_MIGRATION_LOCK = threading.RLock()
_MIGRATED_ROOTS = set()


def app_root() -> Path:
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    path = app_root() / 'data'
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir() -> Path:
    root = app_root()
    target = root / 'logs'
    try:
        target.mkdir(parents=True,exist_ok=True)
    except OSError:
        return target  # TaskLog reports write failures; diagnostics cannot block review.
    with _MIGRATION_LOCK:
        if str(root) not in _MIGRATED_ROOTS:
            old = root / 'data' / 'logs'
            if old.is_dir():
                # Preserve colliding files in their original location. Readers search both roots.
                for source in sorted(old.rglob('*')):
                    if not source.is_file() or source.is_symlink(): continue
                    dest = target / source.relative_to(old)
                    if dest.exists(): continue
                    try:
                        dest.parent.mkdir(parents=True,exist_ok=True)
                        os.rename(source,dest)
                    except OSError:
                        pass  # Old evidence remains accessible; migration never deletes it.
            _MIGRATED_ROOTS.add(str(root))
    return target


def log_roots() -> tuple[Path,...]:
    return (logs_dir(),app_root()/'data'/'logs')
