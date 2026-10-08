from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .models import (
    ATTRIBUTE_ARCHIVE,
    ATTRIBUTE_COSPLAY,
    CATEGORY_AI_REVIEWED,
    CATEGORY_LLM_REVIEW,
    CATEGORY_REVIEW,
    CATEGORY_CONFIRMED,
    MangaDbRecord,
    WorkItem,
)

SESSION_FORMAT = 5
SUPPORTED_SESSION_FORMATS = {1, 2, 3, 4, 5}


def _record_to_dict(record: MangaDbRecord | None):
    return asdict(record) if record is not None else None


def _record_from_dict(data):
    if not isinstance(data, dict):
        return None
    allowed = {
        "id", "title", "filepath", "hash", "status", "tags_raw", "title_jpn",
        "filecount", "page_count", "category", "url", "rating",
    }
    values = {k: v for k, v in data.items() if k in allowed}
    if "id" not in values:
        return None
    return MangaDbRecord(**values)


def item_to_dict(item: WorkItem) -> dict:
    return {
        "local_id": item.local_id,
        "original_path": item.original_path,
        "original_name": item.original_name,
        "suggested_name": item.suggested_name,
        "program_suggested_name": item.program_suggested_name,
        "manual_name": item.manual_name,
        "name_source": item.name_source,
        "suffix": item.suffix,
        "category": item.category,
        "program_category": item.program_category,
        "manual_category_value": item.manual_category_value,
        "match_method": item.match_method,
        "warning": item.warning,
        "manual_edited": item.manual_edited,
        "manual_category": item.manual_category,
        "attributes": list(item.attributes),
        "manual_attribute_overrides": dict(item.manual_attribute_overrides),
        "needs_ai": bool(item.needs_ai),
        "manual_confirmed": bool(item.manual_confirmed),
        "confirmed_name": item.confirmed_name,
        "ai_reviewed": bool(item.ai_reviewed),
        "ai_reviewed_at": item.ai_reviewed_at,
        "ai_model": item.ai_model,
        "ai_review_count": int(item.ai_review_count or 0),
        "ai_review_result": item.ai_review_result if isinstance(item.ai_review_result, dict) else {},
        "ai_status": item.ai_status,
        "review_round": int(item.review_round or 0),
        "manual_ai_flow": item.manual_ai_flow,
        "prior_manual_category": item.prior_manual_category,
        "ai_special_flow": bool(item.ai_special_flow),
        "file_status": item.file_status,
        "file_identity": item.file_identity,
        # 勾选状态故意不持久化。重新打开任务时全部保持未勾选，避免误执行。
        "extra": item.extra,
        "record": _record_to_dict(item.record),
    }


def _migrate_legacy_category(category: str, attributes: list[str]) -> str:
    value = str(category or "").strip()
    if value in {"需要大模型复核", "AI复核", "AI未审", "AI已审"}:
        return CATEGORY_LLM_REVIEW
    if value == CATEGORY_AI_REVIEWED:
        return CATEGORY_AI_REVIEWED
    if value == "需要复核":
        return CATEGORY_REVIEW
    if value in {"非标准命名", "画集/归档类", "画集/归档", "画集"}:
        if ATTRIBUTE_ARCHIVE not in attributes:
            attributes.append(ATTRIBUTE_ARCHIVE)
        return CATEGORY_REVIEW
    if value == "Cosplay":
        if ATTRIBUTE_COSPLAY not in attributes:
            attributes.append(ATTRIBUTE_COSPLAY)
        return CATEGORY_REVIEW
    return value or CATEGORY_REVIEW


