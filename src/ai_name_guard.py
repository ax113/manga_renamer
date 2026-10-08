"""Narrow, local checks before applying a review name; no prompt rewriting.

Only explicit naming metadata or trusted, specific source facts are protected.
The existing candidate alone is not evidence of translation or an AI method.
This module never changes names, review decisions, classifications or inputs.
"""
from __future__ import annotations

import re
import unicodedata

from .analyzer import (
    AI_TRANSLATION_POLISHED_RE, AI_TRANSLATION_TEXT_RE, BRACKET_BLOCK_RE,
    GENERIC_CHINESE_LANGUAGE_BLOCK_RE, SPECIAL_MARKER_TEXT_RE,
    TRANSLATED_CHINESE_BLOCK_RE, _is_translation_group_text, _semantic_marker_key,
    parse_tags,
)
from .models import WorkItem

NAME_GUARD_VERSION = "0.3.3-r6"

_ONGOING_WORD = r"(?:on\s*going|進行中|进行中|持[续續]更新中)"
_ONGOING_BLOCK_RE = re.compile(
    _ONGOING_WORD + r"(?:[\s/、,，+&＆·・-]*" + _ONGOING_WORD
    + r")*\s*(?:\.{3,}|…+|⋯+)?", re.IGNORECASE,
)


def _blocks(text: str) -> list[str]:
    # Reuse the analyzer's recognizers, but require matching delimiters here.
    pairs = {"[": "]", "【": "】", "［": "］", "(": ")", "（": "）"}
    return [m.group("content").strip() for m in BRACKET_BLOCK_RE.finditer(text or "")
            if pairs[m.group("open")] == m.group("close")]


def _mode(content: str) -> str:
    value = content.strip()
    if AI_TRANSLATION_POLISHED_RE.fullmatch(value):
        return "ai-polished"
    if AI_TRANSLATION_TEXT_RE.fullmatch(value):
        return "ai"
    if value in {"机翻", "機翻"}:
        return "machine"
    return ""


def _modes(text: str) -> set[str]:
    modes = {_mode(c) for c in _blocks(text)} - {""}
    # Unbracketed filename suffixes, e.g. Title-ai翻译; not title prose.
    tail = re.search(r"(?:^|[-_\s])([^\[\]【】［］()（）]+)$", text or "")
    if tail:
        suffix = re.search(r"(?:^|[-_\s])((?:中文\s*)?(?:AI|机器|機器).+)$",
                           tail.group(0), re.IGNORECASE)
        if suffix and _mode(suffix.group(1)):
            modes.add(_mode(suffix.group(1)))
    return modes


def _no_text(content: str) -> bool:
    compact = re.sub(r"[\s_-]+", "", content).casefold()
    return compact in {"notext", "textless", "无文字", "無文字"}


def _generated(content: str) -> bool:
    compact = re.sub(r"[\s_-]+", "", unicodedata.normalize("NFKC", content)).casefold()
    return compact in {"aigenerated", "ai生成", "ai生成作品", "ai绘图", "ai繪圖"}


def _ongoing(content: str) -> bool:
    # Entire metadata blocks only: words inside title prose are not state.
    return bool(_ONGOING_BLOCK_RE.fullmatch(
        unicodedata.normalize("NFKC", content or "").strip()))


def _metadata(content: str) -> bool:
    value = content.strip()
    return bool(_mode(value) or _no_text(value) or _ongoing(value) or _generated(value)
                or SPECIAL_MARKER_TEXT_RE.fullmatch(value)
                or GENERIC_CHINESE_LANGUAGE_BLOCK_RE.fullmatch(value)
                or TRANSLATED_CHINESE_BLOCK_RE.fullmatch(value)
                or _is_translation_group_text(value))


def _square_head(text: str) -> tuple[str, str] | None:
    value = unicodedata.normalize("NFKC", text or "").strip()
    value = value.replace("【", "[").replace("】", "]")
    if not value.startswith("["):
        return None
    depth = 0
    for index, char in enumerate(value):
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return value[1:index], value[index + 1:].strip()
    return None


def _metadata_tail(text: str) -> bool:
    # An actual title after a creator block must never be treated as a tail.
    remaining = text.strip()
    while remaining:
        match = BRACKET_BLOCK_RE.match(remaining)
        if not match or not _blocks(match.group()) or not _metadata(match.group("content")):
            return False
        remaining = remaining[match.end():].strip()
    return True


def _title_key(text: str) -> str:
    value = unicodedata.normalize("NFKC", text or "")
    value = value.replace("【", "[").replace("】", "]")
    value = BRACKET_BLOCK_RE.sub(
        lambda match: "" if _blocks(match.group()) and _metadata(match.group("content"))
        else match.group(), value,
    )
    return re.sub(r"[\s_]+", "", value).casefold()


