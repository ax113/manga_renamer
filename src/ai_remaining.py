"""Remaining review eligibility shared by the task table and transport actions."""
from .ai_supplements import current_input_fingerprint
from .ai_txt_store import accessible

REMAINING_STATES = {"pending", "failed", "uncertain", "dispatching"}


def remaining_status(row, item):
    if row.get("state") not in REMAINING_STATES:
        return "done"
    if item is None:
        return "missing"
    control = row.get("input", {}).get("CONTROL", {})
    if item.review_round != control.get("review_round"):
        return "superseded"
    if not accessible(item.original_path):
        return "unavailable"
    if current_input_fingerprint(item, row["input"]) != control.get("input_fingerprint"):
        return "changed"
    return "ready"


def remaining_ids(task, by_id, selected=None):
    wanted = None if selected is None else set(selected)
    return tuple(i for i, row in task.get("items", {}).items()
                 if (wanted is None or i in wanted) and remaining_status(row, by_id.get(i)) == "ready")