def item_from_dict(data: dict) -> WorkItem:
    raw_local_id = str(data.get("local_id", "")).strip()
    attrs = data.get("attributes") if isinstance(data.get("attributes"), list) else []
    attrs = [
        ATTRIBUTE_ARCHIVE if str(x).strip() in {"画集/归档类", "画集/归档"} else str(x).strip()
        for x in attrs if str(x).strip()
    ]
    attrs = list(dict.fromkeys(attrs))
    category = _migrate_legacy_category(str(data.get("category", CATEGORY_REVIEW)), attrs)
    manual_confirmed = bool(data.get("manual_confirmed", False))
    manual_category = bool(data.get("manual_category", False)) or manual_confirmed
    manual_value = str(data.get("manual_category_value", ""))
    if manual_confirmed:
        manual_value = CATEGORY_CONFIRMED
    elif manual_category and not manual_value:
        manual_value = category

    item = WorkItem(
        local_id=raw_local_id,
        original_path=str(data.get("original_path", "")),
        original_name=str(data.get("original_name", "")),
        suggested_name=str(data.get("suggested_name", data.get("original_name", ""))),
        program_suggested_name=str(
            data.get("program_suggested_name", data.get("suggested_name", data.get("original_name", "")))
        ),
        manual_name=str(data.get("manual_name", "")),
        name_source=str(data.get("name_source", "manual" if data.get("manual_name") else "program")),
        suffix=str(data.get("suffix", "")),
        category=category,
        program_category=str(data.get("program_category", "")),
        manual_category_value=manual_value,
        match_method=str(data.get("match_method", "")),
        warning=str(data.get("warning", "")),
        record=_record_from_dict(data.get("record")),
        manual_edited=bool(data.get("manual_edited", bool(data.get("manual_name")))),
        manual_category=manual_category,
        checked=False,
        extra=data.get("extra") if isinstance(data.get("extra"), dict) else {},
        attributes=attrs,
        manual_attribute_overrides=(
            data.get("manual_attribute_overrides")
            if isinstance(data.get("manual_attribute_overrides"), dict)
            else {}
        ),
        needs_ai=bool(data.get("needs_ai", (data.get("extra") or {}).get("needs_ai", False))),
        manual_confirmed=manual_confirmed,
        confirmed_name=str(data.get("confirmed_name", "")),
        ai_reviewed=bool(data.get("ai_reviewed", False)),
        ai_reviewed_at=str(data.get("ai_reviewed_at", "")),
        ai_model=str(data.get("ai_model", "")),
        ai_review_count=int(data.get("ai_review_count", 0) or 0),
        ai_review_result=data.get("ai_review_result") if isinstance(data.get("ai_review_result"), dict) else {},
        ai_status=str(data.get("ai_status", "AI未审")),
        review_round=int(data.get("review_round", 0) or 0),
        manual_ai_flow=str(data.get("manual_ai_flow", "")),
        prior_manual_category=str(data.get("prior_manual_category", "")),
        ai_special_flow=bool(data.get("ai_special_flow", False)),
        file_status=str(data.get("file_status", "未执行")),
        file_identity=data.get("file_identity") if isinstance(data.get("file_identity"), dict) else {},
    )
    if not raw_local_id:
        item.extra = dict(item.extra or {})
        item.extra["_local_id_was_missing"] = True
    item.checked = False
    return item


def save_session(path: str | Path, payload: dict) -> None:
    target = Path(path)
    temp = None
    phase = "创建会话目录"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        data = dict(payload)
        data["format"] = SESSION_FORMAT
        data["saved_at"] = datetime.now().isoformat(timespec="seconds")
        phase = "序列化会话"
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        phase = "写入并同步临时文件"
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                         prefix=target.name + ".", suffix=".tmp", delete=False) as stream:
            temp = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        phase = "原子替换会话文件"
        for attempt in range(3):
            try:
                os.replace(temp, target)
                break
            except OSError as exc:
                if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 2:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except Exception as exc:
        exc.session_write_stage = phase
        raise
    finally:
        if temp is not None:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass


def load_session(path: str | Path) -> dict | None:
    target = Path(path)
    if not target.exists():
        return None
    data = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return None
    if int(data.get("format", 0)) not in SUPPORTED_SESSION_FORMATS:
        return None
    return data


def archive_session(path: str | Path, archive_dir: str | Path, prefix: str = "session") -> Path | None:
    source = Path(path)
    if not source.exists():
        return None
    archive = Path(archive_dir)
    archive.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = archive / f"{prefix}_{stamp}.json"
    counter = 1
    while dest.exists():
        dest = archive / f"{prefix}_{stamp}_{counter}.json"
        counter += 1
    shutil.copy2(source, dest)
    return dest


def create_checkpoint(path: str | Path, checkpoint_dir: str | Path) -> Path | None:
    """把当前会话复制为永久检查点。检查点目录会被数据库快照清理器视为引用来源。"""
    return archive_session(path, checkpoint_dir, prefix="checkpoint")


def delete_session(path: str | Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        pass
