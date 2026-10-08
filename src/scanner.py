from __future__ import annotations

import json
import ntpath
import os
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from .models import (
    CATEGORY_ERROR,
    CATEGORY_REVIEW,
    CATEGORY_UNMATCHED,
    MangaDbRecord,
    WorkEntry,
    WorkItem,
)

ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z", ".cbz", ".cbr"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif", ".jxl"}


def strip_archive_suffix(name: str) -> tuple[str, str]:
    lower = name.lower()
    for ext in sorted(ARCHIVE_EXTENSIONS, key=len, reverse=True):
        if lower.endswith(ext):
            return name[: -len(ext)], name[-len(ext) :]
    return name, ""


def db_basename(filepath: str) -> str:
    if not filepath:
        return ""
    # 数据库保存的是 Windows 路径，即使程序开发环境不是 Windows 也要按 Windows 规则切。
    base = ntpath.basename(filepath.rstrip("\\/"))
    return strip_archive_suffix(base)[0]


def normalize_name(value: str) -> str:
    return value.strip().casefold()


def normalize_windows_path(value: str) -> str:
    if not value:
        return ""
    return ntpath.normcase(ntpath.normpath(value.strip()))


def _directory_direct_profile(path: str) -> tuple[int, list[str]]:
    """返回目录直接图片数与直接子目录。

    普通漫画只扫描这一层；只有确认“既有直接图片又有子目录”的复合漫画，
    才进一步递归统计子目录图片，避免给大库增加无意义的全量递归 I/O。
    """
    image_count = 0
    subdirs: list[str] = []
    try:
        with os.scandir(path) as iterator:
            for entry in iterator:
                if entry.name.startswith("."):
                    continue
                try:
                    if entry.is_file(follow_symlinks=False):
                        if Path(entry.name).suffix.casefold() in IMAGE_EXTENSIONS:
                            image_count += 1
                    elif entry.is_dir(follow_symlinks=False):
                        subdirs.append(entry.path)
                except OSError:
                    continue
    except OSError:
        return 0, []
    return image_count, subdirs


def _count_nested_images(paths: list[str]) -> tuple[int, int]:
    """只为复合漫画递归统计子目录图片；返回 (图片数, 子目录总数)。"""
    images = 0
    dirs = 0
    stack = list(paths)
    while stack:
        path = stack.pop()
        dirs += 1
        try:
            with os.scandir(path) as iterator:
                for entry in iterator:
                    if entry.name.startswith("."):
                        continue
                    try:
                        if entry.is_file(follow_symlinks=False):
                            if Path(entry.name).suffix.casefold() in IMAGE_EXTENSIONS:
                                images += 1
                        elif entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                    except OSError:
                        continue
        except OSError:
            continue
    return images, dirs

def scan_work_directory(work_dir: str, max_depth: int = 2) -> list[WorkEntry]:
    """递归发现工作目录中的漫画项目。

    深度定义：工作目录的直接子项为第 1 层。
    - 目录内直接有图片：视为漫画目录，加入后停止向该目录继续递归。
    - 没有直接图片：视为容器候选，在 max_depth 范围内继续向下扫描。
    - zip/rar/7z/cbz/cbr：沿用旧行为，压缩包本身作为漫画项目。
    """
    root = os.path.abspath(work_dir)
    try:
        max_depth = max(1, int(max_depth))
    except Exception:
        max_depth = 2

    entries: list[WorkEntry] = []

    def add_archive(path: str, name: str, depth: int):
        stem, suffix = strip_archive_suffix(name)
        if suffix.lower() not in ARCHIVE_EXTENSIONS:
            return
        entries.append(
            WorkEntry(
                full_path=path,
                display_name=stem,
                suffix=suffix,
                is_dir=False,
                relative_path=os.path.relpath(path, root),
                scan_depth=depth,
            )
        )

    def walk(container: str, container_depth: int):
        # container_depth=0 表示工作目录本身；其直接子项深度为 1。
        item_depth = container_depth + 1
        if item_depth > max_depth:
            return
        try:
            children = list(os.scandir(container))
        except OSError:
            return

        for entry in children:
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_file(follow_symlinks=False):
                    add_archive(entry.path, entry.name, item_depth)
                    continue
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue

            direct_images, direct_subdirs = _directory_direct_profile(entry.path)
            if direct_images > 0:
                nested_images = 0
                nested_dirs = 0
                if direct_subdirs:
                    nested_images, nested_dirs = _count_nested_images(direct_subdirs)
                entries.append(
                    WorkEntry(
                        full_path=entry.path,
                        display_name=entry.name,
                        suffix="",
                        is_dir=True,
                        relative_path=os.path.relpath(entry.path, root),
                        scan_depth=item_depth,
                        direct_image_count=direct_images,
                        nested_image_count=nested_images,
                        nested_dir_count=nested_dirs,
                    )
                )
                # 复合漫画仍作为一本处理，不把下面章节目录拆成多本。
                continue

            if item_depth < max_depth:
                walk(entry.path, item_depth)

    walk(root, 0)
    entries.sort(key=lambda x: (x.relative_path.casefold(), x.display_name.casefold()))
    return entries