def _new_title_wrapper(local: str, current: str, proposed: str) -> bool:
    head = _square_head(proposed)
    if not head or not head[1] or not _metadata_tail(head[1]):
        return False
    key = _title_key(head[0])
    if not key:
        return False
    # Do not repair old bracket titles or infer creator identity from length.
    for before in (local, current):
        old_head = _square_head(before)
        if old_head and _metadata_tail(old_head[1]) and _title_key(old_head[0]) == key:
            return False
    return any(_title_key(before) == key for before in (local, current))


def _whole_square_block(text: str) -> bool:
    value = unicodedata.normalize("NFKC", text or "").strip()
    value = value.replace("【", "[").replace("】", "]")
    if not value.startswith("["):
        return False
    depth = 0
    for index, char in enumerate(value):
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return index == len(value) - 1
            if depth < 0:
                return False
    return False


def naming_content_problem(item: WorkItem, proposed: str) -> str:
    """Return a refusal reason, or empty text; never repair a model answer."""
    local = item.original_name or ""
    current = item.program_suggested_name or ""
    source = [item.record.title or "", item.record.title_jpn or ""] if item.record else []
    # Caller already rejects untrusted/gallery-conflicted source application.
    evidence = [local, *source]
    blocks = [c for text in evidence for c in _blocks(text)]
    proposed_blocks = _blocks(proposed)
    tags = parse_tags(item.record.tags_raw) if item.record else {}
    languages = {str(x).strip().casefold() for x in tags.get("language", [])
                 if isinstance(x, str)} if isinstance(tags.get("language", []), list) else set()

    if (_whole_square_block(proposed)
            and not _whole_square_block(local) and not _whole_square_block(current)):
        return "建议名将整个名称套入方括号"

    if _new_title_wrapper(local, current, proposed):
        return "建议名将原有标题主体套入方括号（后接附加标签）"

    analysis = (item.extra or {}).get('analysis', {})
    trusted = bool(item.record) and not analysis.get('gallery_match_risk') and analysis.get('source_trust') != 'untrusted'
    local_generated = any(_generated(c) for c in _blocks(local))
    other = tags.get('other', []) if trusted else []
    source_generated = trusted and (any(_generated(c) for text in source for c in _blocks(text))
        or any(str(tag).strip().casefold() == 'ai generated' for tag in other if isinstance(tag, str)))
    generated_evidence = local_generated or source_generated
    proposed_generated = any(_generated(c) for c in proposed_blocks)
    if generated_evidence and not proposed_generated:
        return '明确生成标记未保留 [AI Generated]'
    if proposed_generated and not generated_evidence:
        return '无可靠依据新增生成标记 [AI Generated]'

    if any(_ongoing(c) for c in _blocks(local)) and not any(
            _ongoing(c) for c in proposed_blocks):
        return "本地明确进行中状态未保留 [進行中]"

    no_text = (bool(languages & {"speechless", "text cleaned", "no text"})
               or any(_no_text(c) for c in blocks))
    if no_text and not any(_no_text(c) for c in proposed_blocks):
        return "明确无文字信息未保留 [No Text]"

    # Do not trust is_chinese_translation or a candidate generated from only
    # language:chinese. Such ambiguous identity is still for the model/manual review.
    chinese_translation = ({"chinese", "translated"} <= languages
                           or any(TRANSLATED_CHINESE_BLOCK_RE.fullmatch(c) for c in blocks))
    if chinese_translation and not any(TRANSLATED_CHINESE_BLOCK_RE.fullmatch(c)
                                       for c in proposed_blocks):
        return "明确中文翻译信息未保留 [中国翻訳]"

    # Local explicit method wins. Trusted source is a fallback, not an excuse
    # to change the local version's plain AI translation into a polished version.
    modes = _modes(local) or set().union(*(_modes(text) for text in source))
    proposed_modes = _modes(proposed)
    labels = {"ai": "AI翻译", "ai-polished": "AI翻译润色", "machine": "机翻"}
    if modes - proposed_modes:
        return "明确翻译方式未保留：" + "、".join(labels[x] for x in sorted(modes - proposed_modes))
    unsupported = (proposed_modes & {"ai", "ai-polished"}) - modes
    if unsupported:
        return "无可靠依据新增翻译方式：" + "、".join(labels[x] for x in sorted(unsupported))

    # Protect confidently recognized translation group blocks with semantic
    # normalization (brackets/whitespace/underscores), not arbitrary author text.
    proposed_keys = {_semantic_marker_key(c) for c in proposed_blocks}
    for content in blocks:
        key = _semantic_marker_key(content)
        if _is_translation_group_text(content) and key.startswith("translation-group:"):
            if key not in proposed_keys:
                return "明确译者或汉化组信息未保留：" + content

    local_versions = {c.casefold() for c in _blocks(local) if c.casefold() in {"digital", "dl版"}}
    proposed_versions = {c.casefold() for c in proposed_blocks if c.casefold() in {"digital", "dl版"}}
    if local_versions and proposed_versions != local_versions:
        return "本地 Digital / DL版标记被删除、替换或混加"
    return ""
