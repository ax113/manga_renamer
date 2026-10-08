from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
import uuid


# V0.2.23：主标签页只表达“处理建议”，不再混入作品类型/属性。
CATEGORY_SUGGESTED = "建议修改"
CATEGORY_UNCHANGED = "无需修改"
CATEGORY_REVIEW = "人工复核"
CATEGORY_LLM_REVIEW = "待AI复核"
CATEGORY_AI_REVIEWED = "AI复核完成"
CATEGORY_CONFIRMED = "已确认"
CATEGORY_UNMATCHED = "未匹配"
CATEGORY_ERROR = "异常"

# 兼容旧代码/旧会话；这两个值从 V0.2.23 起属于 attributes，不再是主分类。
CATEGORY_NONSTANDARD = "画集"
CATEGORY_COSPLAY = "Cosplay"

CATEGORIES = [
    CATEGORY_UNCHANGED,
    CATEGORY_SUGGESTED,
    CATEGORY_LLM_REVIEW,
    CATEGORY_AI_REVIEWED,
    CATEGORY_REVIEW,
    CATEGORY_CONFIRMED,
    CATEGORY_UNMATCHED,
    CATEGORY_ERROR,
]

ATTRIBUTE_ARCHIVE = "画集"
ATTRIBUTE_COSPLAY = "Cosplay"
ATTRIBUTE_NONSTANDARD_NAME = "非标准命名"
ATTRIBUTE_GALLERY_CONFLICT = "画廊关联冲突"


def generate_local_id() -> str:
    return f"M-{uuid.uuid4().hex[:16]}"


@dataclass
class MangaDbRecord:
    id: str
    title: str = ""
    filepath: str = ""
    hash: str = ""
    status: str = ""
    tags_raw: str = "{}"
    title_jpn: str = ""
    filecount: Optional[int] = None
    page_count: Optional[int] = None
    category: str = ""
    url: str = ""
    rating: Optional[float] = None


@dataclass
class WorkEntry:
    full_path: str
    display_name: str
    suffix: str = ""
    is_dir: bool = True
    relative_path: str = ""
    scan_depth: int = 1
    direct_image_count: int = 0
    nested_image_count: int = 0
    nested_dir_count: int = 0


@dataclass
class WorkItem:
    original_path: str
    original_name: str
    suggested_name: str
    suffix: str
    category: str
    local_id: str = field(default_factory=generate_local_id)
    match_method: str = ""
    warning: str = ""
    record: Optional[MangaDbRecord] = None
    manual_edited: bool = False
    manual_category: bool = False
    program_category: str = ""
    manual_category_value: str = ""
    checked: bool = False
    extra: dict = field(default_factory=dict)

    # V0.1.11 起把“程序建议”和“手动版本”分开保存。
    program_suggested_name: str = ""
    manual_name: str = ""
    name_source: str = "program"  # "program" / "manual"

    # V0.2.23：与“处理建议”分离的独立状态。
    attributes: list[str] = field(default_factory=list)
    manual_attribute_overrides: dict[str, bool] = field(default_factory=dict)
    needs_ai: bool = False

    # 人工修改 != 人工确认。确认状态下仍可编辑；重新分析保留用户确认的名称。
    manual_confirmed: bool = False
    confirmed_name: str = ""

    # AI审查是“实际发生过”的历史状态，与 needs_ai（程序推荐）完全不同。
    ai_reviewed: bool = False
    ai_reviewed_at: str = ""
    ai_model: str = ""
    ai_review_count: int = 0
    ai_review_result: dict = field(default_factory=dict)
    ai_status: str = "AI未审"  # Current independent state, never a main category.
    review_round: int = 0
    manual_ai_flow: str = ""  # "pending" / "complete" for explicit manual wait.
    prior_manual_category: str = ""
    ai_special_flow: bool = False
    file_status: str = "未执行"  # Independent of main categories and AI status.
    file_identity: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.file_status not in {"未执行", "已改名", "已移动", "已改名 · 已移动", "执行失败", "已撤销", "已撤销移动"}:
            self.file_status = "未执行"
        if not isinstance(self.file_identity, dict):
            self.file_identity = {}
        if self.ai_status not in {"AI未审", "AI已审", "AI审核失败"}:
            self.ai_status = "AI未审"
        if not str(self.local_id or "").strip():
            self.local_id = generate_local_id()
        if not self.program_category and not self.manual_category and not self.manual_confirmed:
            self.program_category = self.category
        if self.manual_category and not self.manual_category_value:
            self.manual_category_value = self.category
        if self.manual_confirmed:
            self.category = CATEGORY_CONFIRMED
            self.manual_category = True
            self.manual_category_value = CATEGORY_CONFIRMED
        if not self.program_suggested_name:
            self.program_suggested_name = self.suggested_name or self.original_name
        if self.name_source not in {"program", "manual"}:
            self.name_source = "manual" if self.manual_name else "program"

        if not isinstance(self.attributes, list):
            self.attributes = []
        normalized_attributes = []
        for value in self.attributes:
            value = str(value).strip()
            if value in {"画集/归档类", "画集/归档"}:
                value = ATTRIBUTE_ARCHIVE
            if value:
                normalized_attributes.append(value)
        self.attributes = list(dict.fromkeys(normalized_attributes))
        if not isinstance(self.manual_attribute_overrides, dict):
            self.manual_attribute_overrides = {}
        self.manual_attribute_overrides = {
            str(key).strip(): bool(value)
            for key, value in self.manual_attribute_overrides.items()
            if str(key).strip()
        }

        if self.manual_name:
            self.manual_edited = True
        if self.name_source == "manual" and self.manual_name:
            self.suggested_name = self.manual_name
        elif self.name_source == "program":
            self.suggested_name = self.program_suggested_name

        # 已确认名称在重新分析和恢复旧会话时保留，后续仍可经用户确认修改。
        if self.manual_confirmed:
            if not self.confirmed_name:
                self.confirmed_name = self.suggested_name or self.original_name
            self.suggested_name = self.confirmed_name

    @property
    def has_manual_version(self) -> bool:
        return bool(self.manual_name)

    @property
    def is_name_locked(self) -> bool:
        return False

    @property
    def final_name_with_suffix(self) -> str:
        return f"{self.suggested_name}{self.suffix}"
