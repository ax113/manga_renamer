from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .models import MangaDbRecord


@dataclass
class DatabaseInfo:
    source_path: str
    snapshot_path: str
    record_count: int
    source_mtime: float
    snapshot_created_at: str
    fingerprint: str


def validate_database(path: str) -> tuple[bool, str]:
    if not path:
        return False, "未选择数据库"
    if not os.path.isfile(path):
        return False, "数据库文件不存在"
    try:
        with sqlite3.connect(path) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='Mangas'"
            ).fetchone()
            if not row:
                return False, "数据库中没有 Mangas 表"
            cols = {r[1] for r in conn.execute("PRAGMA table_info(Mangas)")}
            required = {"id", "filepath", "title", "title_jpn", "status", "tags"}
            missing = sorted(required - cols)
            if missing:
                return False, f"Mangas 表缺少字段：{', '.join(missing)}"
    except sqlite3.Error as exc:
        return False, f"数据库读取失败：{exc}"
    return True, ""


def _fingerprint(path: str) -> str:
    st = os.stat(path)
    seed = f"{st.st_size}|{st.st_mtime_ns}|{os.path.abspath(path)}".encode("utf-8", "ignore")
    return hashlib.sha256(seed).hexdigest()[:12].upper()


def create_snapshot(source_path: str, snapshot_dir: str) -> DatabaseInfo:
    ok, message = validate_database(source_path)
    if not ok:
        raise ValueError(message)

    os.makedirs(snapshot_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    fp = _fingerprint(source_path)
    snapshot_path = os.path.join(snapshot_dir, f"database_{stamp}_{fp}.sqlite")

    # 使用 SQLite Backup API，而不是直接复制正在使用的数据库文件。
    src = sqlite3.connect(source_path)
    src.execute("PRAGMA query_only = ON")
    dst = sqlite3.connect(snapshot_path)
    try:
        src.backup(dst)
        dst.commit()
        record_count = dst.execute("SELECT COUNT(*) FROM Mangas").fetchone()[0]
    finally:
        dst.close()
        src.close()

    return DatabaseInfo(
        source_path=os.path.abspath(source_path),
        snapshot_path=os.path.abspath(snapshot_path),
        record_count=record_count,
        source_mtime=os.path.getmtime(source_path),
        snapshot_created_at=datetime.now().isoformat(timespec="seconds"),
        fingerprint=fp,
    )


def load_records(snapshot_path: str) -> list[MangaDbRecord]:
    query = """
        SELECT
            id,
            COALESCE(title, ''),
            COALESCE(filepath, ''),
            COALESCE(hash, ''),
            COALESCE(status, ''),
            COALESCE(tags, '{}'),
            COALESCE(title_jpn, ''),
            filecount,
            pageCount,
            COALESCE(category, ''),
            COALESCE(url, ''),
            rating
        FROM Mangas
    """
    with sqlite3.connect(snapshot_path) as conn:
        rows = conn.execute(query).fetchall()
    return [MangaDbRecord(*row) for row in rows]


SNAPSHOT_RETENTION_DAYS = 7
SNAPSHOT_MAX_UNREFERENCED_PER_DAY = 5


def _normalize_path(path: str | os.PathLike) -> str:
    try:
        return os.path.normcase(os.path.abspath(os.fspath(path)))
    except Exception:
        return ""


def _collect_snapshot_paths_from_json(value) -> set[str]:
    """递归查找 JSON 结构里所有 snapshot_path，兼容未来操作历史格式。"""
    result: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "snapshot_path" and isinstance(item, str) and item:
                norm = _normalize_path(item)
                if norm:
                    result.add(norm)
            else:
                result.update(_collect_snapshot_paths_from_json(item))
    elif isinstance(value, list):
        for item in value:
            result.update(_collect_snapshot_paths_from_json(item))
    return result


def collect_referenced_snapshots(reference_roots: Iterable[str | os.PathLike]) -> set[str]:
    """从 sessions / history 等目录收集仍被引用的快照路径。"""
    referenced: set[str] = set()
    for root_value in reference_roots:
        root = Path(root_value)
        if not root.exists():
            continue
        try:
            files = root.rglob("*.json") if root.is_dir() else [root]
            for path in files:
                try:
                    data = json.loads(Path(path).read_text(encoding="utf-8"))
                    referenced.update(_collect_snapshot_paths_from_json(data))
                except (OSError, json.JSONDecodeError, UnicodeError):
                    continue
        except OSError:
            continue
    return referenced


def cleanup_snapshots(
    snapshot_dir: str | os.PathLike,
    reference_roots: Iterable[str | os.PathLike] = (),
    retention_days: int = SNAPSHOT_RETENTION_DAYS,
    max_unreferenced_per_day: int = SNAPSHOT_MAX_UNREFERENCED_PER_DAY,
) -> dict:
    """安全清理数据库快照。

    规则：
    - sessions / history 中仍被引用的快照永不自动删除；
    - 未被引用且超过 retention_days 的快照删除；
    - 保留期内同一天未被引用的快照最多保留最近 N 份；
    - 无法识别/删除的文件只跳过，不影响程序启动和扫描。
    """
    root = Path(snapshot_dir)
    root.mkdir(parents=True, exist_ok=True)
    referenced = collect_referenced_snapshots(reference_roots)

    now = datetime.now()
    cutoff_ts = (now.timestamp() - max(1, int(retention_days)) * 86400)
    max_per_day = max(1, int(max_unreferenced_per_day))

    entries: list[tuple[Path, float, bool]] = []
    for path in root.glob("*.sqlite"):
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append((path, stat.st_mtime, _normalize_path(path) in referenced))

    deleted: list[str] = []
    kept_referenced: list[str] = []
    kept_recent: list[str] = []

    recent_unreferenced_by_day: dict[str, list[tuple[Path, float]]] = {}

    for path, mtime, is_referenced in entries:
        if is_referenced:
            kept_referenced.append(str(path))
            continue
        if mtime < cutoff_ts:
            try:
                path.unlink(missing_ok=True)
                deleted.append(str(path))
            except OSError:
                kept_recent.append(str(path))
            continue
        day = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
        recent_unreferenced_by_day.setdefault(day, []).append((path, mtime))

    for _day, day_items in recent_unreferenced_by_day.items():
        day_items.sort(key=lambda x: x[1], reverse=True)
        for idx, (path, _mtime) in enumerate(day_items):
            if idx < max_per_day:
                kept_recent.append(str(path))
                continue
            try:
                path.unlink(missing_ok=True)
                deleted.append(str(path))
            except OSError:
                kept_recent.append(str(path))

    return {
        "deleted": deleted,
        "kept_referenced": kept_referenced,
        "kept_recent": kept_recent,
        "referenced_count": len(referenced),
    }
