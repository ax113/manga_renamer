from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from .models import CATEGORIES, WorkItem, generate_local_id


PAGE_SIZES = (50, 100, 200, 500)
DEFAULT_PAGE_SIZE = 100


@dataclass(frozen=True)
class PageAnchor:
    """A refresh anchor based on the whole page, never on a visual row."""

    scope: str
    page: int
    local_ids: tuple[str, ...]


def stable_item_key(item: WorkItem) -> tuple[str, str, str]:
    """Frozen V0.2.27 ordering: actual name, full path, then local_id."""

    actual_name = f"{item.original_name}{item.suffix}"
    return (
        actual_name.casefold(),
        str(item.original_path or "").casefold(),
        str(item.local_id or ""),
    )


def sort_items(items: Iterable[WorkItem]) -> list[WorkItem]:
    return sorted(items, key=stable_item_key)


def page_count(total: int, page_size: int) -> int:
    size = max(1, int(page_size))
    return max(1, math.ceil(max(0, int(total)) / size))


def clamp_page(page: int, total: int, page_size: int) -> int:
    try:
        value = int(page)
    except (TypeError, ValueError):
        value = 1
    return min(max(1, value), page_count(total, page_size))


def page_slice(items: Sequence[WorkItem], page: int, page_size: int) -> list[WorkItem]:
    valid_page = clamp_page(page, len(items), page_size)
    size = max(1, int(page_size))
    start = (valid_page - 1) * size
    return list(items[start:start + size])


def page_for_local_id(
    items: Sequence[WorkItem],
    local_id: str,
    page_size: int,
) -> int | None:
    for index, item in enumerate(items):
        if item.local_id == local_id:
            return index // max(1, int(page_size)) + 1
    return None


def resolve_anchor_page(
    old_page_local_ids: Sequence[str],
    old_page: int,
    new_items: Sequence[WorkItem],
    page_size: int,
) -> int:
    """Apply PAGE-MUTATE-ANCHOR-01 exactly as frozen."""

    new_ids = {item.local_id for item in new_items}
    for local_id in old_page_local_ids:
        if local_id not in new_ids:
            continue
        located = page_for_local_id(new_items, local_id, page_size)
        if located is not None:
            return located
    return clamp_page(old_page, len(new_items), page_size)


def ensure_unique_local_ids(items: Iterable[WorkItem]) -> list[dict[str, str]]:
    """Repair missing/duplicate IDs while no external AI/history references exist."""

    seen: set[str] = set()
    repairs: list[dict[str, str]] = []
    for item in items:
        current = str(item.local_id or "").strip()
        reason = ""
        missing_was_repaired = bool(
            isinstance(item.extra, dict) and item.extra.pop("_local_id_was_missing", False)
        )
        if missing_was_repaired:
            reason = "missing"
        elif not current:
            reason = "missing"
        elif current in seen:
            reason = "duplicate"
        if reason == "missing" and current and current not in seen:
            repairs.append({"old": "", "new": current, "reason": reason})
        elif reason:
            old = current
            current = generate_local_id()
            while current in seen:
                current = generate_local_id()
            item.local_id = current
            repairs.append({"old": old, "new": current, "reason": reason})
        else:
            item.local_id = current
        seen.add(current)
    return repairs


def group_and_sort(items: Iterable[WorkItem]) -> dict[str, list[WorkItem]]:
    grouped = {category: [] for category in CATEGORIES}
    for item in items:
        grouped.setdefault(item.category, []).append(item)
    for category in list(grouped):
        grouped[category] = sort_items(grouped[category])
    return grouped


def dedupe_in_order(items: Iterable[WorkItem]) -> list[WorkItem]:
    result: list[WorkItem] = []
    seen: set[str] = set()
    for item in items:
        if item.local_id in seen:
            continue
        seen.add(item.local_id)
        result.append(item)
    return result


def ordered_categories_export(
    category_items: Mapping[str, Sequence[WorkItem]],
    selected_categories: Iterable[str],
    *,
    search_label: str,
    search_results: Sequence[WorkItem],
) -> list[WorkItem]:
    """Fixed category order, search last, first occurrence wins."""

    chosen = set(selected_categories)
    combined: list[WorkItem] = []
    for category in CATEGORIES:
        if category in chosen:
            combined.extend(category_items.get(category, ()))
    if search_label in chosen:
        combined.extend(search_results)
    return dedupe_in_order(combined)


def ordered_all_export(
    category_items: Mapping[str, Sequence[WorkItem]],
) -> list[WorkItem]:
    combined: list[WorkItem] = []
    for category in CATEGORIES:
        combined.extend(category_items.get(category, ()))
    return dedupe_in_order(combined)