def match_entries(entries: list[WorkEntry], records: list[MangaDbRecord]) -> list[WorkItem]:
    by_full_path: dict[str, list[MangaDbRecord]] = defaultdict(list)
    by_name: dict[str, list[MangaDbRecord]] = defaultdict(list)

    for record in records:
        if record.filepath:
            by_full_path[normalize_windows_path(record.filepath)].append(record)
        name = db_basename(record.filepath)
        if name:
            by_name[normalize_name(name)].append(record)

    items: list[WorkItem] = []
    for entry in entries:
        # V0.1 的原则：数据库只负责识别，绝不主动把数据库中的其它记录拉进本批任务。
        exact_candidates = by_full_path.get(normalize_windows_path(entry.full_path), [])
        name_candidates = by_name.get(normalize_name(entry.display_name), [])

        if len(exact_candidates) == 1:
            record = exact_candidates[0]
            items.append(
                WorkItem(
                    original_path=entry.full_path,
                    original_name=entry.display_name,
                    suggested_name=entry.display_name,
                    suffix=entry.suffix,
                    category=CATEGORY_REVIEW,
                    match_method="完整路径匹配",
                    record=record,
                    extra={
                        "relative_path": entry.relative_path,
                        "scan_depth": entry.scan_depth,
                        "direct_image_count": entry.direct_image_count,
                        "nested_image_count": entry.nested_image_count,
                        "nested_dir_count": entry.nested_dir_count,
                        "local_page_count": (entry.direct_image_count + entry.nested_image_count) if entry.is_dir and (entry.direct_image_count + entry.nested_image_count) > 0 else None,
                        "composite_manga_dir": bool(entry.is_dir and entry.nested_dir_count > 0),
                    },
                )
            )
            continue

        if len(exact_candidates) > 1:
            items.append(
                WorkItem(
                    original_path=entry.full_path,
                    original_name=entry.display_name,
                    suggested_name=entry.display_name,
                    suffix=entry.suffix,
                    category=CATEGORY_ERROR,
                    match_method="完整路径重复",
                    warning=f"数据库中有 {len(exact_candidates)} 条相同路径记录",
                    extra={
                        "relative_path": entry.relative_path,
                        "scan_depth": entry.scan_depth,
                        "direct_image_count": entry.direct_image_count,
                        "nested_image_count": entry.nested_image_count,
                        "nested_dir_count": entry.nested_dir_count,
                        "local_page_count": (entry.direct_image_count + entry.nested_image_count) if entry.is_dir and (entry.direct_image_count + entry.nested_image_count) > 0 else None,
                        "composite_manga_dir": bool(entry.is_dir and entry.nested_dir_count > 0),
                    },
                )
            )
            continue

        if len(name_candidates) == 1:
            record = name_candidates[0]
            items.append(
                WorkItem(
                    original_path=entry.full_path,
                    original_name=entry.display_name,
                    suggested_name=entry.display_name,
                    suffix=entry.suffix,
                    category=CATEGORY_REVIEW,
                    match_method="文件夹名匹配",
                    record=record,
                    extra={
                        "relative_path": entry.relative_path,
                        "scan_depth": entry.scan_depth,
                        "direct_image_count": entry.direct_image_count,
                        "nested_image_count": entry.nested_image_count,
                        "nested_dir_count": entry.nested_dir_count,
                        "local_page_count": (entry.direct_image_count + entry.nested_image_count) if entry.is_dir and (entry.direct_image_count + entry.nested_image_count) > 0 else None,
                        "composite_manga_dir": bool(entry.is_dir and entry.nested_dir_count > 0),
                    },
                )
            )
            continue

        if len(name_candidates) > 1:
            items.append(
                WorkItem(
                    original_path=entry.full_path,
                    original_name=entry.display_name,
                    suggested_name=entry.display_name,
                    suffix=entry.suffix,
                    category=CATEGORY_ERROR,
                    match_method="名称匹配冲突",
                    warning=f"数据库中有 {len(name_candidates)} 条同名候选",
                    extra={
                        "relative_path": entry.relative_path,
                        "scan_depth": entry.scan_depth,
                        "direct_image_count": entry.direct_image_count,
                        "nested_image_count": entry.nested_image_count,
                        "nested_dir_count": entry.nested_dir_count,
                        "local_page_count": (entry.direct_image_count + entry.nested_image_count) if entry.is_dir and (entry.direct_image_count + entry.nested_image_count) > 0 else None,
                        "composite_manga_dir": bool(entry.is_dir and entry.nested_dir_count > 0),
                    },
                )
            )
            continue

        items.append(
            WorkItem(
                original_path=entry.full_path,
                original_name=entry.display_name,
                suggested_name=entry.display_name,
                suffix=entry.suffix,
                category=CATEGORY_UNMATCHED,
                match_method="未匹配",
                warning="当前工作目录中的项目没有可靠匹配到数据库记录",
                extra={
                        "relative_path": entry.relative_path,
                        "scan_depth": entry.scan_depth,
                        "direct_image_count": entry.direct_image_count,
                        "nested_image_count": entry.nested_image_count,
                        "nested_dir_count": entry.nested_dir_count,
                        "local_page_count": (entry.direct_image_count + entry.nested_image_count) if entry.is_dir and (entry.direct_image_count + entry.nested_image_count) > 0 else None,
                        "composite_manga_dir": bool(entry.is_dir and entry.nested_dir_count > 0),
                    },
            )
        )

    return items
