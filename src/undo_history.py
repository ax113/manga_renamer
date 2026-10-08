from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import CATEGORY_CONFIRMED, WorkItem


def snapshot_classification_state(item: WorkItem) -> dict[str, Any]:
    """Capture only classification/confirmation state, never name state."""
    return {
        "manual_category": bool(item.manual_category),
        "manual_category_value": str(item.manual_category_value or ""),
        "manual_confirmed": bool(item.manual_confirmed),
        "fallback_category": str(item.category or ""),
        "manual_ai_flow": str(item.manual_ai_flow or ""),
        "prior_manual_category": str(item.prior_manual_category or ""),
        "ai_special_flow": bool(item.ai_special_flow),
    }


def apply_classification_state(item: WorkItem, state: dict[str, Any]) -> None:
    item.manual_category = bool(state.get("manual_category", False))
    item.manual_category_value = str(state.get("manual_category_value", ""))
    item.manual_confirmed = bool(state.get("manual_confirmed", False))
    item.manual_ai_flow = str(state.get("manual_ai_flow", ""))
    item.prior_manual_category = str(state.get("prior_manual_category", ""))
    item.ai_special_flow = bool(state.get("ai_special_flow", False))
    if item.manual_confirmed:
        item.manual_category = True
        item.manual_category_value = CATEGORY_CONFIRMED
        item.category = CATEGORY_CONFIRMED
        # Confirmation follows the name that exists NOW. Undo/redo must never
        # roll a later manual edit back to an older name.
        item.confirmed_name = item.suggested_name or item.original_name
        return

    item.confirmed_name = ""
    if item.manual_category:
        if not item.manual_category_value:
            item.manual_category_value = str(state.get("fallback_category", ""))
        if item.manual_category_value:
            item.category = item.manual_category_value
            return

    item.manual_category = False
    item.manual_category_value = ""
    item.category = item.program_category or str(state.get("fallback_category", "")) or item.category


def snapshot_attribute_state(item: WorkItem, attribute: str) -> dict[str, Any]:
    overrides = item.manual_attribute_overrides or {}
    return {
        "present": attribute in (item.attributes or []),
        "override_present": attribute in overrides,
        "override_value": bool(overrides.get(attribute, False)),
    }


def apply_attribute_state(item: WorkItem, attribute: str, state: dict[str, Any]) -> None:
    attrs = list(item.attributes or [])
    present = bool(state.get("present", False))
    if present and attribute not in attrs:
        attrs.append(attribute)
    elif not present and attribute in attrs:
        attrs.remove(attribute)
    item.attributes = attrs

    overrides = dict(item.manual_attribute_overrides or {})
    if bool(state.get("override_present", False)):
        overrides[attribute] = bool(state.get("override_value", False))
    else:
        overrides.pop(attribute, None)
    item.manual_attribute_overrides = overrides


@dataclass(frozen=True)
class UndoChange:
    local_id: str
    before: dict[str, Any]
    after: dict[str, Any]


@dataclass(frozen=True)
class UndoRecord:
    action_name: str
    kind: str
    changes: tuple[UndoChange, ...]
    attribute: str = ""

    @property
    def item_count(self) -> int:
        return len(self.changes)


class UndoHistory:
    """Runtime-only state undo/redo history.

    History is intentionally not serialized with sessions. It is scoped to the
    currently loaded task and stores only the state fields owned by an action,
    never comic names or name-version selection.
    """

    def __init__(self, max_steps: int = 50):
        self.max_steps = max(1, int(max_steps))
        self.undo_stack: list[UndoRecord] = []
        self.redo_stack: list[UndoRecord] = []

    def clear(self) -> None:
        self.undo_stack.clear()
        self.redo_stack.clear()

    def push(self, record: UndoRecord) -> None:
        if not record.changes:
            return
        self.undo_stack.append(record)
        if len(self.undo_stack) > self.max_steps:
            del self.undo_stack[:-self.max_steps]
        self.redo_stack.clear()

    def pop_undo(self) -> UndoRecord | None:
        return self.undo_stack.pop() if self.undo_stack else None

    def pop_redo(self) -> UndoRecord | None:
        return self.redo_stack.pop() if self.redo_stack else None

    def finish_undo(self, record: UndoRecord) -> None:
        self.redo_stack.append(record)
        if len(self.redo_stack) > self.max_steps:
            del self.redo_stack[:-self.max_steps]

    def finish_redo(self, record: UndoRecord) -> None:
        self.undo_stack.append(record)
        if len(self.undo_stack) > self.max_steps:
            del self.undo_stack[:-self.max_steps]

    @property
    def next_undo(self) -> UndoRecord | None:
        return self.undo_stack[-1] if self.undo_stack else None

    @property
    def next_redo(self) -> UndoRecord | None:
        return self.redo_stack[-1] if self.redo_stack else None
