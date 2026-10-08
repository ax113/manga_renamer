from __future__ import annotations

import os
import sys
import traceback
import json
import re
import threading
from datetime import datetime
from pathlib import Path
from .version import VERSION
from .app_paths import data_dir, logs_dir

_CURRENT_LOG_PATH: Path | None = None
_SECRETS: set[str] = set()
LOG_MAX_BYTES = 10 * 1024 * 1024
_LOCK = threading.RLock()


def register_secret(value: str) -> None:
    if isinstance(value, str) and value.strip():
        with _LOCK:
            _SECRETS.add(value.strip())


def _redact(text: str) -> str:
    with _LOCK:
        secrets = sorted(_SECRETS, key=len, reverse=True)
    for secret in secrets:
        text = text.replace(secret, "[API KEY REDACTED]")
        text = text.replace(json.dumps(secret, ensure_ascii=True)[1:-1], "[API KEY REDACTED]")
    text = re.sub(r'(?i)(\bBearer\s+)[^\s"\'<>\\,;]+', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)("(?:api[_-]?key|authorization)"\s*:\s*")[^"]*', r'\1[REDACTED]', text)
    return text


def redact_bytes(data: bytes) -> tuple[bytes, bool]:
    text = data.decode("utf-8", errors="surrogateescape")
    cleaned = _redact(text)
    return cleaned.encode("utf-8", errors="surrogateescape"), cleaned != text


def append_rotating(directory: Path, stem: str, text: str, suffix: str = ".log",
                    max_bytes: int | None = None) -> Path:
    """Append one whole event. New volumes never remove older evidence."""
    limit = LOG_MAX_BYTES if max_bytes is None else max_bytes
    encoded = (_redact(text).rstrip("\n") + "\n").encode("utf-8")
    with _LOCK:
        directory.mkdir(parents=True, exist_ok=True)
        volume = 1
        while True:
            path = directory / f"{stem}_{volume:03d}{suffix}"
            size = path.stat().st_size if path.exists() else 0
            if size == 0 or size + len(encoded) <= limit:
                with path.open("ab") as stream:
                    stream.write(encoded)
                    stream.flush()
                return path
            volume += 1


def _tool_dir() -> Path:
    return data_dir()


def _logs_dir() -> Path:
    return logs_dir()


def start_run_log() -> Path | None:
    """按日期与体积分卷；本版保留旧日志，不自动清理。"""
    global _CURRENT_LOG_PATH

    try:
        log_dir = _logs_dir()
    except OSError:
        _CURRENT_LOG_PATH = None
        try:
            print("运行日志目录无法创建，程序将继续启动。", file=sys.stderr)
        except (OSError, AttributeError):
            pass
        return None
    _CURRENT_LOG_PATH = log_dir / f"run_{datetime.now():%Y%m%d}_001.log"
    written = _append_raw(
        "=" * 80
        + "\n"
        + f"启动时间：{datetime.now().isoformat(timespec='seconds')}\n"
        + f"Python：{sys.version.replace(os.linesep, ' ')}\n"
        + f"软件版本：{VERSION}\n"
        + f"参数：{sys.argv!r}\n"
        + f"工作目录：{os.getcwd()}\n"
        + "=" * 80
        + "\n"
    )
    return _CURRENT_LOG_PATH if written else None


def current_log_path() -> Path | None:
    return _CURRENT_LOG_PATH


def _append_raw(text: str) -> bool:
    global _CURRENT_LOG_PATH
    if _CURRENT_LOG_PATH is None:
        return False
    try:
        _CURRENT_LOG_PATH = append_rotating(_CURRENT_LOG_PATH.parent, f"run_{datetime.now():%Y%m%d}", text)
        return True
    except OSError:
        try:
            print("运行日志写入失败。", file=sys.stderr)
        except (OSError, AttributeError):
            pass
        return False


def log_message(message: str, level: str = "INFO") -> None:
    stamp = datetime.now().isoformat(timespec="seconds")
    _append_raw(f"[{stamp}] [{level}] {message}\n")


def log_exception(exc_type=None, exc_value=None, exc_tb=None, prefix: str = "未处理异常") -> str:
    if exc_type is None:
        text = traceback.format_exc()
    else:
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    log_message(prefix, "ERROR")
    _append_raw(text)
    return _redact(text)
