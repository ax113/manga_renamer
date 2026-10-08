from __future__ import annotations

import json
import re
import unicodedata
from difflib import SequenceMatcher

from .models import (
    CATEGORY_ERROR,
    CATEGORY_COSPLAY,
    CATEGORY_NONSTANDARD,
    CATEGORY_REVIEW,
    CATEGORY_LLM_REVIEW,
    CATEGORY_AI_REVIEWED,
    ATTRIBUTE_GALLERY_CONFLICT,
    CATEGORY_CONFIRMED,
    CATEGORY_SUGGESTED,
    CATEGORY_UNCHANGED,
    CATEGORY_UNMATCHED,
    ATTRIBUTE_ARCHIVE,
    ATTRIBUTE_COSPLAY,
    WorkItem,
)

ANALYZER_VERSION = "0.2.27"

CHINESE_MARKER = "[中国翻訳]"
AI_TRANSLATION_MARKER = "[AI翻译]"
AI_TRANSLATION_POLISHED_MARKER = "[AI翻译润色]"
NO_TEXT_MARKER = "[No Text]"

# 中文翻译身份：只要语义已经明确，就统一成 [中国翻訳]。
CHINESE_WORDS_RE = re.compile(
    r"(?:Chinese|中国翻訳|中国翻译|中国翻譯|中國翻译|中國翻譯|中国語|中国语|中國語|中國语|中文翻译|中文翻譯|中文)",
    re.IGNORECASE,
)
CHINESE_BRACKET_VARIANT_RE = re.compile(
    r"(?:\[|【|［|\(|（)\s*(?:Chinese|中国翻訳|中国翻译|中国翻譯|中國翻译|中國翻譯|中国語|中国语|中國語|中國语|中文翻译|中文翻譯|中文)\s*(?:\]|】|］|\)|）)",
    re.IGNORECASE,
)

# “AI 翻译”与“AI Generated”严格分开。
AI_TRANSLATION_TEXT_RE = re.compile(
    r"(?:中文\s*)?(?:AI)(?:\s*1)?\s*(?:中文\s*)?(?:翻译|翻譯|翻訳|机翻|機翻|汉化|漢化|翻(?![A-Za-z]))"
    r"|(?:中文\s*)?(?:AI)(?:\s*1)?\s*中文(?![\u3400-\u9fff])"
    r"|(?:机器|機器)\s*(?:翻译|翻譯)",
    re.IGNORECASE,
)

AI_TRANSLATION_POLISHED_RE = re.compile(
    r"(?:中文\s*)?(?:AI|ai|Ai|aI)\s*(?:机翻|機翻|翻译|翻譯|翻訳)\s*(?:润色|潤色)|(?:AI|ai|Ai|aI)\s*(?:中文\s*)?(?:翻译|翻譯|翻訳)\s*(?:润色|潤色)",
    re.IGNORECASE,
)

LANG_SHORT_RE = re.compile(r"^(?:CH|CN)$", re.IGNORECASE)

# 日本来源中常见的全角英数字；只转换英文字母、数字和用户明确要求的全角方/圆括号。
def normalize_fullwidth_latin_digits_brackets(text: str) -> str:
    out = []
    for ch in text or "":
        code = ord(ch)
        if 0xFF10 <= code <= 0xFF19 or 0xFF21 <= code <= 0xFF3A or 0xFF41 <= code <= 0xFF5A:
            out.append(chr(code - 0xFEE0))
        elif ch == "［":
            out.append("[")
        elif ch == "］":
            out.append("]")
        elif ch == "（":
            out.append("(")
        elif ch == "）":
            out.append(")")
        elif ch == "　":
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)

def _normalize_filename_underscores(text: str) -> tuple[str, bool]:
    value = text or ""
    # 文件名式英文标题常见： [Author]_Title_Words-ai翻译。
    # 关键点：作者/社团块内部的合法下划线（如 [Hessen_52hz]）必须原样保留。
    ai_tail = bool(re.search(
        r"[-_\s]*(?:中文\s*)?(?:AI)(?:\s*1)?\s*(?:中文\s*)?(?:翻译|翻譯|翻訳|机翻|機翻|汉化|漢化|翻)\s*$",
        value, re.IGNORECASE
    ))
    m_prefix = re.match(r"^((?:\[[^\]]+\]\s*)+)", value)
    prefix = m_prefix.group(1) if m_prefix else ""
    body = value[m_prefix.end():] if m_prefix else value
    many = body.count("_") >= 4
    english_sep = bool(re.search(r"(?<=[A-Za-z0-9'’])_(?=[A-Za-z0-9'’])", body))
    creator_sep = bool(prefix and re.match(r"^_+(?=[A-Za-z0-9])", body))
    if not (many or (ai_tail and (english_sep or creator_sep))):
        return value, False

    # 作者块之后的下划线是标题分隔符；作者块内部不动。
    if prefix:
        body = re.sub(r"^_+", " ", body)
    body = re.sub(r"(?<=[A-Za-z0-9'’])_+(?=[A-Za-z0-9'’])", " ", body)
    body = re.sub(
        r"[_-]+(?=\s*(?:中文\s*)?(?:AI)(?:\s*1)?\s*(?:中文\s*)?(?:翻译|翻譯|翻訳|机翻|機翻|汉化|漢化|翻)\s*$)",
        " ", body, flags=re.IGNORECASE
    )
    if many:
        body = re.sub(r"_+", " ", body)
    cleaned = (prefix + body).strip(" _-")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned, cleaned != value

TRANSLATION_GROUP_HINT_RE = re.compile(
    r"(?:汉化组|漢化組|汉化|漢化|翻译组|翻譯組|翻译|翻譯|翻訳|机翻|機翻)",
    re.IGNORECASE,
)
NEGATIVE_TRANSLATION_RE = re.compile(
    r"(?:没有|沒有|未|无|無)\s*(?:汉化|漢化|翻译|翻譯)",
    re.IGNORECASE,
)

# 不能按字面误判的已知汉化组/署名。
KNOWN_TRANSLATION_GROUP_NAMES = {
    "沒有漢化",
    "没有汉化",
    "果酱面包房",
    "酸菜魚ゅ°",
    "酸菜魚ゅ°个人汉化",
}

GENERIC_CHINESE_LANGUAGE_BLOCK_RE = re.compile(
    r"^(?:Chinese|中文|中国語|中国语|中國語|中國语|CH|CN|cn简|CN简|简中|簡中)$",
    re.IGNORECASE,
)

EXPLICIT_CHINESE_ORIGINAL_BLOCK_RE = re.compile(
    r"^(?:中国語|中国语|中國語|中國语)$", re.IGNORECASE
)

TRANSLATED_CHINESE_BLOCK_RE = re.compile(
    r"^(?:中国翻訳|中国翻译|中国翻譯|中國翻译|中國翻譯|中文翻译|中文翻譯)$",
    re.IGNORECASE,
)

CATEGORY_DESCRIPTOR_RE = re.compile(
    r"(?:^|\s)[(（](?:同人誌|同人志|成年コミック|成年漫画|Manga)[)）]\s*",
    re.IGNORECASE,
)

PROGRESS_MARKER_RE = re.compile(
    r"^(?:進行中|进行中|Ongoing|Complete|Completed|Completed Version|完結|完结|完整版)$",
    re.IGNORECASE,
)

# 这些是“附加标记”，合法时不互相转换；只负责识别边界和括号。
SPECIAL_MARKER_TEXT_RE = re.compile(
    r"^(?:DL版|Digital|AI[-\s]?Generated|AI\s*生成|無修正|无修正|半無修正|半无修正|Decensored|Uncensored|No Text|Full Color|フルカラー|彩色版|ENG|English|JP[／/]?EN|EN[／/]?JP|EP\.?\s*\d+[A-Za-z+.-]*)$",
    re.IGNORECASE,
)

# 活动名前缀。只移除明确匹配到“活动/展会”的圆括号，不能把 (DBComix)/(NekoNyanNyan) 之类作者误删。
CONVENTION_INNER_PATTERNS = [
    r"C\d{2,4}",                       # Comiket / Comic Market
    r"CSP\d+",                        # Comic Market Special
    r"COMIC1(?:☆|★)?\d+",
    r"COMITIA\d+",
    r"コミティア\d+",
    r"SC\d+",                         # Sunshine Creation
    r"サンクリ(?:\d{4}\s*(?:Summer|Winter)?|\d+)?",
    r"Reitaisai\s*\d+",
    r"例大祭\s*\d+",
    r"CT\d+",                         # Comic Treasure
    r"CR\d+",                         # Comic Revolution
    r"CCOsaka\d+",
    r"SPARK\d+",
    r"Futaket\s*\d+",
    r"ふたけっと\s*\d+",
    r"Mimiket\s*\d+",
    r"Puniket\s*\d+",
    r"CosCafe\s*\d+",
    r"FF\d+",                         # Fancy Frontier
    r"HaruCC\d+",
    r"SUPER(?:Kansai)?\d+",
    r"SHT[^)]*",
    r"コミケ\s*\d+",
]
EVENT_INNER_RE = r"(?:" + "|".join(CONVENTION_INNER_PATTERNS) + r")"
EVENT_BLOCK_RE = r"\(" + EVENT_INNER_RE + r"\)"
# 支持 (C96)+(C100) 这类复合活动前缀；只消费明确活动块，不碰普通标题圆括号。
EVENT_PREFIX_RE = re.compile(
    r"^\s*" + EVENT_BLOCK_RE + r"(?:\s*\+\s*" + EVENT_BLOCK_RE + r")*\s*",
    re.IGNORECASE,
)

LEADING_BRACKET_RE = re.compile(r"^\[([^\[\]]{1,120})\]\s*")

NON_AUTHOR_MARKERS = {
    "fanbox", "patreon", "fantia", "pixiv", "gumroad", "booth",
    "ai generated", "ai生成", "ai 生成", "中国翻訳", "中国語", "chinese",
    "dl版", "digital", "full color", "uncensored",
    "アンソロジー", "雑誌", "anthology",
    "skeb絵", "skeb绘", "skeb繪", "r-18", "r18", "差分", "variant", "variants",
}

KNOWN_CREATOR_PAREN_PREFIXES = {
    "dbcomix",
    "nekonyannyan",
}

SERVICE_RE = re.compile(r"\b(?:fanbox|patreon|fantia|pixiv|gumroad|booth)\b", re.IGNORECASE)
COLLECTION_RE = re.compile(
    r"\b(?:works|collection|compilation|archive|artbook|image\s*set|cg\s*set)\b|作品集|画像セット|合集|総集|まとめ|画集|圖集|图集|归档|歸檔",
    re.IGNORECASE,
)

INCOMPLETE_MARKER_RE = re.compile(
    r"^(?:ページ欠落|Incomplete|缺页|缺頁|ページ不足)$",
    re.IGNORECASE,
)

ROMAJI_HINT_RE = re.compile(
    r"\b(?:wa|no|ni|ga|wo|to|de|kara|made|shoujo|shojo|onna|otoko|kanojo|"
    r"oppai|futari|futanari|sensei|oneesan|imouto|ane|mama|musume|hime|mahou|"
    r"seihei|chikan|netorare|mesu|ero|ecchi|hentai|joutai|kaizou|gyakushuu|"
    r"sareru|shiro|kuro|hana|yume|koi|tsuma|musaboru)\b",
    re.IGNORECASE,
)

# 上传者描述性文本。命中时不能贸然“自动洗干净”，应进入复核。
UPLOAD_DESCRIPTION_RE = re.compile(
    r"(?:\bCompleted\b|\bOngoing\b|\b\d{2,5}\s*Pages?\b|\((?:English|Spanish|Korean|French|German)\))",
    re.IGNORECASE,
)

# 下列括号样式只在内容语义明确时规范成 []，绝不全局替换括号。
BRACKET_BLOCK_RE = re.compile(
    r"(?P<open>\[|【|［|\(|（)(?P<content>[^\[\]【】［］()（）]{1,100})(?P<close>\]|】|］|\)|）)"
)


def parse_tags(raw: str) -> dict:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _list_tag(tags: dict, key: str) -> list[str]:
    value = tags.get(key)
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def detect_text_kind(text: str) -> str:
    text = text or ""
    if not text.strip():
        return "空"
    has_hiragana = bool(re.search(r"[\u3040-\u309f]", text))
    has_katakana = bool(re.search(r"[\u30a0-\u30ff]", text))
    has_cjk = bool(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", text))
    has_latin = bool(re.search(r"[A-Za-z]", text))

    if has_hiragana or has_katakana:
        return "日文/拉丁混合" if has_latin else "日文"
    if has_cjk:
        return "中日汉字/拉丁混合" if has_latin else "纯汉字（中日待定）"
    if has_latin:
        hints = len(ROMAJI_HINT_RE.findall(text))
        return "拉丁字母（疑似日文罗马音）" if hints >= 2 else "拉丁字母（英文/罗马音待定）"
    return "其他/符号"


def contains_japanese_kana(text: str) -> bool:
    return bool(re.search(r"[\u3040-\u30ff]", text or ""))


def is_latin_only_text(text: str) -> bool:
    if not text or not re.search(r"[A-Za-z]", text):
        return False
    return not bool(re.search(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]", text))


def sanitize_windows_name(text: str) -> str:
    text = normalize_fullwidth_latin_digits_brackets((text or "").strip())
    # 只替换 Windows 真正禁止的字符。! 本身合法，因此 !? 应保留为 !？，
    # 不为了视觉统一把合法的 ! 强制改成全角。
    replacements = {
        # Windows 禁止半角 / 与 \\；使用视觉等价的全角字符，不把作品名符号粗暴改成连字符。
        "/": "／",
        "\\": "＼",
        ":": "：",
        "*": "＊",
        "?": "？",
        '"': "＂",
        "<": "＜",
        ">": "＞",
        "|": "｜",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    # 竖线的全角/装饰形式只在最终文件名中保留，不做全局删改。
    text = re.sub(r"\s+", " ", text).strip().rstrip(". ")
    return text


def strip_event_prefix(text: str, remove: bool = True) -> str:
    value = (text or "").strip()
    return EVENT_PREFIX_RE.sub("", value) if remove else value


def classify_leading_parenthesis(text: str, artist_tags: list[str] | None = None, group_tags: list[str] | None = None) -> tuple[str, str]:
    """返回 (原始块, 类型)：CONVENTION / CREATOR / UNKNOWN / NONE。"""
    value = (text or "").strip()
    m = re.match(r"^\(([^()]{1,120})\)\s*", value)
    if not m:
        return "", "NONE"
    raw = m.group(0).strip()
    inner = m.group(1).strip()
    if EVENT_PREFIX_RE.match(value):
        return raw, "CONVENTION"
    norm = _norm_entity(inner)
    if norm in KNOWN_CREATOR_PAREN_PREFIXES:
        return raw, "CREATOR"
    for tag in (artist_tags or []) + (group_tags or []):
        nt = _norm_entity(tag)
        if norm and nt and (norm == nt or norm in nt or nt in norm):
            return raw, "CREATOR"
    return raw, "UNKNOWN"


def _is_chinese_marker_text(content: str) -> bool:
    return bool(re.fullmatch(
        r"\s*(?:Chinese|中国翻訳|中国翻译|中国翻譯|中國翻译|中國翻譯|中国語|中国语|中國語|中國语|中文翻译|中文翻譯|中文)\s*",
        content or "",
        re.IGNORECASE,
    ))


def _is_ai_translation_polished_text(content: str) -> bool:
    return bool(AI_TRANSLATION_POLISHED_RE.search(content or ""))

def _is_ai_translation_text(content: str) -> bool:
    return bool(AI_TRANSLATION_TEXT_RE.search(content or "") or AI_TRANSLATION_POLISHED_RE.search(content or ""))


def _is_translation_group_text(content: str) -> bool:
    value = (content or "").strip()
    if not value or _is_chinese_marker_text(value):
        return False
    if value in KNOWN_TRANSLATION_GROUP_NAMES:
        return True
    if NEGATIVE_TRANSLATION_RE.search(value):
        return False
    if _is_ai_translation_text(value) and len(value) <= 16:
        # 纯“AI翻译/中文AI翻译”是方式标记，不当作组名。
        return False
    return bool(TRANSLATION_GROUP_HINT_RE.search(value))


def _is_special_marker_text(content: str) -> bool:
    return bool(SPECIAL_MARKER_TEXT_RE.fullmatch((content or "").strip()))


def _canonical_special_marker_text(content: str) -> str:
    value = (content or "").strip()
    cf = value.casefold()
    if cf in {"ai generated", "ai-generated", "aigenerated"} or re.fullmatch(r"AI\s*生成", value, re.IGNORECASE):
        return "AI Generated"
    # Digital / DL版 都是可选来源标记。只规范括号，不强制互相改写。
    if cf == "digital":
        return "Digital"
    if value == "DL版":
        return "DL版"
    if cf == "no text":
        return "No Text"
    # 原生无修正与后期去码不是同一版本身份，必须分开保留。
    if cf == "uncensored" or value in {"無修正", "无修正"}:
        return "無修正"
    if cf == "decensored":
        return "Decensored"
    compact = re.sub(r"\s+", "", value).upper()
    compact_ascii = compact.replace("／", "/")
    if compact_ascii in {"JPEN", "JP/EN", "ENJP", "EN/JP"}:
        return "JP／EN"
    return value


def _canonical_progress_marker_text(content: str) -> str:
    """进行中统一输出；完整状态属于默认值，不需要输出。"""
    value = (content or "").strip()
    if value.casefold() == "ongoing" or value in {"進行中", "进行中"}:
        return "進行中"
    if value.casefold() in {"complete", "completed", "completed version"} or value in {"完結", "完结", "完整版"}:
        return ""
    return value


def _is_henan_dialect_text(content: str) -> bool:
    value = re.sub(r"^[・·\s]+", "", (content or "").strip())
    return value.casefold() in {"河南話".casefold(), "河南话".casefold(), "henan dialect"}


def _contains_chinese_language_semantics(content: str) -> bool:
    value = (content or "").strip()
    return bool(re.search(r"(?:Chinese|中国翻訳|中国翻译|中国翻譯|中國翻译|中國翻譯|中国語|中国语|中國語|中國语|中文翻译|中文翻譯)", value, re.IGNORECASE))


def _has_explicit_chinese_block(text: str) -> bool:
    for m in BRACKET_BLOCK_RE.finditer(text or ""):
        if _is_chinese_marker_text(m.group("content")) or GENERIC_CHINESE_LANGUAGE_BLOCK_RE.fullmatch(m.group("content").strip()):
            return True
    return False


def _has_explicit_translation_evidence(*texts: str) -> bool:
    """只认结构化/尾部翻译说明，避免标题正文偶然出现“翻译”二字造成误判。"""
    for text in texts:
        value = text or ""
        if AI_TRANSLATION_TEXT_RE.search(value) or AI_TRANSLATION_POLISHED_RE.search(value):
            return True
        for m in BRACKET_BLOCK_RE.finditer(value):
            c = m.group("content").strip()
            if TRANSLATED_CHINESE_BLOCK_RE.fullmatch(c) or _is_translation_group_text(c):
                return True
        if re.search(r"(?:^|\s)(?:个人|個人|多人|团队|團隊)?\s*(?:汉化|漢化|翻译|翻譯|机翻|機翻)(?:组|組)?\s*$", value, re.IGNORECASE):
            return True
    return False


def _has_no_text_block(text: str) -> bool:
    for m in BRACKET_BLOCK_RE.finditer(text or ""):
        if _semantic_marker_key((m.group("content") or "").strip()) == "language:no-text":
            return True
    return False


def _normalize_anthology_prefix(text: str) -> tuple[str, bool]:
    value = text or ""
    changed = False
    for pattern in (r"^\s*\[Anthology\]\s*", r"^\s*\[雑誌\]\s*"):
        new = re.sub(pattern, "[アンソロジー] ", value, flags=re.IGNORECASE)
        if new != value:
            value = new
            changed = True
    return value.strip(), changed


def _ensure_anthology_prefix(text: str) -> tuple[str, bool]:
    """给已由 E-H 明确判定为多作者 anthology 的作品统一加 [アンソロジー]。

    这里只负责加统一分类前缀，不改变标题正文。
    """
    value = (text or "").strip()
    if re.match(r"^\[アンソロジー\](?:\s|$)", value):
        return value, False
    return f"[アンソロジー] {value}".strip(), True


def _fix_leading_creator_from_tags(text: str, artist_tags: list[str], group_tags: list[str]) -> tuple[str, bool]:
    """纠正 title 中被上传者写成 (Creator) 或 (Creator} 的 creator 前缀。

    只有内容能被 artist/group tags 佐证时才动，避免把活动名或标题正文圆括号误改。
    """
    value = (text or "").strip()
    m = re.match(r"^\s*\(([^(){}\[\]]{1,120})[)}]\s*", value)
    if not m:
        return value, False
    creator = m.group(1).strip()
    if not _creator_block_matches_tags(creator, artist_tags, group_tags):
        return value, False
    if EVENT_PREFIX_RE.match(value):
        return value, False
    return f"[{creator}] {value[m.end():].lstrip()}".strip(), True


def _body_without_leading_creator_and_tail(text: str) -> str:
    value = (text or "").strip()
    value = EVENT_PREFIX_RE.sub("", value).lstrip()
    value = CATEGORY_DESCRIPTOR_RE.sub(" ", value).strip()
    value = re.sub(r"^\[[^\]]+\]\s*", "", value, count=1).strip()
    core, _ = _extract_trailing_structured_markers(value)
    return re.sub(r"\s+", " ", core).strip()


def _review_body_key(text: str) -> str:
    """用于判断“候选正文与当前正文是否真的有实质差异”。

    忽略活动、creator 括号样式、尾部 metadata、(オリジナル) 等结构差异，
    也忽略 Unicode 组合形式和纯标点差异；只有正文字符顺序/内容变化才算实质差异。
    """
    value = unicodedata.normalize("NFKC", text or "").strip()
    value = EVENT_PREFIX_RE.sub("", value).lstrip()
    value = CATEGORY_DESCRIPTOR_RE.sub(" ", value).strip()
    value = re.sub(r"^(?:\[[^\]]+\]|【[^】]+】|［[^］]+］)\s*", "", value, count=1).strip()
    core, _ = _extract_trailing_structured_markers(value)
    core = re.sub(r"\s*[（(](?:original|オリジナル)[)）]\s*$", "", core, flags=re.IGNORECASE)
    # 保留字母数字/日中韩文字本身，忽略空格和纯标点。这样 FateGrandOrder 与 Fate/GrandOrder 不会制造复核噪声。
    return re.sub(r"[^0-9A-Za-z\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]+", "", core).casefold()


def _review_body_raw(text: str) -> str:
    """与 _review_body_key 同样剥离结构，但保留正文标点，用于发现 `_` vs `&` 等局部差异。"""
    value = unicodedata.normalize("NFKC", text or "").strip()
    value = EVENT_PREFIX_RE.sub("", value).lstrip()
    value = CATEGORY_DESCRIPTOR_RE.sub(" ", value).strip()
    value = re.sub(r"^(?:\[[^\]]+\]|【[^】]+】|［[^］]+］)\s*", "", value, count=1).strip()
    core, _ = _extract_trailing_structured_markers(value)
    core = re.sub(r"\s*[（(](?:original|オリジナル)[)）]\s*$", "", core, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", core).strip().casefold()


def _candidate_extends_local_body(local_name: str, candidate: str) -> bool:
    local_body = _body_without_leading_creator_and_tail(local_name)
    candidate_body = _body_without_leading_creator_and_tail(candidate)
    if not local_body or not candidate_body:
        return False
    a = local_body.casefold()
    b = candidate_body.casefold()
    if not b.startswith(a) or a == b:
        return False
    extra = candidate_body[len(local_body):].strip()
    # 只要来源标题确实多出可见正文/符号，就视为可能被旧本地名截断。
    return bool(extra)


def _separator_insensitive_key(text: str) -> str:
    value = sanitize_windows_name(text or "").casefold()
    value = re.sub(r"[_：:｜|／/]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _has_required_structure_normalization(current: str, suggested: str) -> bool:
    """当前差异若属于已经确认的结构规范，就不能被“纯美化”抑制。"""
    if not current or not suggested or current == suggested:
        return False

    # 全角数字/结构括号属于明确规范化。
    if re.search(r"[０-９［］（）]", current):
        return True

    # 当前存在结构损坏，而建议已经修复。
    if _has_unbalanced_delimiters(current) and not _has_unbalanced_delimiters(suggested):
        return True

    # creator 的圆括号前缀 -> 方括号，以及 `[社团(作者)] -> [社团 (作者)]`。
    if re.match(r"^\s*[（(]", current) and re.match(r"^\s*\[", suggested):
        return True
    if re.match(r"^\s*\[[^\]]+?\S\([^()]+\)\]", current):
        cur_block = re.match(r"^\s*(\[[^\]]+\])", current)
        sug_block = re.match(r"^\s*(\[[^\]]+\])", suggested)
        if cur_block and sug_block and cur_block.group(1) != sug_block.group(1):
            return True

    # 结构块之间/结构块与正文之间的标准半角空格不能视为视觉美化。
    if re.search(r"^\s*\[[^\]]+\](?=\S)", current):
        return True
    if re.search(r"\]\s*\[", current):
        for m in re.finditer(r"\](\s*)\[", current):
            if m.group(1) != " ":
                return True

    # 已知 metadata 使用 【】/［］ 等非标准结构括号时，规范为 [] 是硬规则。
    for m in BRACKET_BLOCK_RE.finditer(current):
        content = m.group("content").strip()
        semantic = _semantic_marker_key(content)
        if m.group("open") != "[" and (
            _is_special_marker_text(content)
            or _is_translation_group_text(content)
            or _is_ai_translation_text(content)
            or _is_chinese_marker_text(content)
            or semantic.startswith("series:")
            or semantic in {"language:english", "language:japanese", "language:jp-en"}
        ):
            return True
    return False


def _legacy_cosmetic_change_for_review_gate(current: str, suggested: str) -> bool:
    """
    仅用于语义复核路由的兼容判断。

    V0.2.25 会把一部分“仅空格/标点”建议撤回为当前名；V0.2.26 把其中已确认的
    结构规范改为必须执行。语义警告不能因为这些结构修正而消失，但也不能把
    下划线清洗、翻译标记补全等原本就会形成建议修改的项目批量误送 AI。
    因此这里保留旧版“纯标点/空格”判定的窄用途，不参与最终命名。
    """
    if not current or not suggested or current == suggested:
        return False
    if re.search(r"[０-９]", current):
        return False
    if re.match(r"^\s*[（(]", current) and re.match(r"^\s*\[", suggested):
        return False

    def content_key(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value or "").casefold()
        return "".join(ch for ch in normalized if unicodedata.category(ch)[0] in {"L", "N"})

    if content_key(current) != content_key(suggested):
        return False

    current_blocks = [_semantic_marker_key(m.group("content")) for m in BRACKET_BLOCK_RE.finditer(current)]
    suggested_blocks = [_semantic_marker_key(m.group("content")) for m in BRACKET_BLOCK_RE.finditer(suggested)]
    return current_blocks == suggested_blocks


def _only_cosmetic_punctuation_change(current: str, suggested: str) -> bool:
    """判断差异是否只来自空格/标点；确定的结构规范不能被这里撤回。"""
    if not current or not suggested or current == suggested:
        return False
    if _has_required_structure_normalization(current, suggested):
        return False

    def content_key(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value or "").casefold()
        return "".join(ch for ch in normalized if unicodedata.category(ch)[0] in {"L", "N"})

    if content_key(current) != content_key(suggested):
        return False

    current_blocks = [_semantic_marker_key(m.group("content")) for m in BRACKET_BLOCK_RE.finditer(current)]
    suggested_blocks = [_semantic_marker_key(m.group("content")) for m in BRACKET_BLOCK_RE.finditer(suggested)]
    return current_blocks == suggested_blocks


def _title_bodies_nearly_same_ignoring_creator(a: str, b: str) -> bool:
    ba = _body_without_leading_creator_and_tail(a)
    bb = _body_without_leading_creator_and_tail(b)
    if not ba or not bb:
        return False
    return SequenceMatcher(None, _normalize_compare(ba), _normalize_compare(bb)).ratio() >= 0.94


def _canonicalize_known_blocks(text: str, is_chinese_translation: bool, chinese_original: bool = False) -> tuple[str, list[str]]:
    """只规范语义明确的元数据块，返回规范后的文本与动作说明。"""
    actions: list[str] = []
    value = (text or "").strip()

    # 未加括号的 AI 翻译说明优先抽成结构标记；“AI机翻润色”保持复合语义。
    if AI_TRANSLATION_POLISHED_RE.search(value):
        value = AI_TRANSLATION_POLISHED_RE.sub(" ", value)
        if AI_TRANSLATION_POLISHED_MARKER not in value:
            value = f"{value.strip()} {AI_TRANSLATION_POLISHED_MARKER}".strip()
        actions.append("将 AI机翻/AI翻译润色说明规范为 [AI翻译润色]")
    elif AI_TRANSLATION_TEXT_RE.search(value):
        value = AI_TRANSLATION_TEXT_RE.sub(" ", value)
        if AI_TRANSLATION_MARKER not in value:
            value = f"{value.strip()} {AI_TRANSLATION_MARKER}".strip()
        actions.append("将 AI/机器翻译说明规范为 [AI翻译]")

    # 上面的全文提取可能把原本包在 []/【】 中的翻译说明抽空，清掉空结构块。
    value = re.sub(r"(?:\[\s*\]|【\s*】|\(\s*\))", " ", value)

    def repl_block(m: re.Match) -> str:
        content = m.group("content").strip()

        # 进行中属于需要保留的异常状态；完整/Completed 属于默认状态，明确 metadata 块直接删除。
        if PROGRESS_MARKER_RE.fullmatch(content):
            canonical_progress = _canonical_progress_marker_text(content)
            if canonical_progress == "":
                actions.append("移除 Complete/Completed/完結 等冗余完整状态标记")
                return ""
            if canonical_progress != content or m.group("open") != "[" or m.group("close") != "]":
                if canonical_progress == "進行中":
                    actions.append("将 Ongoing/进行中统一规范为 [進行中]")
            return f"[{canonical_progress}]"

        # 方言说明是独立 metadata，不与 [中国翻訳] 合并。
        if _is_henan_dialect_text(content):
            if content != "河南話" or m.group("open") != "[" or m.group("close") != "]":
                actions.append("将河南话/Henan dialect 统一规范为 [河南話]")
            return "[河南話]"

        # 历史数据中偶有 [中国翻訳・河南話] 这种复合块，拆成两个独立槽位。
        if re.fullmatch(r"(?:中国翻訳|中国翻译|中国翻譯|中國翻译|中國翻譯|中文翻译|中文翻譯)[・·\s]+(?:河南話|河南话|Henan dialect)", content, re.IGNORECASE):
            actions.append("将复合语言/方言标记拆分为 [中国翻訳] [河南話]")
            return f"{CHINESE_MARKER} [河南話]"

        if content in KNOWN_TRANSLATION_GROUP_NAMES:
            if m.group("open") != "[" or m.group("close") != "]":
                actions.append(f"将已知汉化组/译者标记 {m.group(0)} 规范为 [{content}]")
            return f"[{content}]"
        if EXPLICIT_CHINESE_ORIGINAL_BLOCK_RE.fullmatch(content):
            if is_chinese_translation:
                actions.append("将中文语言标记规范为 [中国翻訳]")
                return CHINESE_MARKER
            # 只有未判定为翻译版时才保留原始“中国語/中国语”语义。
            if m.group("open") != "[" or m.group("close") != "]":
                actions.append(f"将中国語/中国语语言标记 {m.group(0)} 规范为 [{content}]")
            return f"[{content}]"
        if GENERIC_CHINESE_LANGUAGE_BLOCK_RE.fullmatch(content):
            if is_chinese_translation:
                actions.append("将中文语言标记规范为 [中国翻訳]")
                return CHINESE_MARKER
            if chinese_original:
                actions.append("中文原创作品移除冗余中文语言说明")
                return ""
            return m.group(0)
        if _is_chinese_marker_text(content):
            if is_chinese_translation:
                actions.append("将中文语言标记规范为 [中国翻訳]")
                return CHINESE_MARKER
            if chinese_original and GENERIC_CHINESE_LANGUAGE_BLOCK_RE.fullmatch(content):
                actions.append("中文原创作品移除冗余中文语言说明")
                return ""
            # 中国翻訳/中文翻译一类仍表示翻译事实，不在这里擅自删除。
            return m.group(0)
        if _is_ai_translation_polished_text(content):
            actions.append("将 AI机翻/AI翻译润色说明规范为 [AI翻译润色]")
            return AI_TRANSLATION_POLISHED_MARKER
        if _is_ai_translation_text(content):
            actions.append("将 AI/机器翻译说明规范为 [AI翻译]")
            return AI_TRANSLATION_MARKER
        if _is_translation_group_text(content):
            # 汉化组/译者类统一使用半角 []。
            actions.append(f"将汉化组/译者标记 {m.group(0)} 规范为 [{content}]")
            return f"[{content}]"
        if _is_special_marker_text(content):
            canonical = _canonical_special_marker_text(content)
            # Digital / DL版 只规范括号，不互相改写；AI Generated / 無修正仍使用既定规范。
            if m.group("open") != "[" or m.group("close") != "]":
                actions.append(f"将附加标记 {m.group(0)} 的括号规范为 [{canonical}]")
            if canonical != content:
                if canonical == "AI Generated":
                    actions.append("将 AI Generated 标记统一规范为 [AI Generated]")
                elif canonical == "無修正":
                    actions.append("将 Uncensored/无修正规范为 [無修正]")
                elif canonical == "Decensored":
                    actions.append("保留 Decensored 后期去码版本标记")
                elif canonical == "No Text":
                    actions.append("将无文字标记统一规范为 [No Text]")
            return f"[{canonical}]"
        return m.group(0)

    value = BRACKET_BLOCK_RE.sub(repl_block, value)

    # 明确以分隔符引出的 Complete/Completed Version 是上传状态，不是作品正文。
    # 允许其后还有已抽取出的 [] metadata，例如 `[AI翻译] [中国翻訳]`。
    completed_version = re.sub(
        r"\s*[-–—_:：|｜]\s*(?:Complete|Completed)\s+Version(?=\s*(?:\[[^\]]+\]\s*)*$)",
        " ", value, flags=re.IGNORECASE,
    )
    if completed_version != value:
        value = completed_version.strip()
        actions.append("移除尾部 Completed Version 冗余完整状态说明")

    # 只删除文件名尾部独立出现的 Complete/Completed；Complete Edition 等正文不会命中。
    new_value = re.sub(r"\s+(?:Complete|Completed)\s*$", "", value, flags=re.IGNORECASE).strip()
    if new_value != value:
        value = new_value
        actions.append("移除尾部 Complete/Completed 冗余完整状态说明")

    if chinese_original:
        # cn简/简中 等通常紧挨在标题主体末尾、其后还可能有 [Decensored] 等版本标签。
        new_value = re.sub(
            r"\s*(?:cn简|CN简|简中|簡中)(?=\s*(?:\[[^\]]+\]\s*)*$)",
            "", value, flags=re.IGNORECASE
        ).strip()
        # 若前面还有 (CH)/(CN)/(Chinese) 等冗余语言说明，也一并移除。
        new_value = re.sub(
            r"\s*[\[(](?:CH|CN|Chinese|中文)[\])](?=\s*(?:\[[^\]]+\]\s*)*$)",
            "", new_value, flags=re.IGNORECASE
        ).strip()
        if new_value != value:
            value = new_value
            actions.append("中文原创作品移除冗余中文/简体语言说明")

    # 明确的未加括号“中文翻译”等尾部文字也统一为语言标记。
    # 只在末尾/结构边界处理，避免误伤标题正文。
    unbracketed_cn = re.compile(
        r"(?<![\w\]])\s*(?:中文翻译|中文翻譯|中国翻译|中國翻譯|Chinese)\s*$",
        re.IGNORECASE,
    )
    if unbracketed_cn.search(value):
        value = unbracketed_cn.sub("", value).strip()
        value = f"{value} {CHINESE_MARKER}".strip()
        actions.append("将未加方括号的中文翻译标记规范为 [中国翻訳]")

    # 明确位于末尾、但漏掉 [] 的常见附加标记。内容本身保持原样。
    unbracketed_special = re.compile(
        r"(?<![\]】）])\s*(DL版|Digital|AI Generated|AI\s*生成|無修正|无修正|Decensored|Uncensored|No Text|Full Color|フルカラー|彩色版)\s*$",
        re.IGNORECASE,
    )
    m_special = unbracketed_special.search(value)
    if m_special:
        marker_text = _canonical_special_marker_text(m_special.group(1))
        value = unbracketed_special.sub("", value).strip()
        value = f"{value} [{marker_text}]".strip()
        actions.append(f"为附加标记 {marker_text} 补充 []")

    # 明确位于末尾且带汉化/翻译关键词的来源说明，漏括号时补 []。
    # 排除“没有汉化/未汉化”等否定描述。
    tail_group = re.compile(r"\s+([^\[\]【】［］()（）]{2,40}(?:汉化组|漢化組|个人汉化|個人漢化|个人机翻汉化|個人機翻漢化|翻译组|翻譯組))\s*$", re.IGNORECASE)
    m_group = tail_group.search(value)
    if m_group and not NEGATIVE_TRANSLATION_RE.search(m_group.group(1)):
        content = m_group.group(1).strip()
        value = value[:m_group.start()].rstrip() + f" [{content}]"
        actions.append("为未加方括号的汉化组/译者标记补充 []")

    # 纯类别描述不是正式作品名：同人誌 / 成年コミック 等默认剔除。
    category_removed = bool(CATEGORY_DESCRIPTOR_RE.search(value))
    cleaned_category = CATEGORY_DESCRIPTOR_RE.sub(" ", value)
    cleaned_category = re.sub(r"\s+", " ", cleaned_category).strip()
    if cleaned_category != value:
        value = cleaned_category
    if category_removed:
        actions.append("移除同人誌/成年コミック等纯类别说明")

    # 上传者有时把汉化组写在最前面。将其移到尾部元数据区，避免被当成作者块并防止重复。
    moved_groups: list[str] = []
    while True:
        m_lead = LEADING_BRACKET_RE.match(value)
        if not m_lead:
            break
        c = m_lead.group(1).strip()
        if not _is_translation_group_text(c):
            break
        moved_groups.append(f"[{c}]")
        value = value[m_lead.end():].lstrip()
    if moved_groups:
        for g in moved_groups:
            if g not in value:
                value = f"{value} {g}".strip()
        actions.append("将开头的汉化组/译者标记移到尾部结构区")

    # AI Generated / No Text 等是 metadata，不能占据 creator 槽位。
    moved_specials: list[str] = []
    while True:
        m_lead = LEADING_BRACKET_RE.match(value)
        if not m_lead:
            break
        c = m_lead.group(1).strip()
        if not _is_special_marker_text(c):
            break
        canonical = _canonical_special_marker_text(c)
        moved_specials.append(f"[{canonical}]")
        value = value[m_lead.end():].lstrip()
    if moved_specials:
        for marker in moved_specials:
            if marker not in value:
                value = f"{value} {marker}".strip()
        actions.append("将开头的 AI Generated/No Text 等 metadata 移到尾部结构区")

    # E-H / 标题信息已确定为中文翻译时，最终统一使用 [中国翻訳]。
    if is_chinese_translation and CHINESE_MARKER not in value:
        value = f"{value} {CHINESE_MARKER}".strip()
        actions.append("E-H 标记为中文翻译，补充 [中国翻訳]")

    # 已识别 metadata 无论原来写在正文哪里，最终统一放到尾部。
    relocated, moved = _relocate_known_metadata_to_tail(value)
    if moved:
        value = relocated
        actions.append("将语言/翻译/版本 metadata 统一移动到尾部结构区")
    value = _cleanup_extracted_marker_residue(value)

    # 去重同一规范语言标记。
    value = re.sub(r"(?:\s*\[中国翻訳\]){2,}", f" {CHINESE_MARKER}", value).strip()
    value = re.sub(r"(?:\s*\[AI翻译\]){2,}", f" {AI_TRANSLATION_MARKER}", value).strip()
    value = re.sub(r"(?:\s*\[AI翻译润色\]){2,}", f" {AI_TRANSLATION_POLISHED_MARKER}", value).strip()
    return value, actions


def _relocate_known_metadata_to_tail(text: str) -> tuple[str, bool]:
    """把已经识别出的语言/翻译/版本 metadata 从正文任意位置移动到尾部。

    只处理语义明确的块；普通 [creator]、[Vol.00]、作品正文中的方括号不动。
    """
    value = text or ""
    moved: list[str] = []

    def repl(m: re.Match) -> str:
        content = m.group("content").strip()
        marker = ""
        if EXPLICIT_CHINESE_ORIGINAL_BLOCK_RE.fullmatch(content):
            marker = f"[{content}]"
        elif _is_chinese_marker_text(content) or GENERIC_CHINESE_LANGUAGE_BLOCK_RE.fullmatch(content):
            marker = CHINESE_MARKER
        elif _is_ai_translation_polished_text(content):
            marker = AI_TRANSLATION_POLISHED_MARKER
        elif _is_ai_translation_text(content):
            marker = AI_TRANSLATION_MARKER
        elif _is_translation_group_text(content):
            marker = f"[{content}]"
        elif _is_special_marker_text(content):
            marker = f"[{_canonical_special_marker_text(content)}]"
        if not marker:
            return m.group(0)
        moved.append(marker)
        return " "

    body = BRACKET_BLOCK_RE.sub(repl, value)
    if not moved:
        return value, False

    # 语义去重；若存在 中国語/中国语，则优先保留，不再追加默认 [中国翻訳]。
    out: list[str] = []
    seen: set[str] = set()
    has_original_cn = any(EXPLICIT_CHINESE_ORIGINAL_BLOCK_RE.fullmatch(x[1:-1]) for x in moved if x.startswith("[") and x.endswith("]"))
    for item in moved:
        content = item[1:-1] if item.startswith("[") and item.endswith("]") else item
        if has_original_cn and item == CHINESE_MARKER:
            continue
        key = _semantic_marker_key(content)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)

    body = re.sub(r"\s+", " ", body).strip(" _")
    rebuilt = f"{body} {' '.join(out)}".strip()
    return re.sub(r"\s+", " ", rebuilt).strip(), rebuilt.strip() != value.strip()


def _cleanup_extracted_marker_residue(text: str) -> str:
    """AI/语言说明抽走后，清理只剩包装意义的尾部符号与空壳。"""
    value = text or ""
    value = re.sub(r"(?:\[\s*\]|【\s*】|\(\s*\)|\{\s*(?:个人|個人)?\s*\})", " ", value, flags=re.IGNORECASE)
    # 只清理紧邻尾部结构 metadata 之前的孤立分隔符，不碰正文内部。
    value = re.sub(r"\s+[-_]+\s*(?=(?:\[[^\[\]]+\]\s*)+$)", " ", value)
    if CHINESE_MARKER in value or re.search(r"\[(?:中国語|中国语|中國語|中國语)\]", value):
        value = re.sub(r"\bChinese\b\s*(?=(?:\[[^\[\]]+\]\s*)+$)", " ", value, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", value).strip(" _")


def _normalize_structure_spacing(text: str) -> str:
    """只规范确定的结构块边界，不乱动标题正文内部空格。"""
    value = (text or "").strip()

    # 结构化 creator 统一使用 `[社团 (作者)]`。只处理开头方括号块，
    # 且排除 Fanbox/Pixiv 等明确平台块，避免把正文中的普通圆括号当 creator。
    m_creator = re.match(r"^\[([^\[\]]+)\](?=\s*\S)", value)
    if m_creator:
        inner = m_creator.group(1).strip()
        m_inner = re.fullmatch(r"(.+?)\s*\(([^()]+)\)", inner)
        if m_inner:
            group = m_inner.group(1).strip()
            author = m_inner.group(2).strip()
            if group and author and not _is_service_prefix_block(group) and group.casefold() not in NON_AUTHOR_MARKERS:
                fixed_block = f"[{group} ({author})]"
                value = fixed_block + value[m_creator.end():]

    # 开头一个或多个 [] 块结束后，与正文之间恰好一个半角空格。
    value = re.sub(r"^(\[[^\]]+\])(?=\S)", r"\1 ", value)
    # 相邻 [] 结构块之间一个空格。
    value = re.sub(r"\]\s*\[", "] [", value)
    # 标题正文紧贴已知尾部结构块时补空格。
    value = re.sub(
        r"(?<=\S)(?=\[(?:中国翻訳|AI翻译|AI翻译润色|DL版|Digital|AI Generated|AI\s*生成|無修正|无修正|Decensored|Uncensored|No Text|Full Color|フルカラー|彩色版|ENG|English|JP[／/]?EN|EN[／/]?JP|EP\.?\s*\d+[A-Za-z+.-]*)\])",
        " ",
        value,
        flags=re.IGNORECASE,
    )
    # 明确的结构化附加块后若紧贴正文/署名，补一个空格。
    value = re.sub(
        r"(\[(?:中国翻訳|AI翻译|AI翻译润色|DL版|Digital|AI Generated|AI\s*生成|無修正|无修正|半無修正|半无修正|Decensored|Uncensored|No Text|Full Color|フルカラー|彩色版|ENG|English|JP[／/]?EN|EN[／/]?JP|EP\.?\s*\d+[A-Za-z+.-]*)\])(?=\S)",
        r"\1 ", value, flags=re.IGNORECASE
    )
    # 汉化组块也属于尾部结构块；只对含关键词者补边界空格。
    value = re.sub(
        r"(?<=\S)(?=\[[^\]]*(?:汉化|漢化|翻译|翻譯|机翻|機翻)[^\]]*\])",
        " ",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\s+", " ", value).strip()
    return value


def normalize_name_structure(
    text: str,
    is_chinese_translation: bool,
    remove_event_prefix: bool,
    chinese_original: bool = False,
) -> tuple[str, list[str]]:
    value = sanitize_windows_name(text)
    actions: list[str] = []
    # 上传标题偶见 `[半無修正]中文` / `[DL版]中文` 粘连；中文身份另由结构化标记兜底。
    glued_cleaned = re.sub(
        r"(\[(?:半無修正|半无修正|無修正|无修正|Decensored|Uncensored|DL版|Digital)\])\s*中文(?=\s|$|\[)",
        r"\1",
        value,
        flags=re.IGNORECASE,
    )
    if glued_cleaned != value:
        value = glued_cleaned
        actions.append("拆分版本 metadata 后粘连的中文说明")
    value2, underscore_changed = _normalize_filename_underscores(value)
    if underscore_changed:
        value = value2
        actions.append("将文件名式英文标题下划线还原为空格")
    # [pixiv] 出现在作者块之后时视为发布/来源平台噪声；
    # 开头 [Pixiv] 作品集仍保留给“画集”识别，不与 Fanbox/Patreon 混用。
    pixiv_cleaned = re.sub(r"^(\[[^\]]+\])\s*\[pixiv\]\s*", r"\1 ", value, flags=re.IGNORECASE)
    if pixiv_cleaned != value:
        value = pixiv_cleaned.strip()
        actions.append("移除作者块后的 [pixiv] 平台来源标记")
    creator_space_cleaned = re.sub(
        r"^\[\s*([^\]]*?)\s*\]",
        lambda m: "[" + re.sub(r"\s+([)])", r"\1", re.sub(r"([(])\s+", r"\1", m.group(1).strip())) + "]",
        value,
    )
    if creator_space_cleaned != value:
        value = creator_space_cleaned
        actions.append("清理作者/社团块括号内侧的多余空格")
    before_event = value
    value = strip_event_prefix(value, remove=remove_event_prefix)
    if value != before_event:
        actions.append("按设置移除开头活动/展会标记")
    value, anthology_changed = _normalize_anthology_prefix(value)
    if anthology_changed:
        actions.append("将 [Anthology]/[雑誌] 规范为 [アンソロジー]")
    value, marker_actions = _canonicalize_known_blocks(value, is_chinese_translation, chinese_original)
    actions.extend(marker_actions)
    value, anthology_changed_after = _normalize_anthology_prefix(value)
    if anthology_changed_after:
        actions.append("将 [Anthology]/[雑誌] 规范为 [アンソロジー]")
    spaced = _normalize_structure_spacing(value)
    if spaced != value:
        actions.append("规范结构块之间的空格")
    return spaced, _dedupe(actions)


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    seen = set()
    for value in values:
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


def _language_set_key(content: str) -> str:
    """识别中/英/日/韩组合语言块，用于跨语言写法去重。"""
    value = unicodedata.normalize("NFKC", content or "").strip().casefold()
    if not value:
        return ""
    mapping = {
        "japanese": "japanese", "jp": "japanese", "日本語": "japanese",
        "english": "english", "eng": "english", "en": "english", "英語": "english",
        "korean": "korean", "kr": "korean", "韓国語": "korean", "韩国语": "korean",
        "chinese": "chinese", "中文": "chinese", "中国語": "chinese", "中国语": "chinese",
    }
    parts = [x.strip() for x in re.split(r"[\s,，、/／|｜+＆&・·]+", value) if x.strip()]
    languages = [mapping[x] for x in parts if x in mapping]
    if len(languages) < 2 or len(languages) != len(parts):
        return ""
    return "language-set:" + "+".join(sorted(set(languages)))


def _semantic_marker_key(content: str) -> str:
    c = content.strip()
    cf = c.casefold()
    language_set = _language_set_key(c)
    if language_set:
        return language_set
    if _is_chinese_marker_text(c) or _contains_chinese_language_semantics(c):
        return "language:chinese"
    if _is_ai_translation_polished_text(c):
        return "translation:ai-polished"
    if _is_ai_translation_text(c):
        return "translation:ai"
    if cf in {"机翻", "機翻"}:
        return "translation:machine"
    if cf in {"dl版", "digital"}:
        return "source:digital"
    services = _service_components(c)
    if services:
        return "source:" + "+".join(sorted(services))
    if cf in {"ai generated", "ai-generated", "aigenerated", "ai生成", "ai 生成"}:
        return "content:ai-generated"
    if cf in {"無修正", "无修正", "uncensored"}:
        return "censor:uncensored"
    if cf == "decensored":
        return "censor:decensored"
    if cf in {"半無修正", "半无修正"}:
        return "censor:partial"
    if INCOMPLETE_MARKER_RE.fullmatch(c):
        return "content:incomplete"
    if cf in {"no text", "textless"}:
        return "language:no-text"
    if cf in {"japanese", "jp"} or c == "日本語":
        return "language:japanese"
    if cf in {"english", "eng", "en"} or c == "英語":
        return "language:english"
    if cf in {"ongoing"} or c in {"進行中", "进行中"}:
        return "progress:ongoing"
    if cf in {"complete", "completed", "completed version"} or c in {"完結", "完结", "完整版"}:
        return "progress:complete"
    compact = re.sub(r"\s+", "", c).upper().replace("／", "/")
    if compact in {"JPEN", "JP/EN", "ENJP", "EN/JP", "日本語・英語", "日本語、英語", "日本語/英語", "JAPANESE/ENGLISH"}:
        return "language:jp-en"
    if c in {"続", "續"} or cf == "zoku":
        return "series:continuation"
    if cf in {"full color", "フルカラー", "彩色版"}:
        return "color:full"
    m_date = re.fullmatch(r"(20\d{2})(?:年|[-/.])(\d{1,2})(?:月|[-/.])(\d{1,2})(?:日)?", c)
    if m_date:
        return f"date:{int(m_date.group(1)):04d}-{int(m_date.group(2)):02d}-{int(m_date.group(3)):02d}"
    if _is_henan_dialect_text(c):
        return "dialect:henan"
    if cf in {"fanbox", "patreon", "fantia", "pixiv", "gumroad", "booth"}:
        return "source:" + cf
    m_series = re.fullmatch(r"(?:vol(?:ume)?|part|ep(?:isode)?|ch(?:apter)?)\.?\s*(\d+(?:\.\d+)?)", c, re.IGNORECASE)
    if m_series:
        label = re.match(r"[A-Za-z]+", c).group(0).casefold()
        if label.startswith("vol"):
            label = "vol"
        elif label.startswith("part"):
            label = "part"
        elif label.startswith("ep"):
            label = "ep"
        elif label.startswith("ch"):
            label = "ch"
        return f"series:{label}:{m_series.group(1)}"
    if _is_translation_group_text(c):
        normalized_group = unicodedata.normalize("NFKC", cf)
        normalized_group = re.sub(r"[\s_&＆|｜/／+]+", "", normalized_group)
        return "translation-group:" + normalized_group
    return "literal:" + cf


def _dedupe_structured_metadata(text: str) -> tuple[str, bool]:
    """最终输出兜底：对明确 metadata / 进度块做语义去重。

    中文翻译与方言使用独立语义槽位；例如
    [中国翻訳・河南話] 会先拆为 [中国翻訳] [河南話]。
    """
    value = text or ""
    before_split = value
    value = re.sub(
        r"[\[【［(（]\s*(?:中国翻訳|中国翻译|中国翻譯|中國翻译|中國翻譯|中文翻译|中文翻譯)[・·\s]+(?:河南話|河南话|Henan dialect)\s*[\]】］)）]",
        f"{CHINESE_MARKER} [河南話]",
        value,
        flags=re.IGNORECASE,
    )
    seen: set[str] = set()
    changed = value != before_split

    chinese_contents = [
        m.group("content").strip()
        for m in BRACKET_BLOCK_RE.finditer(value)
        if _contains_chinese_language_semantics(m.group("content"))
    ]
    chinese_kept = False

    def repl(m: re.Match) -> str:
        nonlocal changed, chinese_kept
        raw = m.group(0)
        content = m.group("content").strip()

        if _contains_chinese_language_semantics(content):
            if chinese_kept:
                changed = True
                return ""
            chinese_kept = True
            canonical = CHINESE_MARKER if _is_chinese_marker_text(content) or GENERIC_CHINESE_LANGUAGE_BLOCK_RE.fullmatch(content) else raw
            if canonical != raw:
                changed = True
            return canonical

        key = ""
        semantic_key = _semantic_marker_key(content)
        if PROGRESS_MARKER_RE.fullmatch(content):
            key = semantic_key
        elif (
            _is_ai_translation_polished_text(content)
            or _is_ai_translation_text(content)
            or _is_translation_group_text(content)
            or _is_special_marker_text(content)
            or semantic_key.startswith("date:")
            or semantic_key in {"language:jp-en", "language:japanese", "language:english", "language:no-text", "dialect:henan", "series:continuation", "content:incomplete"}
            or semantic_key.startswith("language-set:")
            or semantic_key.startswith("series:")
            or semantic_key.startswith("source:")
        ):
            key = semantic_key
        if not key:
            return raw
        if key in seen:
            changed = True
            return ""
        seen.add(key)
        return raw

    value2 = BRACKET_BLOCK_RE.sub(repl, value)
    if re.search(r"[（(]\s*fanbox\s*版\s*[)）]", value2, re.IGNORECASE):
        value2 = re.sub(r"\s*\[Fanbox\]", "", value2, flags=re.IGNORECASE)
    # 统一 anthology 后，不允许旧 [雑誌]/[Anthology] 又作为 creator/普通块残留。
    value2 = re.sub(r"^\s*\[アンソロジー\]\s*\[(?:雑誌|Anthology)\]\s*", "[アンソロジー] ", value2, flags=re.IGNORECASE)
    # 中文翻译是主语言状态，方言说明固定跟在其后，便于后续规则/人工阅读。
    value2 = re.sub(r"\[河南話\]\s*\[中国翻訳\]", "[中国翻訳] [河南話]", value2)
    value2 = re.sub(r"\s+", " ", value2).strip()
    return value2, changed or value2 != value


def _extract_trailing_structured_markers(text: str) -> tuple[str, list[str]]:
    """从末尾取出 metadata 块。

    已知语言/版本/译者会规范化；末尾未知的方括号块也作为“不透明 metadata”保留。
    这样 [DiamondDogs]、[translated by xxx] 等不会在 title_jpn 重建时丢失；
    Complete/Completed 属于默认完整状态，会在提取时直接丢弃。
    未知圆括号仍视为标题结构并停止提取。
    """
    value = (text or "").strip()
    markers_rev: list[str] = []
    while value:
        m = re.search(r"\s*(\[[^\[\]]+\]|【[^【】]+】|［[^［］]+］|\([^()]{1,100}\)|（[^（）]{1,100}）)\s*$", value)
        if not m:
            break
        raw = m.group(1)
        content = raw[1:-1].strip()
        square_like = raw[0] in "[［"
        if EXPLICIT_CHINESE_ORIGINAL_BLOCK_RE.fullmatch(content):
            marker = f"[{content}]"
        elif _is_chinese_marker_text(content):
            marker = CHINESE_MARKER
        elif _contains_chinese_language_semantics(content):
            marker = f"[{content}]"
        elif _is_ai_translation_polished_text(content):
            marker = AI_TRANSLATION_POLISHED_MARKER
        elif _is_ai_translation_text(content):
            marker = AI_TRANSLATION_MARKER
        elif _is_translation_group_text(content):
            marker = f"[{content}]"
        elif PROGRESS_MARKER_RE.fullmatch(content):
            canonical_progress = _canonical_progress_marker_text(content)
            marker = f"[{canonical_progress}]" if canonical_progress else ""
        elif _is_special_marker_text(content):
            marker = f"[{_canonical_special_marker_text(content)}]"
        elif square_like:
            marker = f"[{content}]"
        else:
            break
        if marker:
            markers_rev.append(marker)
        value = value[:m.start()].rstrip()
    markers_rev.reverse()
    return value, markers_rev


def _find_progress_markers(text: str) -> list[str]:
    result: list[str] = []
    for m in BRACKET_BLOCK_RE.finditer(text or ""):
        content = m.group("content").strip()
        if PROGRESS_MARKER_RE.fullmatch(content):
            # 进度状态保留原来的括号风格，避免擅自改成方括号。
            result.append(m.group(0))
    return result


def _find_translation_groups(text: str) -> list[str]:
    result: list[str] = []
    for m in BRACKET_BLOCK_RE.finditer(text or ""):
        c = m.group("content").strip()
        if _is_translation_group_text(c):
            result.append(f"[{c}]")
    return result


def _merge_markers(current: str, db_title: str, title_jpn: str, is_chinese: bool) -> list[str]:
    """组合尾部标记。

    原则：
    - 当前文件中的合法标记最优先；已约定同义 metadata 会规范成统一写法。
    - 使用 title_jpn 作为原版主体时，title_jpn 的版本/修正标记优先于 title。
    - title 主要用来补更具体的汉化组/译者信息，不把不同来源的冲突修正状态混在一起。
    """
    _, cur = _extract_trailing_structured_markers(current)
    _, db = _extract_trailing_structured_markers(db_title)
    _, jp = _extract_trailing_structured_markers(title_jpn)

    group_candidates = _find_translation_groups(current) + _find_translation_groups(db_title) + _find_translation_groups(title_jpn)
    best_group = max(group_candidates, key=lambda x: len(x), default="")

    combined: list[str] = []
    semantic_seen: set[str] = set()
    # Digital/DL版 是可选 metadata：只有当前本地名称原本就有时才允许保留。
    # 数据库 title/title_jpn 单独出现 Digital/DL版 不再触发“补全”。
    current_has_digital = any(
        _semantic_marker_key((m[1:-1] if m.startswith("[") and m.endswith("]") else m)) == "source:digital"
        for m in cur
    )

    def add(marker: str):
        content = marker[1:-1] if marker.startswith("[") and marker.endswith("]") else marker
        key = _semantic_marker_key(content)
        if key == "language:chinese" and key in semantic_seen:
            # 已有 中国語/中国语 时优先保留；它是用户明确要求保真的原语言标记。
            for i, old_marker in enumerate(combined):
                if not (old_marker.startswith("[") and old_marker.endswith("]")):
                    continue
                old_content = old_marker[1:-1]
                if _semantic_marker_key(old_content) != "language:chinese":
                    continue
                old_original = bool(EXPLICIT_CHINESE_ORIGINAL_BLOCK_RE.fullmatch(old_content))
                new_original = bool(EXPLICIT_CHINESE_ORIGINAL_BLOCK_RE.fullmatch(content))
                if old_original:
                    return
                if new_original or len(content) > len(old_content):
                    combined[i] = marker
                return
            return
        if key.startswith("translation-group:"):
            if key in semantic_seen:
                return
        elif key in semantic_seen:
            return
        semantic_seen.add(key)
        combined.append(marker)

    for marker in cur:
        add(marker)

    if is_chinese:
        add(CHINESE_MARKER)

    if best_group:
        generic_idx = None
        best_content = best_group[1:-1]
        for i, marker in enumerate(combined):
            if not (marker.startswith("[") and marker.endswith("]")):
                continue
            content = marker[1:-1]
            if _is_translation_group_text(content):
                if len(best_content) > len(content) and content in best_content:
                    generic_idx = i
                    break
        if generic_idx is not None:
            old = combined.pop(generic_idx)
            semantic_seen.discard(_semantic_marker_key(old[1:-1]))
        add(best_group)

    # 与所选原版主体同来源的特殊标记优先。
    preferred = jp if title_jpn else db
    for marker in preferred:
        content = marker[1:-1] if marker.startswith("[") and marker.endswith("]") else marker
        if _semantic_marker_key(content) == "source:digital" and not current_has_digital:
            continue
        add(marker)

    # 次级 title 主要补翻译信息；同时保留末尾“不透明方括号 metadata”。
    # 后者用于 DiamondDogs / translated by xxx / complete 等没有关键词但明确处在 E-H 尾部结构区的块。
    # 已知 Decensored / 無修正 / Digital 等版本状态仍不从次级 title 强行混入，避免版本冲突。
    for marker in db:
        if not (marker.startswith("[") and marker.endswith("]")):
            continue
        content = marker[1:-1]
        key = _semantic_marker_key(content)
        if (
            _is_ai_translation_text(content)
            or _is_translation_group_text(content)
            or _is_chinese_marker_text(content)
            or _contains_chinese_language_semantics(content)
            or key.startswith("literal:")
        ):
            add(marker)

    for progress in _find_progress_markers(current):
        if progress not in combined:
            combined.append(progress)

    return combined


def _translation_metadata_markers(*texts: str, include_chinese_language_marker: bool = True) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for text in texts:
        # 已结构化块
        for m in BRACKET_BLOCK_RE.finditer(text or ""):
            c = m.group("content").strip()
            marker = ""
            if _is_ai_translation_polished_text(c):
                marker = AI_TRANSLATION_POLISHED_MARKER
            elif _is_ai_translation_text(c):
                marker = AI_TRANSLATION_MARKER
            elif _is_translation_group_text(c):
                marker = f"[{c}]"
            elif EXPLICIT_CHINESE_ORIGINAL_BLOCK_RE.fullmatch(c):
                marker = f"[{c}]"
            elif _is_chinese_marker_text(c):
                if TRANSLATED_CHINESE_BLOCK_RE.fullmatch(c) or include_chinese_language_marker:
                    marker = CHINESE_MARKER
            elif GENERIC_CHINESE_LANGUAGE_BLOCK_RE.fullmatch(c) and include_chinese_language_marker:
                marker = CHINESE_MARKER
            if marker:
                key = _semantic_marker_key(marker[1:-1])
                if key not in seen:
                    seen.add(key)
                    result.append(marker)
        # 未加括号的 AI 翻译说明
        if AI_TRANSLATION_POLISHED_RE.search(text or ""):
            key = _semantic_marker_key(AI_TRANSLATION_POLISHED_MARKER[1:-1])
            if key not in seen:
                seen.add(key); result.append(AI_TRANSLATION_POLISHED_MARKER)
        elif AI_TRANSLATION_TEXT_RE.search(text or ""):
            key = _semantic_marker_key(AI_TRANSLATION_MARKER[1:-1])
            if key not in seen:
                seen.add(key); result.append(AI_TRANSLATION_MARKER)
    return result

def _append_missing_translation_metadata(
    name: str,
    current_name: str,
    record_title: str,
    title_jpn: str,
    is_chinese_translation: bool,
) -> tuple[str, list[str], list[str]]:
    """补确定性的翻译 metadata；弱对应的翻译组只作为候选，不自动写入。"""
    value = name
    actions: list[str] = []
    skipped_groups: list[str] = []
    markers = _translation_metadata_markers(record_title, title_jpn, include_chinese_language_marker=is_chinese_translation)
    if is_chinese_translation and CHINESE_MARKER not in markers:
        markers.append(CHINESE_MARKER)

    jp_alignment = False
    if title_jpn:
        a = _review_body_key(current_name)
        b = _review_body_key(title_jpn)
        jp_alignment = bool(a and b and SequenceMatcher(None, a, b).ratio() >= 0.72)

    def group_source_count(key: str) -> int:
        count = 0
        for src in (record_title, title_jpn):
            for g in _find_translation_groups(src or ""):
                if _semantic_marker_key(g[1:-1]) == key:
                    count += 1
                    break
        return count

    def group_source_aligns(key: str) -> bool:
        cur = _review_body_key(current_name)
        if not cur:
            return False
        for src in (record_title, title_jpn):
            if not src:
                continue
            groups = {_semantic_marker_key(g[1:-1]) for g in _find_translation_groups(src)}
            if key not in groups:
                continue
            body = _review_body_key(src)
            if body and SequenceMatcher(None, cur, body).ratio() >= 0.72:
                return True
        return False

    for marker in markers:
        content = marker[1:-1]
        key = _semantic_marker_key(content)
        existing_keys = {_semantic_marker_key(m.group("content").strip()) for m in BRACKET_BLOCK_RE.finditer(value)}
        if key in existing_keys:
            continue
        if key.startswith("translation-group:"):
            # 有 title_jpn 且当前与其主体能对应时，可从同 Gallery 的 title 补译者；
            # 无 title_jpn 时则要求该翻译组来源标题本身与当前主体足够相似。
            reliable_group = group_source_count(key) >= 2 or jp_alignment or group_source_aligns(key)
            if not reliable_group:
                skipped_groups.append(marker)
                continue
        value = f"{value.rstrip()} {marker}".strip()
        if key.startswith("translation-group:"):
            actions.append("从数据库标题补充翻译组/译者标记")
        elif key.startswith("translation:ai"):
            actions.append("从数据库标题保留 AI/机器翻译方式标记")
    return _normalize_structure_spacing(value), _dedupe(actions), _dedupe(skipped_groups)

def _join_core_markers(core: str, markers: list[str]) -> str:
    core = _normalize_structure_spacing(core.strip())
    if not markers:
        return core
    return _normalize_structure_spacing(f"{core} {' '.join(markers)}")


def _ensure_chinese_marker(text: str) -> tuple[str, bool]:
    value = text or ""
    changed = False

    def repl(m: re.Match) -> str:
        nonlocal changed
        content = m.group("content").strip()
        if _is_chinese_marker_text(content) or GENERIC_CHINESE_LANGUAGE_BLOCK_RE.fullmatch(content):
            if m.group(0) != CHINESE_MARKER:
                changed = True
            return CHINESE_MARKER
        return m.group(0)

    value = BRACKET_BLOCK_RE.sub(repl, value)
    if not any(_contains_chinese_language_semantics(m.group("content")) for m in BRACKET_BLOCK_RE.finditer(value)):
        value = f"{value.rstrip()} {CHINESE_MARKER}".strip()
        changed = True
    return _normalize_structure_spacing(value), changed

def _ensure_ai_generated_marker(text: str) -> tuple[str, bool]:
    value = text or ""
    for m in BRACKET_BLOCK_RE.finditer(value):
        if _semantic_marker_key(m.group("content").strip()) == "content:ai-generated":
            return _normalize_structure_spacing(value), False
    return _normalize_structure_spacing(f"{value.rstrip()} [AI Generated]"), True



def _ensure_uncensored_marker(text: str) -> tuple[str, bool]:
    """E-H other:uncensored 只在名称没有任何修正状态时补 `[無修正]`。

    `[Decensored]`、`[半無修正]` 都是独立版本身份；已有时不能被泛化 tag 覆盖。
    """
    value = text or ""
    states = _censor_states(value)
    if states:
        return value, False
    return _normalize_structure_spacing(f"{value.rstrip()} [無修正]"), True

def _drop_surplus_closing_delimiters(text: str) -> tuple[str, bool]:
    """只删除明显多余的结构方括号右半边；圆括号/引号不自动猜测。"""
    value = text or ""
    pairs = {"[": "]", "【": "】", "［": "］"}
    closes = {v: k for k, v in pairs.items()}
    stack: list[str] = []
    out: list[str] = []
    changed = False
    for ch in value:
        if ch in pairs:
            stack.append(ch)
            out.append(ch)
        elif ch in closes:
            if not stack:
                changed = True
                continue
            if stack[-1] == closes[ch]:
                stack.pop()
            out.append(ch)
        else:
            out.append(ch)
    if not changed:
        return value, False
    return "".join(out).strip(), True


def _has_unbalanced_delimiters(text: str) -> bool:
    """轻量结构风险检查；先统一全/半角括号，再检查明确成对结构。"""
    value = normalize_fullwidth_latin_digits_brackets(text or "")
    opens = {"[": "]", "【": "】", "(": ")", "（": "）", "『": "』", "「": "」", "“": "”"}
    closes = {v: k for k, v in opens.items()}
    if value.count("＂") % 2:
        return True
    stack: list[str] = []
    for ch in value:
        if ch in opens:
            stack.append(ch)
        elif ch in closes:
            if not stack or stack[-1] != closes[ch]:
                return True
            stack.pop()
    return bool(stack)


def _ensure_no_text_marker(text: str) -> tuple[str, bool]:
    value = text or ""
    if _has_no_text_block(value):
        return value, False
    return _normalize_structure_spacing(f"{value.rstrip()} {NO_TEXT_MARKER}"), True


def _normalize_compare(text: str) -> str:
    text = sanitize_windows_name(text).casefold()
    return re.sub(r"\s+", " ", text).strip()


def _author_like_leading_bracket(text: str) -> str:
    value = _strip_non_creator_prefixes(text)
    m = LEADING_BRACKET_RE.match(value)
    if not m:
        return ""
    block = m.group(1).strip()
    if block.casefold() in NON_AUTHOR_MARKERS or _is_service_prefix_block(block):
        return ""
    return block


def _creator_bracket_after_leading_parenthesis(text: str) -> tuple[str, str]:
    """返回开头圆括号后的 creator 候选及圆括号类型。"""
    value = (text or "").strip()
    raw, kind = classify_leading_parenthesis(value, [], [])
    if not raw or not value.startswith(raw):
        return "", kind
    remainder = value[len(raw):].lstrip()
    return _author_like_leading_bracket(remainder), kind


def _author_like_creator_after_convention(
    text: str,
    artist_tags: list[str],
    group_tags: list[str],
    known_creator: str = "",
) -> str:
    """识别活动/平台/装饰前缀之后的 creator。

    先跳过明确活动/平台标记；普通 [] creator 可直接读取。
    对 【Creator】 / (Creator) 这类非标准写法只在 tags 或本地已知 creator 能佐证时采用。
    """
    original = (text or "").strip()
    value = _strip_non_creator_prefixes(original)

    m = LEADING_BRACKET_RE.match(value)
    if m:
        block = m.group(1).strip()
        if block.casefold() not in NON_AUTHOR_MARKERS and not _is_service_prefix_block(block):
            return block

    m = re.match(r"^【([^【】]{1,120})】\s*", value)
    if m:
        block = m.group(1).strip()
        if ((known_creator and _normalize_compare(block) == _normalize_compare(known_creator))
                or _creator_block_matches_tags(block, artist_tags, group_tags)):
            return block

    m = re.match(r"^\(([^()]{1,120})\)\s*", value)
    if m:
        block = m.group(1).strip()
        if ((known_creator and _normalize_compare(block) == _normalize_compare(known_creator))
                or _creator_block_matches_tags(block, artist_tags, group_tags)):
            return block

    # 未知活动名（例如专题展名称）仍可通过“活动后 [] creator + tags”识别。
    raw, kind = classify_leading_parenthesis(original, artist_tags, group_tags)
    if raw and original.startswith(raw):
        remainder = original[len(raw):].lstrip()
        after = _author_like_leading_bracket(remainder)
        if after and (kind == "CONVENTION"
                      or (known_creator and _normalize_compare(after) == _normalize_compare(known_creator))
                      or _creator_block_matches_tags(after, artist_tags, group_tags)):
            return after
    return ""

def _page_diff(filecount, page_count) -> tuple[int | None, float | None, str]:
    if not isinstance(filecount, int) or not isinstance(page_count, int):
        return None, None, ""
    if filecount <= 0 or page_count <= 0:
        return None, None, ""
    diff = abs(filecount - page_count)
    ratio = diff / max(filecount, page_count)
    if ratio >= 0.50 and diff >= 5:
        level = "极大"
    elif (ratio >= 0.25 and diff >= 5) or (ratio >= 0.20 and diff >= 10) or (ratio >= 0.15 and diff >= 20):
        level = "较大"
    elif ratio >= 0.10 and diff >= 3:
        level = "轻微"
    else:
        level = "正常范围"
    return diff, ratio, level


def _is_cosplay_category(category: str) -> bool:
    return (category or "").strip().casefold() == "cosplay"


def _is_strong_nonstandard(record, current: str, ai_generated: bool) -> bool:
    """确定作品是否具有“画集”属性。

    Image Set 默认就是画集；Artist CG / Misc 必须有 collection/archive/合集等
    强集合证据。平台名、单个日期、页数或 AI Generated 都不能单独把正规单次
    上传判成画集。
    """
    value = current or ""
    category = (record.category or "").strip().casefold()
    if category == "image set":
        return True
    strong_collection = bool(COLLECTION_RE.search(value))
    tag_data = parse_tags(getattr(record, "tags_raw", "{}"))
    other_tag_values = {x.casefold() for x in _list_tag(tag_data, "other")}
    strong_collection_tag = bool(other_tag_values & {"compilation", "artbook"})
    date_range = bool(re.search(
        r"20\d{2}(?:[年./-]\d{1,2})?\s*(?:[-~～—至到]|から)\s*20\d{2}(?:[年./-]\d{1,2})?",
        value, re.IGNORECASE,
    ))
    if category in {"artist cg", "misc"}:
        return bool(strong_collection or strong_collection_tag or date_range)
    return False


_PROGRESS_TEXT_PATTERNS = [
    re.compile(r"第[0-9一二三四五六七八九十百零〇两兩壹贰貳叁參肆伍陆陸柒捌玖拾]+(?:話|话|章)\s*[-~～ー—至到]\s*第?[0-9一二三四五六七八九十百零〇两兩壹贰貳叁參肆伍陆陸柒捌玖拾]+(?:話|话|章)", re.IGNORECASE),
    re.compile(r"\b(?:ch(?:apter)?|part|vol(?:ume)?|ep(?:isode)?)\.?\s*\d+\s*[-~～ー—]\s*\d+\b", re.IGNORECASE),
    re.compile(r"\bv\s*20\d{2}[-_.]\d{1,2}[-_.]\d{1,2}\b", re.IGNORECASE),
    re.compile(r"(?<![\d-])\d{1,3}\s*[-~～ー—至到]\s*\d{1,3}(?![\d-])", re.IGNORECASE),
]


def _progress_signatures(text: str) -> set[str]:
    value = normalize_fullwidth_latin_digits_brackets(text or "")
    found: set[str] = set()
    for m in BRACKET_BLOCK_RE.finditer(value):
        content = m.group("content").strip()
        if PROGRESS_MARKER_RE.fullmatch(content):
            semantic = _semantic_marker_key(content)
            found.add("state:" + (semantic.split(":", 1)[1] if semantic.startswith("progress:") else re.sub(r"\s+", "", content.casefold())))
    for pattern in _PROGRESS_TEXT_PATTERNS:
        for m in pattern.finditer(value):
            sig = re.sub(r"\s+", "", m.group(0).casefold())
            # 5~8 / 5～8 / 5—8 等只是分隔符写法差异，不应误判为不同进度。
            sig = re.sub(r"[~～ー—至到]", "-", sig)
            found.add("range:" + sig)
    return found


def _has_progress_mismatch(current: str, db_title: str, title_jpn: str) -> tuple[bool, list[str], list[str]]:
    local = _progress_signatures(current)
    remote_text = title_jpn or db_title or ""
    remote = _progress_signatures(title_jpn) or _progress_signatures(db_title)

    if not local:
        return False, [], sorted(remote)
    mismatch = not bool(local & remote)
    return mismatch, sorted(local), sorted(remote)


def _leading_event_value(text: str) -> str:
    m = EVENT_PREFIX_RE.match((text or "").strip())
    return re.sub(r"\s+", "", m.group(0)).casefold() if m else ""


def _series_identity_tokens(text: str) -> set[str]:
    """提取作品身份级编号；用于发现 5 -> 5.5 / Vol.2 -> Vol.3 等变化。

    这里不把所有数字都当版本，只取标题尾部（可在原作括号之前）或明确 Part/Vol/EP/Ch 标签。
    """
    core, _ = _extract_trailing_structured_markers(text or "")
    core = EVENT_PREFIX_RE.sub("", core).strip()
    core = re.sub(r"^\[[^\]]+\]\s*", "", core, count=1).strip()
    # 去掉末尾可能的原作/说明圆括号，仅用于查看它前面的系列编号。
    core_no_tail_paren = re.sub(r"\s*[（(][^()（）]{1,120}[)）]\s*$", "", core).strip()
    found: set[str] = set()
    for m in re.finditer(r"\b(Vol(?:ume)?|Part|EP(?:isode)?|Ch(?:apter)?)\.?\s*(\d+(?:\.\d+)?)\b", core, re.IGNORECASE):
        label = m.group(1).casefold()
        label = "vol" if label.startswith("vol") else "part" if label.startswith("part") else "ep" if label.startswith("ep") else "ch"
        found.add(f"{label}:{m.group(2)}")
    for m in re.finditer(r"第\s*(\d+(?:\.\d+)?)\s*(話|话|章|巻|卷|編|篇)", core):
        found.add(f"jp:{m.group(2)}:{m.group(1)}")
    for m in re.finditer(r"(?:シリーズ|Series)\s*#?\s*(\d+(?:\.\d+)?)", core, re.IGNORECASE):
        found.add(f"series:{m.group(1)}")
    m = re.search(r"(?:^|\s|[-：:])(?P<n>\d{1,3}(?:\.\d+)?)\s*$", core_no_tail_paren)
    if m:
        found.add("tail:" + m.group("n"))
    return found


def _series_identity_conflict(current: str, source: str) -> bool:
    local = _series_identity_tokens(current)
    remote = _series_identity_tokens(source)
    if not local:
        return False
    if local & remote:
        return False
    # 本地明确写了 Series/Vol/Part/EP/第N話 等身份编号，而来源完全缺失时，不允许直接删掉。
    explicit_local = {x for x in local if not x.startswith("tail:")}
    if explicit_local and not remote:
        return True
    if not remote:
        return False
    # 两边都有编号但不同：只有其余正文高度一致时才认作同标题身份冲突。
    a = re.sub(r"\d+(?:\.\d+)?", "#", _review_body_key(current))
    b = re.sub(r"\d+(?:\.\d+)?", "#", _review_body_key(source))
    return bool(a and b and SequenceMatcher(None, a, b).ratio() >= 0.88)


def _embedded_title_number_conflict(current: str, source: str) -> bool:
    """发现类似 `魔法少女5☆ -> 魔法少女` 的正文编号丢失。

    只在去掉数字后正文仍高度相似时触发，4 位年份不作为作品编号。
    """
    if not current or not source:
        return False
    a = _review_body_raw(current)
    b = _review_body_raw(source)
    if not a or not b:
        return False
    nums_a = {x for x in re.findall(r"(?<!\d)(\d{1,3}(?:\.\d+)?)(?!\d)", a)}
    nums_b = {x for x in re.findall(r"(?<!\d)(\d{1,3}(?:\.\d+)?)(?!\d)", b)}
    if not nums_a or nums_a == nums_b:
        return False
    a0 = re.sub(r"\d{1,3}(?:\.\d+)?", "#", a)
    b0 = re.sub(r"\d{1,3}(?:\.\d+)?", "#" if nums_b else "", b)
    a0 = re.sub(r"[\s☆★~～_\-—:：|｜/／]+", "", a0)
    b0 = re.sub(r"[\s☆★~～_\-—:：|｜/／]+", "", b0)
    return bool(a0 and b0 and SequenceMatcher(None, a0, b0).ratio() >= 0.75)


def _censor_states(text: str) -> set[str]:
    states: set[str] = set()
    for m in BRACKET_BLOCK_RE.finditer(text or ""):
        key = _semantic_marker_key(m.group("content").strip())
        if key.startswith("censor:"):
            states.add(key)
    return states


def _source_is_generic_collection_risk(
    current: str,
    source: str,
    source_has_collection_tag: bool = False,
) -> bool:
    """防止 Gallery 合集/范围标题覆盖本地具体章节。

    风险既可能是“来源更短更泛”，也可能是上传者在具体作品名后追加“合集”或 `+ 另一作品`。
    """
    if not current or not source:
        return False
    source_cf = unicodedata.normalize("NFKC", source).casefold()
    current_cf = unicodedata.normalize("NFKC", current).casefold()
    generic = bool(
        source_has_collection_tag
        or SERVICE_RE.search(source_cf)
        or COLLECTION_RE.search(source_cf)
        or re.search(r"合集|まとめ|総集|collection|compilation", source_cf, re.IGNORECASE)
    )
    cur_body = _body_without_leading_creator_and_tail(current)
    src_body = _body_without_leading_creator_and_tail(source)
    if not cur_body or not src_body:
        return False
    cur_key = _normalize_compare(cur_body)
    src_key = _normalize_compare(src_body)
    # 本地具体作品常在末尾保留 (原作)，而上传者合集标题可能把它删掉后追加“合集”。
    # 做范围比较时允许临时忽略这一末尾圆括号，但不修改真实输出。
    cur_scope_body = re.sub(r"\s*[（(][^()（）]{1,120}[)）]\s*$", "", cur_body).strip()
    cur_scope_key = _normalize_compare(cur_scope_body) or cur_key
    sim = SequenceMatcher(None, cur_key, src_key).ratio()

    # 本地是具体编号/章节，来源是 01~23 一类合集范围。只有来源同时具备
    # compilation/collection 强证据，且两边仍共享足够正文时才触发。
    source_ranges = re.findall(r"(?<!\d)(\d{1,3})\s*[-~～—至到]\s*(\d{1,3})(?!\d)", source_cf)
    current_is_range = bool(re.search(r"(?<!\d)\d{1,3}\s*[-~～—至到]\s*\d{1,3}(?!\d)", current_cf))
    current_numbers = {int(x) for x in re.findall(r"(?<!\d)(\d{1,3})(?!\d)", current_cf)}
    if generic and source_ranges and not current_is_range and current_numbers:
        shared = SequenceMatcher(
            None,
            re.sub(r"\d+", "", cur_scope_key),
            re.sub(r"\d+", "", src_key),
        ).ratio()
        in_range = any(int(a) <= n <= int(b) for a, b in source_ranges for n in current_numbers)
        if in_range and shared >= 0.28:
            return True

    if generic:
        # 旧场景：来源是更短、更泛的合集总标题。
        if len(cur_body) >= max(12, int(len(src_body) * 1.35)) and sim < 0.68:
            return True
        # 新场景：来源在本地具体标题后追加“合集/作品集”等上传说明。
        if not COLLECTION_RE.search(current_cf) and cur_scope_key and cur_scope_key in src_key and len(src_key) > len(cur_scope_key):
            return True

    # `A -> A + B` 很可能是 Gallery 合并上传范围扩大，不自动当成标题补全。
    if ("+" in source or "＋" in source) and not ("+" in current or "＋" in current):
        compact_cur = re.sub(r"\s+", "", cur_key)
        compact_src = re.sub(r"\s+", "", src_key)
        if compact_cur and compact_cur in compact_src:
            return True
    return False


def _strip_extra_source_date_blocks(text: str, current: str, category: str) -> tuple[str, bool]:
    """非 Image Set 不把来源独有的单日上传时间导入正式名称。"""
    value = text or ""
    if (category or "").strip().casefold() == "image set":
        return value, False
    # 进行中、明确日期范围属于版本/内容范围证据，必须保留。
    if re.search(r"\bOngoing\b|進行中|进行中", value, re.IGNORECASE):
        return value, False
    if re.search(
        r"20\d{2}(?:[年./-]\d{1,2})?(?:[月./-]\d{1,2}日?)?\s*(?:[-~～—至到]|から)\s*20\d{2}",
        value, re.IGNORECASE,
    ):
        return value, False

    date_inner = re.compile(
        r"^20\d{2}(?:年\d{1,2}月\d{1,2}日?|[-/.]\d{1,2}[-/.]\d{1,2})$",
        re.IGNORECASE,
    )
    current_dates = {
        unicodedata.normalize("NFKC", m.group("content")).casefold()
        for m in BRACKET_BLOCK_RE.finditer(current or "")
        if date_inner.fullmatch(unicodedata.normalize("NFKC", m.group("content")).strip())
    }
    changed = False

    def repl(m: re.Match) -> str:
        nonlocal changed
        content = unicodedata.normalize("NFKC", m.group("content")).strip()
        if date_inner.fullmatch(content) and content.casefold() not in current_dates:
            changed = True
            return " "
        return m.group(0)

    cleaned = BRACKET_BLOCK_RE.sub(repl, value)
    return re.sub(r"\s+", " ", cleaned).strip(), changed


def _core_for_subtitle_compare(text: str) -> str:
    value, _ = _extract_trailing_structured_markers(text or "")
    value = EVENT_PREFIX_RE.sub("", value).strip()
    value = re.sub(r"^\s*\[[^\[\]]+\]\s*", "", value).strip()
    return re.sub(r"\s+", " ", value)


def _local_has_extra_title_text(local_name: str, source_title: str) -> bool:
    local_core = _core_for_subtitle_compare(local_name)
    source_core = _core_for_subtitle_compare(source_title)
    if not local_core or not source_core:
        return False
    a = local_core.casefold()
    b = source_core.casefold()
    if a == b or not a.startswith(b):
        return False

    # 只把真正的“正文/副标题增量”当成需要保护的内容。
    # (オリジナル)、[译者]、【署名】、(2) 这类纯结构尾块不应触发副标题警告。
    extra = local_core[len(source_core):].strip()
    if TRANSLATION_GROUP_HINT_RE.search(extra) or AI_TRANSLATION_TEXT_RE.search(extra):
        return False
    # 短尾巴也可能是正式版本/章节名，例如“垂涎編”“男性視点ver.”，不能因不足 6 字就删除。
    if re.search(r"(?:編|篇|視点|视点|ver\.?|版|第?\d+(?:話|话|章|巻|卷)|Vol\.?|Part\.?|EP\.?)", extra, re.IGNORECASE):
        return True
    extra = re.sub(r"\[[^\]]*\]|【[^】]*】|［[^］]*］|\([^()]*\)|（[^（）]*）", " ", extra)
    extra = re.sub(r"[\s~～_\-—:：|｜/／]+", "", extra)
    meaningful = re.findall(r"[A-Za-z0-9\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]", extra)
    return len(meaningful) >= 6


def _norm_entity(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").casefold())


def _norm_creator_literal(text: str) -> str:
    """比较 title/current 中的 creator 字面写法；保留日中韩文字，不用于罗马字 tag 映射。"""
    value = unicodedata.normalize("NFKC", text or "").casefold()
    return re.sub(r"[\s_\-・·•･.]+", "", value)


def _is_multi_creator_placeholder(text: str) -> bool:
    """E-H/同人标题里的“よろず / Various”是多作者/杂项占位，不按具体作者名处理。"""
    value = unicodedata.normalize("NFKC", text or "").strip().casefold()
    value = re.sub(r"\s+", " ", value)
    return value in {"よろず", "various", "various artists", "various artist"}


def _service_components(content: str) -> list[str]:
    """把 `[Fanbox Patreon]` / `[Fanbox ｜ Patreon]` 归到同一平台集合。"""
    value = unicodedata.normalize("NFKC", content or "").strip().casefold()
    services = {"fanbox", "patreon", "fantia", "pixiv", "gumroad", "booth"}
    tokens = re.findall(r"[a-z0-9]+", value)
    if not tokens or any(token not in services for token in tokens):
        return []
    # 除平台词之外只能有空格或常见分隔符，避免把普通标题误当平台块。
    residue = re.sub(r"\b(?:fanbox|patreon|fantia|pixiv|gumroad|booth)\b", "", value)
    if re.sub(r"[\s/|,+&_・·-]+", "", residue):
        return []
    return list(dict.fromkeys(tokens))


def _is_service_prefix_block(content: str) -> bool:
    """识别 [Pixiv] / [Fanbox/Pixiv] 等平台前缀，它们不是 creator。"""
    return bool(_service_components(content))


def _strip_non_creator_prefixes(text: str) -> str:
    """只为 creator 识别跳过明确活动/类别/平台前缀，不改变最终标题。"""
    value = (text or "").strip()
    value = EVENT_PREFIX_RE.sub("", value).lstrip()
    value = CATEGORY_DESCRIPTOR_RE.sub(" ", value).strip()
    while True:
        m = re.match(r"^(?:\[([^\[\]]{1,120})\]|【([^【】]{1,120})】|［([^［］]{1,120})］)\s*", value)
        if not m:
            break
        content = next((x for x in m.groups() if x is not None), "").strip()
        cf = content.casefold()
        if (
            cf in NON_AUTHOR_MARKERS
            or _is_service_prefix_block(content)
            or _is_special_marker_text(content)
            or _is_chinese_marker_text(content)
            or _is_translation_group_text(content)
        ):
            value = value[m.end():].lstrip()
            continue
        break
    return value


def _normalize_creator_dot_prefix(text: str, artist_tags: list[str], group_tags: list[str]) -> tuple[str, bool]:
    """规范少数汉化上传名：`作者·标题` -> `[作者] 标题`。

    只认中文间隔点 U+00B7 / bullet，且前缀必须能被 E-H artist/group tag 佐证；
    不处理日文标题常见的 `・`，避免把人物名/正文误拆成 creator。
    """
    value = (text or "").strip()
    # 允许明确活动/平台前缀后再出现 作者·标题，但前缀仍保留。
    prefix = ""
    event = EVENT_PREFIX_RE.match(value)
    if event:
        prefix = event.group(0).strip()
        value = value[event.end():].lstrip()
    m = re.match(r"^([^\[\]【】()（）]{1,80}?)\s*[·•]\s*(.+)$", value)
    if not m:
        return text, False
    creator = m.group(1).strip()
    body = m.group(2).strip()
    if not creator or not body or not _creator_block_matches_tags(creator, artist_tags, group_tags):
        return text, False
    rebuilt = f"[{creator}] {body}".strip()
    if prefix:
        rebuilt = f"{prefix} {rebuilt}".strip()
    return rebuilt, rebuilt != (text or "").strip()


def _creator_parts(block: str) -> tuple[str, str]:
    m = re.match(r"^(.*?)\s*\(([^()]*)\)\s*$", block or "")
    return (m.group(1).strip(), m.group(2).strip()) if m else ((block or "").strip(), "")


def _dedupe_structured_creator_token(text: str) -> tuple[str, bool]:
    """移除 creator 已结构化后，正文首尾再次出现的同一独立 token。

    拉丁 creator 只有带明确分隔符时才从正文开头移除，避免把
    `DarkFlame Anthology` 这类正式系列名误删；正文末尾的完全相同独立 token
    则可安全视为上传署名重复。
    """
    value = (text or "").strip()
    leading_non_creator: list[str] = []
    remainder = value
    m = LEADING_BRACKET_RE.match(remainder)
    while m:
        candidate = m.group(1).strip()
        if not (
            candidate.casefold() in NON_AUTHOR_MARKERS
            or _is_service_prefix_block(candidate)
            or _is_special_marker_text(candidate)
            or _is_chinese_marker_text(candidate)
            or _is_translation_group_text(candidate)
        ):
            break
        leading_non_creator.append(m.group(0).strip())
        remainder = remainder[m.end():].lstrip()
        m = LEADING_BRACKET_RE.match(remainder)
    if not m:
        return value, False
    block = m.group(1).strip()

    group, artist = _creator_parts(block)
    tokens = [x for x in dict.fromkeys([block, group, artist]) if x]
    body_with_tail = remainder[m.end():].strip()
    core, markers = _extract_trailing_structured_markers(body_with_tail)
    changed = False

    for token in sorted(tokens, key=len, reverse=True):
        escaped = re.escape(token)
        # CJK creator 紧接正文时也常以空格充当独立分隔；拉丁 creator 必须有标点分隔。
        if _looks_native_creator(token):
            prefix = re.compile(rf"^{escaped}(?:\s+[-–—:：|｜·・]?\s*|\s*[-–—:：|｜·・]\s*)", re.IGNORECASE)
        else:
            prefix = re.compile(rf"^{escaped}\s*[-–—:：|｜·・]\s*", re.IGNORECASE)
        new_core = prefix.sub("", core, count=1).strip()
        if new_core != core:
            core = new_core
            changed = True
            break

    for token in sorted(tokens, key=len, reverse=True):
        suffix = re.compile(rf"(?:\s+|\s*[-–—:：|｜·・]\s*){re.escape(token)}\s*$", re.IGNORECASE)
        new_core = suffix.sub("", core, count=1).strip()
        if new_core != core:
            core = new_core
            changed = True
            break

    if not changed:
        return value, False
    prefix = " ".join(leading_non_creator + [f"[{block}]"])
    return _join_core_markers(f"{prefix} {core}".strip(), markers), True


def _looks_native_creator(text: str) -> bool:
    return bool(re.search(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]", text or ""))


def _creator_source_should_preserve_local(local_block: str, source_block: str) -> tuple[bool, str]:
    """本地 creator 已具体/原生时，不被 Various/罗马字来源降级。"""
    if not local_block or not source_block or local_block == source_block:
        return False, ""
    lg, la = _creator_parts(local_block)
    sg, sa = _creator_parts(source_block)
    same_group_literal = bool(lg and sg and unicodedata.normalize("NFKC", lg).casefold() == unicodedata.normalize("NFKC", sg).casefold())
    if same_group_literal and la and _is_multi_creator_placeholder(sa):
        return True, "来源 creator 使用 よろず/Various 多作者占位，保留本地具体作者"
    if _looks_native_creator(local_block) and is_latin_only_text(source_block):
        return True, "保留本地原生 creator 写法，避免被罗马字/英文形式覆盖"
    return False, ""

def _leading_bracket_raw(text: str) -> str:
    m = LEADING_BRACKET_RE.match((text or "").strip())
    return m.group(1).strip() if m else ""

def _creator_block_matches_tags(block: str, artist_tags: list[str], group_tags: list[str]) -> bool:
    nb = _norm_entity(block)
    if not nb:
        return False
    candidates = [_norm_entity(x) for x in (artist_tags + group_tags) if x]
    return any(c and c in nb for c in candidates)

def _creator_block_shape(block: str) -> tuple[int, bool]:
    """返回 creator 块的大致结构：实体数、是否包含 circle(author) 式括号。"""
    value = (block or "").strip()
    has_inner = bool(re.search(r"\([^()]+\)", value))
    return (2 if has_inner else 1), has_inner


def _creator_field_conflict(record, artist_tags: list[str], group_tags: list[str]) -> bool:
    if not record.title_jpn:
        return False
    title_block = _leading_bracket_raw(record.title)
    jp_block = _leading_bracket_raw(record.title_jpn)
    if not title_block or not jp_block or title_block == jp_block:
        return False

    title_matches = _creator_block_matches_tags(title_block, artist_tags, group_tags)
    jp_matches = _creator_block_matches_tags(jp_block, artist_tags, group_tags)
    if title_matches and jp_matches:
        return False

    # 若两边共享明显的拉丁别名，视为同一 creator 的不同写法。
    title_tokens = {x.casefold() for x in re.findall(r"[A-Za-z0-9]{4,}", title_block)}
    jp_tokens = {x.casefold() for x in re.findall(r"[A-Za-z0-9]{4,}", jp_block)}
    if title_tokens & jp_tokens:
        return False

    # 同一 E-H 记录里，title 常用罗马音、title_jpn 使用日文/汉字/韩文原生署名。
    # title creator 被 tags 佐证、正文高度一致，且原生侧结构不比 title 更复杂时，
    # 视为“原文名/简写”，而不是冲突。
    native_script = bool(re.search(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]", jp_block))
    if title_matches and re.search(r"[A-Za-z]", title_block) and native_script:
        title_shape = _creator_block_shape(title_block)[0]
        jp_shape = _creator_block_shape(jp_block)[0]
        if jp_shape <= title_shape:
            return False

    # 结构明显不一致仍视为强冲突，例如短的 [Ryou] 对上另一套完整社团(作者)。
    return bool(title_matches and not jp_matches)


def _primary_identity_conflict(
    current: str,
    source: str,
    current_creator: str,
    source_creator: str,
    artist_tags: list[str],
    group_tags: list[str],
    page_diff_ratio: float | None,
) -> bool:
    """识别 creator、标题正文同时严重冲突的高风险错配。

    不在这里判断哪本才对，只阻止来源标题自动覆盖，并送入 AI。页数差只作为
    联合证据，绝不单独触发 AI。
    """
    if not current_creator or not source_creator:
        return False
    if _norm_creator_literal(current_creator) == _norm_creator_literal(source_creator):
        return False
    current_body = _review_body_key(current)
    source_body = _review_body_key(source)
    if not current_body or not source_body:
        return False
    body_similarity = SequenceMatcher(None, current_body, source_body).ratio()
    if body_similarity >= 0.70:
        return False
    tags_exist = bool(artist_tags or group_tags)
    current_supported = _creator_block_matches_tags(current_creator, artist_tags, group_tags)
    severe_page_gap = isinstance(page_diff_ratio, (int, float)) and page_diff_ratio >= 0.25
    return bool(
        (tags_exist and not current_supported and body_similarity < 0.60)
        or (severe_page_gap and body_similarity < 0.35)
    )


def _normalize_leading_creator_parenthesis(title: str, title_jpn: str, artist_tags: list[str], group_tags: list[str]) -> tuple[str, bool]:
    """上传者偶尔把作者写成 (Halo)/(ハイロゥ)。
    只有 title 的开头圆括号能被 creator tags 佐证时，才把 title_jpn 对应位置的圆括号规范成作者块 []。
    """
    mt = re.match(r"^\s*\(([^()]{1,120})\)\s*", title or "")
    mj = re.match(r"^\s*\(([^()]{1,120})\)\s*", title_jpn or "")
    if not mt or not mj:
        return title_jpn, False
    src_creator = mt.group(1).strip()
    src_norm = _norm_entity(src_creator)
    if not _creator_block_matches_tags(src_creator, artist_tags, group_tags) and src_norm not in KNOWN_CREATOR_PAREN_PREFIXES:
        return title_jpn, False
    native_creator = mj.group(1).strip()
    if EVENT_PREFIX_RE.match(title_jpn or ""):
        return title_jpn, False
    rebuilt = f"[{native_creator}] {(title_jpn or '')[mj.end():].lstrip()}".strip()
    return rebuilt, True


def _parody_compare_key(text: str) -> str:
    value = unicodedata.normalize("NFKC", text or "").casefold()
    return re.sub(r"[^\w]+", "", value, flags=re.UNICODE)


def _source_already_has_parody_block(text: str, local_block: str, parody_tags: list[str]) -> bool:
    """判断来源标题是否已经包含可由 parody tag / 本地原作块佐证的原作括号。

    只用于避免“日文原作 + 英文原作”重复；绝不凭 tag 新增原作块。
    """
    local_content = (local_block or "")[1:-1].strip() if local_block else ""
    local_key = _parody_compare_key(local_content)
    tag_keys = [_parody_compare_key(x) for x in parody_tags if x and x.casefold() != "original"]

    # 来源尾部圆括号只有在确有非 original parody tag、且不像平台/版本说明时，才视为原作位。
    # (fantia版)/(男性視点ver.) 不能阻止本地 (オリジナル) 的保护。
    source_core, _ = _extract_trailing_structured_markers(text or "")
    tail = re.search(r"(?:\(([^()]{1,120})\)|（([^（）]{1,120})）)\s*$", source_core)
    if tail:
        tail_content = (tail.group(1) or tail.group(2) or "").strip()
        non_original_tags = [x for x in parody_tags if x and x.casefold() != "original"]
        service_or_version = bool(re.search(r"(?:fanbox|fantia|patreon|pixiv|gumroad|booth)\s*版|視点|视点|ver\.?|カラー|color", tail_content, re.IGNORECASE))
        if non_original_tags and not service_or_version and not PROGRESS_MARKER_RE.fullmatch(tail_content) and not _is_special_marker_text(tail_content):
            return True

    for m in re.finditer(r"\(([^()]{1,120})\)|（([^（）]{1,120})）", text or ""):
        content = (m.group(1) or m.group(2) or "").strip()
        if PROGRESS_MARKER_RE.fullmatch(content) or _is_special_marker_text(content):
            continue
        key = _parody_compare_key(content)
        if not key:
            continue
        if local_key and key == local_key:
            return True
        for tag_key in tag_keys:
            if not tag_key:
                continue
            if key == tag_key or key in tag_key or tag_key in key:
                return True
            if SequenceMatcher(None, key, tag_key).ratio() >= 0.88:
                return True
    return False


def _local_original_block(text: str) -> str:
    """保护本地已经明确写出的 (オリジナル)/(Original)。

    数据库未写 parody:original 只能说明字段缺失，不能反向证明本地 original 是错的。
    """
    core, _ = _extract_trailing_structured_markers(text or "")
    m = re.search(r"\s*(\([^()]{1,120}\)|（[^（）]{1,120}）)\s*$", core)
    if not m:
        return ""
    block = m.group(1)
    content = block[1:-1].strip().casefold()
    return block if content in {"original", "オリジナル"} else ""


def _local_parody_block(text: str, parody_tags: list[str]) -> str:
    """只保护标题本来就存在、且能被 parody tag 佐证的末尾原作块。

    parody tag 只能验证/保护已有块，绝不能凭 tag 新建原作块。
    """
    core, _ = _extract_trailing_structured_markers(text or "")
    m = re.search(r"\s*(\([^()]{1,120}\)|（[^（）]{1,120}）)\s*$", core)
    if not m:
        return ""
    block = m.group(1)
    content = block[1:-1].strip()
    if PROGRESS_MARKER_RE.fullmatch(content) or _is_special_marker_text(content):
        return ""

    tag_values = [x for x in parody_tags if x]
    if content.casefold() in {"original", "オリジナル"}:
        return block if any(x.casefold() == "original" for x in tag_values) else ""

    key = _parody_compare_key(content)
    for tag in tag_values:
        if tag.casefold() == "original":
            continue
        tag_key = _parody_compare_key(tag)
        if not key or not tag_key:
            continue
        if key == tag_key or key in tag_key or tag_key in key or SequenceMatcher(None, key, tag_key).ratio() >= 0.88:
            return block
    return ""


def _ensure_trailing_parody_spacing(text: str, parody_tags: list[str]) -> tuple[str, bool]:
    """已存在且被 tag 佐证的原作块，与正文之间统一保留一个空格。"""
    value = text or ""
    block = _local_parody_block(value, parody_tags)
    if not block:
        return value, False
    core, markers = _extract_trailing_structured_markers(value)
    m = re.search(r"\s*" + re.escape(block) + r"\s*$", core)
    if not m:
        return value, False
    prefix = core[:m.start()].rstrip()
    rebuilt_core = f"{prefix} {block}".strip()
    rebuilt = _join_core_markers(rebuilt_core, markers)
    return rebuilt, rebuilt != value


def _normalize_existing_parody_from_source(text: str, source: str, parody_tags: list[str]) -> tuple[str, bool]:
    """仅当本地和来源都已存在、且都被 parody tag 佐证时，采用来源原作块写法。

    绝不凭 tag 或来源给没有原作块的本地标题新增原作。
    """
    value = text or ""
    local_block = _local_parody_block(value, parody_tags)
    source_block = _local_parody_block(source or "", parody_tags)
    if not local_block or not source_block:
        return value, False
    if _parody_compare_key(local_block[1:-1]) != _parody_compare_key(source_block[1:-1]):
        # 允许两边都分别被同一 tag 佐证，但不在这里做跨语言强行转换。
        return value, False
    safe_source_block = sanitize_windows_name(source_block)
    if safe_source_block == local_block:
        return value, False
    core, markers = _extract_trailing_structured_markers(value)
    if not re.search(re.escape(local_block) + r"\s*$", core):
        return value, False
    core = re.sub(re.escape(local_block) + r"\s*$", safe_source_block, core)
    return _join_core_markers(core, markers), True


def _normalize_bare_native_creator(title_jpn: str, title: str) -> tuple[str, bool]:
    """识别 `社团 (作者) 标题` 这种漏方括号的 creator 头。

    只有另一 title 明确使用 `[Group (Artist)]` 结构时才启用，避免把普通正文括号误判成 creator。
    """
    value = (title_jpn or "").strip()
    m = re.match(r"^([^\[\]【】()（）]{1,60}?)\s*\(([^()]{1,60})\)\s+(.+)$", value)
    title_block = _leading_bracket_raw(title or "")
    if not m or not title_block or not re.search(r"\([^()]+\)", title_block):
        return title_jpn, False
    group = m.group(1).strip(); artist = m.group(2).strip(); body = m.group(3).strip()
    if not group or not artist or not body:
        return title_jpn, False
    return f"[{group} ({artist})] {body}", True


def _normalize_decorative_native_creator(title_jpn: str, current: str) -> tuple[str, bool]:
    """来源用 【Creator】 开头、而本地已有同名 [Creator] 时，统一 creator 槽位为 []。

    只改第一个、且必须与本地 creator 同名，后续 【标题正文】 原样保留。
    """
    value = (title_jpn or "").strip()
    m = re.match(r"^【([^【】]{1,120})】\s*", value)
    current_creator = _author_like_leading_bracket(current)
    if not m or not current_creator:
        return title_jpn, False
    native = m.group(1).strip()
    if _norm_entity(native) != _norm_entity(current_creator):
        return title_jpn, False
    return f"[{native}] {value[m.end():].lstrip()}".strip(), True


def _trailing_series_token(text: str) -> str:
    """取得标题尾部明确的系列编号 token；仅用于“本地与 title 同时存在”时保护。"""
    core, _ = _extract_trailing_structured_markers(text or "")
    core = core.rstrip()
    patterns = [
        r"(?P<t>\b(?:Part|Vol(?:ume)?|EP(?:isode)?|Ch(?:apter)?)\.?\s*\d{1,3})$",
        r"(?P<t>(?:第\s*)?\d{1,3}\s*(?:話|话|章|巻|卷|編|篇))$",
        r"(?P<t>(?:\s|[-：:])\d{1,3})$",
    ]
    for pat in patterns:
        m = re.search(pat, core, re.IGNORECASE)
        if m:
            return m.group("t").strip()
    return ""


def _trailing_series_number(text: str) -> str:
    token = _trailing_series_token(text)
    m = re.search(r"(\d{1,3})$", token)
    return m.group(1) if m else ""


def _series_token_present(token: str, text: str) -> bool:
    """判断目标标题是否已包含同一系列编号/范围，避免 2 -> 2 2、11-14 -> 11-14 14话。"""
    if not token or not text:
        return False
    token_key = _normalize_compare(token)
    text_key = _normalize_compare(text)
    if token_key and token_key in text_key:
        return True
    m = re.search(r"(\d{1,3})(?!.*\d)", token)
    if not m:
        return False
    number = m.group(1)
    core, _ = _extract_trailing_structured_markers(text)
    if re.search(rf"{re.escape(number)}\s*$", core):
        return True
    for sig in _progress_signatures(core):
        if not sig.startswith("range:"):
            continue
        nums = re.findall(r"\d{1,3}", sig)
        if number in nums:
            return True
    return False


def _suspected_copy_suffix(current: str, title: str, title_jpn: str) -> tuple[str, str]:
    """返回 (编号, 原样后缀)：支持 `_2` / `_3` 与 Windows 常见 ` (2)` / ` (3)`。

    副本编号经常位于所有 metadata 之后，例如 `标题 [中国翻訳] (2)`；
    因此必须先从完整文件名末尾剥离副本号，再比较去掉结构化 metadata 的正文。
    """
    raw_current = (current or "").rstrip()
    m = re.search(r"(?P<raw>_(?P<u>\d{1,3})|\s+\((?P<p>\d{1,3})\))\s*$", raw_current)
    if not m:
        return "", ""
    number = m.group("u") or m.group("p") or ""
    raw = m.group("raw")
    base_raw = raw_current[:m.start()].rstrip()
    base, _ = _extract_trailing_structured_markers(base_raw)
    if not base:
        return "", ""
    for source in (title_jpn, title):
        src_core, _ = _extract_trailing_structured_markers(source or "")
        if src_core and SequenceMatcher(None, _normalize_compare(base), _normalize_compare(src_core)).ratio() >= 0.94:
            return number, raw
    return "", ""

def _source_title_polluted(text: str) -> bool:
    value = text or ""
    if not value:
        return False
    return bool(
        TRANSLATION_GROUP_HINT_RE.search(value)
        or AI_TRANSLATION_TEXT_RE.search(value)
        or re.search(r"(?:补全|補全|上传|上傳|页数|頁數|Pages?|Completed|Ongoing)", value, re.IGNORECASE)
        or re.search(r"\[\s*\d{1,5}\s*p\s*\]", value, re.IGNORECASE)
    )

def _title_fields_nearly_same(a: str, b: str) -> bool:
    if not a or not b:
        return False
    na = _normalize_compare(a)
    nb = _normalize_compare(b)
    return SequenceMatcher(None, na, nb).ratio() >= 0.94

def _looks_cjk_translation_title(text: str) -> bool:
    # 中文译名常见为汉字主体；这里只作为“缺原版标题需复核”的保守信号。
    return bool(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", text or "")) and not contains_japanese_kana(text or "")

def _reliable_original_title(record, is_chinese_translation: bool, swapped: bool, remove_event_prefix: bool, creator_conflict: bool = False) -> tuple[str, str, bool]:
    """返回 (候选, 来源, 是否足够可靠用于自动建议)。

    title/title_jpn 只是 E-H 字段位置，不把 title_jpn 机械理解为“日文”。
    """
    title = record.title or ""
    title_jpn = record.title_jpn or ""

    if swapped and title:
        cand, _ = normalize_name_structure(title, is_chinese_translation, remove_event_prefix)
        return cand, "title（疑似字段反置后选用）", True

    if title_jpn:
        cand, _ = normalize_name_structure(title_jpn, is_chinese_translation, remove_event_prefix)
        if creator_conflict:
            return cand, "title_jpn（与 creator tags 冲突）", False
        # title_jpn 也可能被上传者混入页数/完成状态等说明；字段名本身不是绝对真值。
        hard_jp_pollution = bool(re.search(r"(?:补全|補全|上传|上傳|页数|頁數|Pages?|Completed|Ongoing)|\[\s*\d{1,5}\s*p\s*\]", title_jpn, re.IGNORECASE))
        if hard_jp_pollution:
            return cand, "title_jpn（含上传者页数/进度说明）", False
        if _title_fields_nearly_same(title, title_jpn) and (_source_title_polluted(title) or _source_title_polluted(title_jpn)):
            return cand, "title_jpn（与 title 高度相同且含翻译/上传说明）", False
        # 日文假名只有出现在标题正文时才是强证据；creator 块里的日文不能替英文/短标题主体背书。
        if contains_japanese_kana(_title_core_for_language(title_jpn)):
            return cand, "title_jpn", True
        # Western 等非日文原作，title_jpn 槽位也可能保存英文原版标题。
        if (record.category or "").casefold() == "western":
            western_core = _title_core_for_language(title_jpn)
            western_core = re.sub(r"^\[[^\[\]]+\]\s*", "", western_core).strip()
            if is_latin_only_text(western_core) and (is_chinese_translation or _looks_cjk_translation_title(title)):
                return cand, "title_jpn（Western 英文原版标题）", True
        return cand, "title_jpn", False

    cand, _ = normalize_name_structure(title, is_chinese_translation, remove_event_prefix)
    return cand, "title", False


def _title_core_for_language(text: str) -> str:
    value = normalize_fullwidth_latin_digits_brackets(text or "")
    # 去掉开头 creator 块和尾部元数据，避免“AI机翻润色”等中文说明误导主体语言判断。
    value = re.sub(r"^\s*\[[^\]]+\]\s*", "", value)
    value = AI_TRANSLATION_POLISHED_RE.sub(" ", value)
    value = AI_TRANSLATION_TEXT_RE.sub(" ", value)
    for _ in range(8):
        m = re.search(r"\s*(\[[^\[\]]+\]|【[^【】]+】|［[^［］]+］|\([^()]{1,100}\)|（[^（）]{1,100}）)\s*$", value)
        if not m:
            break
        c = m.group(1)[1:-1].strip()
        if _is_chinese_marker_text(c) or GENERIC_CHINESE_LANGUAGE_BLOCK_RE.fullmatch(c) or _is_translation_group_text(c) or _is_special_marker_text(c):
            value = value[:m.start()].rstrip()
        else:
            break
    return re.sub(r"\s+", " ", value).strip()


def _likely_bad_or_incomplete_source(record, title_kind: str, is_chinese_translation: bool = False, current: str = "") -> list[str]:
    warnings: list[str] = []
    title = record.title or ""
    if not record.title_jpn and "疑似日文罗马音" in title_kind:
        warnings.append("缺少原版标题：title_jpn 为空，且 title 疑似日文罗马音")
    elif not record.title_jpn and is_chinese_translation:
        core = _title_core_for_language(title)
        if (record.category or "").casefold() != "western" and _looks_cjk_translation_title(core):
            warnings.append("缺少原版标题：当前 title 更像中文译名，无法可靠恢复原始标题")

    desc_hits = list(UPLOAD_DESCRIPTION_RE.finditer(title))
    if desc_hits:
        matched = [m.group(0).strip().casefold() for m in desc_hits]
        ongoing_only = bool(matched) and all("ongoing" in x for x in matched)
        exact_local_snapshot = bool(current and _normalize_compare(current) == _normalize_compare(title))
        # 单纯 (ongoing) 且本地名与当前 E-H 标题一致时，它本身就是明确进度信息，
        # 不应仅凭这个词制造人工复核。Completed + 页数/语言描述等复杂上传者说明仍保守复核。
        if not (ongoing_only and exact_local_snapshot):
            warnings.append("title 含 Completed/Ongoing/页数/语言等上传者描述，主体需要人工确认")
    if title.count("_") >= 4:
        _cleaned_underscore, underscore_resolved = _normalize_filename_underscores(title)
        if not underscore_resolved:
            warnings.append("title 含大量下划线，疑似来源标题格式异常")
    return warnings


def _has_explicit_multilanguage_marker(*texts: str) -> bool:
    """当前名称/来源已经明确写出日英双语等信息时，不再仅凭两个 language tag 制造复核噪声。"""
    for text in texts:
        value = normalize_fullwidth_latin_digits_brackets(text or "")
        if re.search(r"\b(?:JP\s*[／/]?\s*EN|EN\s*[／/]?\s*JP)\b", value, re.IGNORECASE):
            return True
        if re.search(r"日本語\s*[・、,/＆&+]\s*英語|英語\s*[・、,/＆&+]\s*日本語", value, re.IGNORECASE):
            return True
    return False


def _short_circuit_cosplay(item: WorkItem, preserve_manual_category: bool) -> WorkItem:
    """Cosplay 只作为排除属性：原名保留，不读取 tags 生成命名或复核任务。"""
    current = item.original_name
    attrs = [
        x for x in (item.attributes or [])
        if x not in {ATTRIBUTE_ARCHIVE, ATTRIBUTE_COSPLAY, ATTRIBUTE_GALLERY_CONFLICT, "画集/归档类", "画集/归档"}
    ]
    attrs.append(ATTRIBUTE_COSPLAY)
    for attribute, enabled in (item.manual_attribute_overrides or {}).items():
        if enabled and attribute not in attrs:
            attrs.append(attribute)
        elif not enabled and attribute in attrs:
            attrs.remove(attribute)
    item.attributes = list(dict.fromkeys(attrs))
    item.program_suggested_name = current
    item.needs_ai = False

    item.program_category = CATEGORY_REVIEW
    if item.manual_confirmed and item.confirmed_name:
        item.suggested_name = item.confirmed_name
    elif item.name_source == "program" or not item.manual_name:
        item.name_source = "program"
        item.suggested_name = current
    if preserve_manual_category and item.manual_category:
        item.manual_category_value = item.manual_category_value or item.category
        item.category = item.manual_category_value
    else:
        item.category = CATEGORY_REVIEW

    analysis = {
        "analyzer_version": ANALYZER_VERSION,
        "metadata_candidate": current,
        "metadata_candidate_source": "当前本地名称（Cosplay 短路）",
        "metadata_candidate_reliable": True,
        "metadata_similarity": 1.0,
        "artist_tags": [],
        "group_tags": [],
        "parody_tags": [],
        "language_tags": [],
        "other_tags": [],
        "is_chinese": False,
        "is_translated": False,
        "is_chinese_translation": False,
        "chinese_original": False,
        "auto_nonstandard": False,
        "auto_cosplay": True,
        "attributes": list(item.attributes),
        "needs_ai": False,
        "needs_ai_reasons": [],
        "llm_review_reasons": [],
        "confidence": "高",
        "field_sources": {"title": "当前本地名称"},
        "reasons": ["E-H category 为 Cosplay：保留原名并绕过普通漫画命名分析"],
        "warnings": [],
        "flags": ["E-H category: Cosplay（命名分析短路）"],
    }
    item.extra = dict(item.extra or {})
    item.extra["needs_ai"] = False
    item.extra["analysis"] = analysis
    item.warning = ""
    return item


def analyze_item(
    item: WorkItem,
    preserve_manual_category: bool = True,
    options: dict | None = None,
) -> WorkItem:
    if item.manual_confirmed and not item.manual_category:
        item.manual_category = True
        item.manual_category_value = CATEGORY_CONFIRMED
    if item.record is None:
        item.program_category = item.program_category or item.category
        return item

    options = options or {}
    remove_event_prefix = bool(options.get("remove_event_prefix", True))

    record = item.record
    current = item.original_name
    tags = parse_tags(record.tags_raw)
    language_tags = _list_tag(tags, "language")
    artist_tags = _list_tag(tags, "artist")
    group_tags = _list_tag(tags, "group")
    parody_tags = _list_tag(tags, "parody")
    other_tags = _list_tag(tags, "other")

    language_cf = {x.casefold() for x in language_tags}
    other_cf = {x.casefold() for x in other_tags}
    is_chinese = "chinese" in language_cf
    is_translated = "translated" in language_cf
    all_name_text = " ".join([current, record.title or "", record.title_jpn or ""])
    explicit_translated_cn_marker = bool(re.search(r"中国翻訳|中国翻译|中国翻譯|中國翻译|中國翻譯|中文翻译|中文翻譯", all_name_text, re.IGNORECASE))
    explicit_translation_word = _has_explicit_translation_evidence(current, record.title or "", record.title_jpn or "")
    local_translated_cn_marker = bool(re.search(r"Chinese|中国翻訳|中国翻译|中国翻譯|中國翻译|中國翻譯|中文翻译|中文翻譯", current, re.IGNORECASE))
    local_translation_word = _has_explicit_translation_evidence(current)
    # 中文语言识别优先保证“不漏标”：E-H chinese 是强证据；
    # 文件名/标题里明确写 AI翻译、汉化、个人翻译等中文翻译说明时，即使 E-H 漏 tag 也应补中文标记。
    is_chinese_translation = bool(is_chinese or explicit_translated_cn_marker or explicit_translation_word)
    chinese_original = False
    has_no_text_language = bool(language_cf & {"text cleaned", "speechless", "no text"})
    rough_translation = "rough translation" in other_cf
    extraneous_ads = "extraneous ads" in other_cf or "ads" in other_cf
    ai_generated = "ai generated" in other_cf
    has_uncensored_tag = "uncensored" in other_cf

    is_cosplay = _is_cosplay_category(record.category)
    if is_cosplay:
        return _short_circuit_cosplay(item, preserve_manual_category)
    strong_nonstandard = _is_strong_nonstandard(record, current, ai_generated)

    current_creator_form, current_dot_creator_fixed = _normalize_creator_dot_prefix(current, artist_tags, group_tags)
    title_creator_form, title_dot_creator_fixed = _normalize_creator_dot_prefix(record.title or "", artist_tags, group_tags)
    jp_creator_form, jp_dot_creator_fixed = _normalize_creator_dot_prefix(record.title_jpn or "", artist_tags, group_tags)

    title_kind = detect_text_kind(record.title)
    title_jpn_kind = detect_text_kind(record.title_jpn)
    title_core_lang = _title_core_for_language(record.title or "")
    title_jpn_core_lang = _title_core_for_language(record.title_jpn or "")
    swapped = bool(
        record.title_jpn
        and contains_japanese_kana(title_core_lang)
        and is_latin_only_text(title_jpn_core_lang)
    )

    normalized_creator_jpn, creator_parenthesis_fixed = _normalize_leading_creator_parenthesis(
        title_creator_form, jp_creator_form, artist_tags, group_tags
    )
    bare_native_creator_fixed = False
    if not creator_parenthesis_fixed:
        normalized_creator_jpn, bare_native_creator_fixed = _normalize_bare_native_creator(jp_creator_form, title_creator_form)
    decorative_native_creator_fixed = False
    if not creator_parenthesis_fixed and not bare_native_creator_fixed:
        normalized_creator_jpn, decorative_native_creator_fixed = _normalize_decorative_native_creator(jp_creator_form, current_creator_form)

    creator_conflict = _creator_field_conflict(record, artist_tags, group_tags)
    candidate, candidate_source, candidate_reliable = _reliable_original_title(
        record, is_chinese_translation, swapped, remove_event_prefix, creator_conflict=creator_conflict
    )
    candidate, source_date_removed = _strip_extra_source_date_blocks(
        candidate, current, record.category
    )
    candidate_dot_fixed = False
    if candidate:
        candidate, candidate_dot_fixed = _normalize_creator_dot_prefix(candidate, artist_tags, group_tags)
    if (creator_parenthesis_fixed or bare_native_creator_fixed or decorative_native_creator_fixed) and record.title_jpn:
        source_was_polluted = "上传者" in candidate_source or "上传说明" in candidate_source
        candidate, _ = normalize_name_structure(
            normalized_creator_jpn, is_chinese_translation, remove_event_prefix, chinese_original=chinese_original
        )
        candidate, removed_after_creator_fix = _strip_extra_source_date_blocks(
            candidate, current, record.category
        )
        source_date_removed = source_date_removed or removed_after_creator_fix
        candidate_source = "title_jpn（开头 creator 已规范" + ("，但含上传者说明" if source_was_polluted else "") + "）"
        if contains_japanese_kana(_title_core_for_language(record.title_jpn or "")) and not source_was_polluted:
            candidate_reliable = True
        elif source_was_polluted:
            candidate_reliable = False

    # title 里常见 (Creator) / (Creator} 这类上传者格式错误。
    # 只有 creator tags 能佐证时才纠正，正文剩余部分原样保留。
    candidate_creator_fixed = False
    if candidate and candidate_source.startswith("title") and not candidate_source.startswith("title_jpn"):
        fixed_candidate, candidate_creator_fixed = _fix_leading_creator_from_tags(candidate, artist_tags, group_tags)
        if candidate_creator_fixed:
            candidate = fixed_candidate
            candidate_source = candidate_source + "（creator 前缀已纠正）"

    title_norm_for_creator, _ = normalize_name_structure(
        title_creator_form, is_chinese_translation, remove_event_prefix, chinese_original=chinese_original
    )
    jp_norm_for_creator, _ = normalize_name_structure(
        jp_creator_form, is_chinese_translation, remove_event_prefix, chinese_original=chinese_original
    )
    current_creator = _author_like_creator_after_convention(current_creator_form, artist_tags, group_tags)
    title_creator = _author_like_creator_after_convention(
        title_norm_for_creator, artist_tags, group_tags, known_creator=current_creator
    )
    jp_creator = _author_like_creator_after_convention(
        jp_norm_for_creator, artist_tags, group_tags, known_creator=current_creator or title_creator
    )

    # 即使 artist/group tags 缺失，只要同一记录的 title 与 title_jpn 正文几乎一致，
    # 而 title 明确有 creator 块、title_jpn 没有，就可把 title creator 作为字段级补充。
    creator_bridge_without_tags = bool(
        record.title
        and record.title_jpn
        and title_creator
        and not jp_creator
        and not artist_tags
        and not group_tags
        and _title_bodies_nearly_same_ignoring_creator(title_norm_for_creator, jp_norm_for_creator)
    )
    # 无 artist/group tags 时，另一标题里的 creator 只能作为“未验证候选”，不能自动补入。
    # 本地已经存在的 creator 则属于用户已有信息，应在无反证时保护。
    current_creator_supported = bool(current_creator and _creator_block_matches_tags(current_creator, artist_tags, group_tags))
    missing_native_creator = bool(
        candidate_reliable
        and record.title_jpn
        and not jp_creator
        and (
            bool(current_creator)
            or current_creator_supported
            or (title_creator and _creator_block_matches_tags(title_creator, artist_tags, group_tags))
        )
    )
    creator_fill_block = ""
    if missing_native_creator:
        # title_jpn 缺字段不代表作者应被删除；优先保留本地已有 creator，其次沿用 title creator。
        creator_fill_block = current_creator or title_creator

    # 仅 title 场景中，如果 creator 前缀能被 tags 佐证，且数据库标题只是比本地多出一段正文，
    # 允许恢复这段被旧本地名截断的正文（JOKERKIN 颜文字/尾缀类）。
    title_candidate_can_extend_local = bool(
        candidate_creator_fixed
        and not record.title_jpn
        and current_creator
        and _creator_block_matches_tags(current_creator, artist_tags, group_tags)
        and _candidate_extends_local_body(current, candidate)
        and not _source_title_polluted(record.title or "")
    )
    if title_candidate_can_extend_local:
        candidate_reliable = True
        candidate_source = candidate_source + "（来源标题补回本地缺失尾部正文）"

    # 当前本地名开头若已被可靠识别为 creator 圆括号，属于确定性结构修正；
    # 即使整本仍需人工复核，也不应阻断 (Creator) -> [Creator]。
    convention_block_pre, convention_type_pre = classify_leading_parenthesis(current_creator_form, artist_tags, group_tags)
    current_for_normalization = current_creator_form
    current_creator_prefix_fixed = False
    if convention_block_pre and convention_type_pre == "CREATOR":
        m_creator = re.match(r"^\(([^()]{1,120})\)\s*", current_for_normalization.strip())
        if m_creator:
            creator_text = m_creator.group(1).strip()
            current_for_normalization = f"[{creator_text}] {current_for_normalization.strip()[m_creator.end():].lstrip()}".strip()
            current_creator_prefix_fixed = True

    # 画集和 Cosplay 不主动注入普通漫画的中文语言标记。
    normalize_as_standard = not strong_nonstandard and not is_cosplay
    normalized_current, current_actions = normalize_name_structure(
        current_for_normalization,
        is_chinese_translation if normalize_as_standard else False,
        remove_event_prefix,
        chinese_original=chinese_original if normalize_as_standard else False,
    )

    reasons: list[str] = list(current_actions)
    if source_date_removed:
        reasons.append("非 Image Set 来源独有单日日期视为上传时间，不导入正式文件名")
    if current_dot_creator_fixed:
        reasons.insert(0, "文件名使用‘作者·标题’非标准格式，且作者可由 E-H tags 佐证，规范为 [作者] 标题")
    if candidate_dot_fixed or title_dot_creator_fixed or jp_dot_creator_fixed:
        reasons.append("来源标题中的‘作者·标题’格式已按 tags 佐证拆分 creator 与标题")
    if current_creator_prefix_fixed:
        reasons.insert(0, "当前文件名开头圆括号已可靠识别为 creator，规范为方括号 creator 结构")
    warnings: list[str] = []
    flags: list[str] = []

    local_has_chinese_marker = _has_explicit_chinese_block(current)
    gallery_match_risk = bool(local_has_chinese_marker and not is_chinese)
    if gallery_match_risk:
        warnings.append("本地文件名明确含中文语言标记，但 E-H language tags 未包含 chinese；画廊关联或元数据可能不一致")

    if is_chinese:
        flags.append("E-H language: chinese（命名统一使用 [中国翻訳]）")
    if is_translated:
        flags.append("E-H language: translated")
    if has_no_text_language:
        flags.append("E-H language: 无文字 / text cleaned / speechless")
    if rough_translation:
        flags.append("rough translation（机翻勾选项，仅作辅助证据）")
    if ai_generated:
        flags.append("ai generated")
    if extraneous_ads:
        flags.append("extraneous ads（未来广告组识别/低质量版本筛选证据）")
    if is_cosplay:
        flags.append("E-H category: Cosplay（独立人工审查分类）")

    convention_block, convention_type = classify_leading_parenthesis(current, artist_tags, group_tags)
    if convention_block:
        if convention_type == "CONVENTION":
            flags.append(f"开头圆括号识别为活动：{convention_block}")
        elif convention_type == "CREATOR":
            flags.append(f"开头圆括号识别为作者/社团：{convention_block}")
        else:
            flags.append(f"开头圆括号身份不确定：{convention_block}")

    if creator_parenthesis_fixed:
        reasons.append("识别到开头圆括号为作者署名，规范为方括号 creator 结构")
    if bare_native_creator_fixed:
        reasons.append("识别到无方括号的 社团 (作者) creator 头，规范为方括号 creator 结构")
    if decorative_native_creator_fixed:
        reasons.append("来源标题以 【Creator】 开头且与本地 creator 一致，统一 creator 槽位为 []")
    if missing_native_creator and creator_fill_block:
        if current_creator and creator_fill_block == current_creator and not current_creator_supported:
            reasons.append("title_jpn 缺少 creator 块；来源没有反证，保护本地已有 creator")
        else:
            reasons.append("title_jpn 缺少 creator 块，沿用本地/title 中已被 tags 佐证的 creator")

    # Cosplay / 画集不套普通漫画 creator 完整性规则。
    unverified_creator_candidate = False
    if not is_cosplay and not strong_nonstandard:
        if creator_conflict:
            warnings.append("title_jpn 作者/社团块与 title/tags 的可靠 creator 信息明显冲突，禁止自动采用")
        if (
            not artist_tags
            and not group_tags
            and not _author_like_leading_bracket(current_for_normalization)
            and not current_creator_prefix_fixed
            and not creator_parenthesis_fixed
        ):
            source_creator_candidate = jp_creator or title_creator
            warnings.append("未找到可靠 artist/group tag，当前文件名也没有明显作者块")
            if source_creator_candidate:
                unverified_creator_candidate = True
                warnings.append(f"另一标题存在未验证 creator 候选：{source_creator_candidate}；不能自动补入，需要人工确认")

    content_language_tags = {
        x for x in language_cf
        if x not in {"translated", "rewrite", "text cleaned", "speechless", "no text", "textless narrative"}
    }
    if len(content_language_tags) > 1:
        explicit_multilang = _has_explicit_multilanguage_marker(current, record.title or "", record.title_jpn or "")
        if not explicit_multilang:
            warnings.append("同时存在多种内容语言 tag，实际内容可能为多语言版本，需要人工确认")
        else:
            reasons.append("文件名/来源已明确标注多语言版本，不再仅凭多个 language tag 强制复核")
    if swapped and not is_cosplay and not strong_nonstandard:
        warnings.append("title / title_jpn 疑似反置")

    if not is_cosplay and not strong_nonstandard:
        warnings.extend(_likely_bad_or_incomplete_source(record, title_kind, is_chinese_translation=is_translated, current=current))

    # 本地已有 Group (Artist)，title_jpn 也有同组但具体 Artist 不同：
    # - よろず / Various 是多作者占位符，不能覆盖具体作者；
    # - 罗马字 <-> 原生文字可作为“显示写法升级”继续走既有 creator 保护逻辑；
    # - 同类文字体系下的两个具体名字则视为真实 creator 冲突。
    #   即使 artist tag 只支持其中一边，也不能据此证明另一边就是错误数据/同名别名。
    local_creator_conflict = False
    creator_placeholder_override = False
    if current_creator and jp_creator and current_creator != jp_creator:
        cg, ca = _creator_parts(current_creator)
        jg, ja = _creator_parts(jp_creator)
        same_group = bool(cg and jg and _norm_creator_literal(cg) == _norm_creator_literal(jg))
        different_author = bool(ca and ja and _norm_creator_literal(ca) != _norm_creator_literal(ja))
        # よろず / Various 是多作者占位符，不是能覆盖具体作者的人名。
        if same_group and ca and _is_multi_creator_placeholder(ja):
            creator_placeholder_override = True
            reasons.append("title_jpn 的 よろず/Various 视为多作者占位符，保留本地具体 creator")
        elif same_group and different_author:
            # 两边都是具体作者名时，即使一边罗马字、一边原生文字，也不能仅凭“看起来像转写”自动认定同一人。
            # 这类关系留给原名映射数据库 / AI / 人工确认。
            local_creator_conflict = True
            warnings.append(
                "本地 creator 子作者与 title_jpn 不一致：两边都是具体名字；现有 tags 不能证明两者是同名/别名，"
                "保留本地信息并交由语义复核"
            )

    # 页数差仍保留为版本/匹配信号。复合漫画目录若存在子目录图片，优先用扫描器递归统计值。
    scanned_local_page_count = (item.extra or {}).get("local_page_count") if isinstance(item.extra, dict) else None
    effective_local_page_count = scanned_local_page_count if isinstance(scanned_local_page_count, int) and scanned_local_page_count > 0 else record.page_count
    composite_manga_dir = bool((item.extra or {}).get("composite_manga_dir")) if isinstance(item.extra, dict) else False
    if composite_manga_dir:
        direct_n = int((item.extra or {}).get("direct_image_count") or 0)
        nested_n = int((item.extra or {}).get("nested_image_count") or 0)
        nested_dirs = int((item.extra or {}).get("nested_dir_count") or 0)
        warnings.append(f"检测到复合漫画目录：直接图片 {direct_n}，子目录 {nested_dirs}，子目录图片 {nested_n}；仍按一本漫画处理，不自动拆分")
    diff, ratio, page_level = _page_diff(record.filecount, effective_local_page_count)
    if page_level in {"较大", "极大"} and not is_cosplay:
        warnings.append(
            f"E-H filecount 与本地 pageCount 差距{page_level}："
            f"{record.filecount} vs {effective_local_page_count}（{ratio:.1%}）；可能是历史版本/动态画廊，也可能需要检查匹配"
        )

    # 动态/更新中画廊：本地下载标题是历史快照证据，当前数据库进度不能直接覆盖。
    progress_mismatch, local_progress, remote_progress = _has_progress_mismatch(
        normalized_current, record.title or "", record.title_jpn or ""
    )
    if progress_mismatch and not is_cosplay and not strong_nonstandard:
        warnings.append("本地进度与当前 E-H 标题不一致，疑似动态/更新中画廊历史版本；保留本地进度，禁止用当前标题直接覆盖")

    # title 与 title_jpn 之间若只有一侧出现明确范围进度（如 1-7），不要静默忽略。
    title_progress = {x for x in _progress_signatures(record.title or "") if x.startswith("range:")}
    jp_progress = {x for x in _progress_signatures(record.title_jpn or "") if x.startswith("range:")}
    source_progress_conflict = bool(record.title and record.title_jpn and title_progress and not (title_progress & jp_progress))
    if source_progress_conflict and not is_cosplay and not strong_nonstandard:
        warnings.append("title 与 title_jpn 的系列/进度范围不一致（如 1-7）；该范围很可能代表连载或阶段版本，需要人工确认")

    source_identity_text = record.title_jpn or record.title or candidate or ""

    event_conflict = False
    if not remove_event_prefix and not is_cosplay and not strong_nonstandard:
        local_event_key = _leading_event_value(current)
        source_event_key = _leading_event_value(source_identity_text)
        if local_event_key and source_event_key and local_event_key != source_event_key:
            event_conflict = True
            warnings.append("本地活动/展会前缀与来源标题不一致；活动编号可能代表不同版本，禁止自动覆盖")

    series_identity_conflict = False
    if not is_cosplay and not strong_nonstandard and source_identity_text:
        series_identity_conflict = (
            _series_identity_conflict(current, source_identity_text)
            or _embedded_title_number_conflict(current, source_identity_text)
        )
        if series_identity_conflict:
            warnings.append("本地与来源的系列/卷号/话数存在实质变化（如 5→5.5）；可能是不同章节或增补版，禁止自动覆盖")

    current_censor_states = _censor_states(current)
    source_censor_states = _censor_states(source_identity_text)
    censor_state_conflict = bool(current_censor_states and source_censor_states and current_censor_states.isdisjoint(source_censor_states))
    if censor_state_conflict and not is_cosplay and not strong_nonstandard:
        warnings.append("本地与来源的修正状态存在实质差异（例如 半無修正 与 無修正）；保留本地状态并需要人工确认")

    source_creator_for_identity = jp_creator or title_creator
    primary_identity_conflict = _primary_identity_conflict(
        current,
        source_identity_text,
        current_creator,
        source_creator_for_identity,
        artist_tags,
        group_tags,
        ratio,
    )
    if primary_identity_conflict and not strong_nonstandard:
        warnings.append(
            "当前与来源的 creator、标题正文同时明显冲突，并叠加 tags/页数风险；疑似错配，禁止自动覆盖并交由 AI 复核"
        )

    # 这三类情况说明“标题来源字段本身”就是不确定对象；它可以作为 AI 证据，
    # 但不得继续参与自动标题重建或从标题中抽取新 metadata。
    unsafe_title_source = bool(swapped or unverified_creator_candidate or primary_identity_conflict)

    generic_source_overwrite_risk = False
    if candidate_reliable and source_identity_text and not is_cosplay and not strong_nonstandard:
        generic_source_overwrite_risk = _source_is_generic_collection_risk(
            current,
            source_identity_text,
            source_has_collection_tag=bool(other_cf & {"compilation", "collection"}),
        )
        if generic_source_overwrite_risk:
            warnings.append("来源标题疑似合集/范围扩大（如 A+B、追加合集说明或总标题）；禁止自动覆盖本地具体作品名")

    copy_suffix_number, copy_suffix_raw = _suspected_copy_suffix(current, record.title or "", record.title_jpn or "")
    copy_suffix_risk = bool(copy_suffix_number)
    if copy_suffix_risk and not is_cosplay:
        warnings.append(f"文件名尾部 {copy_suffix_raw.strip()} 疑似本地重复副本编号；本次不自动删除，等待重复关联检查/人工确认")

    # E-H 标题较短时，保护本地可能存在的正式副标题/完整标题文本。
    local_extra_title_text = False
    if candidate_reliable and record.title_jpn and not is_cosplay and not strong_nonstandard:
        source_for_compare = candidate if swapped else (normalized_creator_jpn if (creator_parenthesis_fixed or bare_native_creator_fixed or decorative_native_creator_fixed) else record.title_jpn)
        source_norm_for_compare, _ = normalize_name_structure(
            source_for_compare, is_chinese_translation, remove_event_prefix, chinese_original=chinese_original
        )
        local_extra_title_text = _local_has_extra_title_text(normalized_current, source_norm_for_compare)
        if local_extra_title_text:
            warnings.append("本地标题包含 title_jpn 之外的较长正文/副标题；为避免误删正式副标题，本次保留本地标题并等待确认")

    # ───────── 建议名生成 ─────────
    # Cosplay 只负责分流人工审查，不做普通漫画自动改名。
    if is_cosplay:
        program_name = current
    else:
        program_name = normalized_current

    # 画集保留平台/ID/日期/版本等本地结构；不使用 title_jpn 整体覆盖。
    can_rebuild_from_jp = bool(
        candidate_reliable
        and record.title_jpn
        and not is_cosplay
        and not strong_nonstandard
        and not progress_mismatch
        and not source_progress_conflict
        and not series_identity_conflict
        and not event_conflict
        and not censor_state_conflict
        and not generic_source_overwrite_risk
        and not local_extra_title_text
        and not copy_suffix_risk
        and not local_creator_conflict
        and not primary_identity_conflict
        and not swapped
        and not unverified_creator_candidate
    )

    if can_rebuild_from_jp:
        source_jpn_for_rebuild = candidate if swapped else (normalized_creator_jpn if (creator_parenthesis_fixed or bare_native_creator_fixed or decorative_native_creator_fixed) else record.title_jpn)
        source_jpn_for_rebuild, rebuild_date_removed = _strip_extra_source_date_blocks(
            source_jpn_for_rebuild, current, record.category
        )
        if rebuild_date_removed:
            reasons.append("非 Image Set 来源独有单日日期视为上传时间，不导入正式文件名")
        jp_norm, _ = normalize_name_structure(
            source_jpn_for_rebuild, is_chinese_translation, remove_event_prefix, chinese_original=chinese_original
        )
        jp_core, _jp_markers = _extract_trailing_structured_markers(jp_norm)
        if re.match(r"^\[アンソロジー\](?:\s|$)", normalized_current) and not re.match(
            r"^\[アンソロジー\](?:\s|$)", jp_core
        ):
            jp_core = f"[アンソロジー] {jp_core}".strip()
            reasons.append("重建标题时保护已规范化的 [アンソロジー] 属性前缀")
        if missing_native_creator and creator_fill_block and not _author_like_leading_bracket(jp_core):
            anthology = re.match(r"^\[アンソロジー\]\s*", jp_core)
            if anthology:
                jp_core = f"[アンソロジー] [{creator_fill_block}] {jp_core[anthology.end():].lstrip()}".strip()
            else:
                jp_core = f"[{creator_fill_block}] {jp_core}".strip()

        # creator 来源只用于补全，不允许把本地具体作者降级成よろず/Various或罗马字。
        jp_block = _author_like_leading_bracket(jp_core) or _leading_bracket_raw(jp_core)
        cur_block = _author_like_leading_bracket(normalized_current)
        if jp_block and cur_block and jp_block != cur_block:
            preserve_local, preserve_reason = _creator_source_should_preserve_local(cur_block, jp_block)
            if not preserve_local:
                jg, ja = _creator_parts(jp_block)
                cg, ca = _creator_parts(cur_block)
                same_group = bool(
                    jg and cg
                    and re.sub(r"\s+", "", unicodedata.normalize("NFKC", jg)).casefold()
                        == re.sub(r"\s+", "", unicodedata.normalize("NFKC", cg)).casefold()
                )
                local_richer = same_group and bool(ca) and not bool(ja)
                local_native_author = same_group and bool(ca and ja) and contains_japanese_kana(ca) and is_latin_only_text(ja)
                preserve_local = local_richer or local_native_author
                if preserve_local:
                    preserve_reason = "保留本地已有的更完整/原生 creator 写法，避免被简写或罗马音覆盖"
            if preserve_local:
                # creator 可能位于活动/平台前缀之后，只替换真正 creator 块。
                escaped = re.escape(jp_block)
                jp_core = re.sub(r"\[" + escaped + r"\]", f"[{cur_block}]", jp_core, count=1)
                reasons.append(preserve_reason)

        if not remove_event_prefix:
            local_event_match = EVENT_PREFIX_RE.match(normalized_current)
            local_event = local_event_match.group(0).strip() if local_event_match else ""
            if local_event and not EVENT_PREFIX_RE.match(jp_core):
                jp_core = f"{local_event} {jp_core}".strip()
                reasons.append("保留模式下保护本地已有活动/展会前缀")

        local_series = _trailing_series_token(normalized_current)
        title_series = _trailing_series_token(record.title or "")
        jp_series = _trailing_series_token(jp_core)
        if (
            local_series
            and title_series
            and _normalize_compare(title_series) == _normalize_compare(local_series)
            and not jp_series
            and not _series_token_present(local_series, jp_core)
        ):
            jp_core = f"{jp_core.rstrip()} {local_series}".strip()
            reasons.append("本地与 title 同时存在相同系列编号，title_jpn 缺失时保护该编号")

        local_parody = _local_parody_block(normalized_current, parody_tags) or _local_original_block(normalized_current)
        if local_parody and not _source_already_has_parody_block(jp_core, local_parody, parody_tags):
            jp_core = f"{jp_core.rstrip()} {local_parody}".strip()
            reasons.append("本地标题已有原作块；来源没有可靠反证，重建标题时保护该原作信息")

        merged_markers = _merge_markers(normalized_current, record.title, record.title_jpn, is_chinese_translation)
        rebuilt = _join_core_markers(jp_core, merged_markers)
        if rebuilt and _normalize_compare(rebuilt) != _normalize_compare(program_name):
            program_name = rebuilt
            reasons.append("检测到可靠 title_jpn 标题主体，按字段合并后用于程序建议")

    # 仅 title 且 creator 前缀被可靠纠正时，允许补回本地缺失的尾部正文。
    if (title_candidate_can_extend_local and not is_cosplay and not strong_nonstandard and not progress_mismatch
            and not source_progress_conflict and not series_identity_conflict and not event_conflict
            and not censor_state_conflict and not generic_source_overwrite_risk and not primary_identity_conflict
            and not swapped and not unverified_creator_candidate):
        candidate_core, _ = _extract_trailing_structured_markers(candidate)
        merged_markers = _merge_markers(normalized_current, record.title, "", is_chinese_translation)
        rebuilt = _join_core_markers(candidate_core, merged_markers)
        if rebuilt and _normalize_compare(rebuilt) != _normalize_compare(program_name):
            program_name = rebuilt
            reasons.append("来源 title 的 creator 已被 tags 佐证，补回本地缺失的尾部正文/颜文字")

    # 翻译方式/汉化组是确定性 metadata；普通漫画可从数据库补充。
    # 非标准归档只保护平台/ID/日期主体，不应阻止本地已经明确的语言/AI 等安全 metadata。
    if not is_cosplay and not strong_nonstandard and not unsafe_title_source:
        program_name, extra_translation_actions, weak_translation_groups = _append_missing_translation_metadata(
            program_name, current, record.title or "", record.title_jpn or "", is_chinese_translation
        )
        reasons.extend(extra_translation_actions)
        if weak_translation_groups:
            warnings.append("数据库标题存在翻译组/译者候选，但标题主体对应不足，未自动补入：" + "、".join(weak_translation_groups))

    # language:chinese / text cleaned / speechless / AI Generated 是确定性的版本属性，最终输出阶段统一兜底。
    safe_chinese_output = bool(
        is_chinese_translation
        and (not primary_identity_conflict or local_translated_cn_marker or local_translation_word)
    )
    if safe_chinese_output and not is_cosplay:
        program_name, chinese_added = _ensure_chinese_marker(program_name)
        if chinese_added:
            if is_chinese and not primary_identity_conflict:
                reasons.append("E-H language 含 chinese，最终名称保证中文语言标记")
            else:
                reasons.append("当前文件名已有明确中文翻译语义，最终名称统一为 [中国翻訳]")
    if has_no_text_language:
        program_name, no_text_added = _ensure_no_text_marker(program_name)
        if no_text_added:
            reasons.append("E-H language 表示无文字，最终名称补充 [No Text]")
    if ai_generated and not is_cosplay and not primary_identity_conflict:
        program_name, ai_added = _ensure_ai_generated_marker(program_name)
        if ai_added:
            reasons.append("E-H other 含 ai generated，最终名称保证 [AI Generated]")
    if has_uncensored_tag and not is_cosplay and not primary_identity_conflict:
        program_name, uncensored_added = _ensure_uncensored_marker(program_name)
        if uncensored_added:
            reasons.append("E-H other 含 uncensored，最终名称补充 [無修正]")

    # 用户规则：E-H 明确为 anthology 且存在多个作者时，统一归类到 [アンソロジー]。
    if not is_cosplay and not strong_nonstandard and "anthology" in other_cf and len(artist_tags) >= 2:
        program_name, anthology_added = _ensure_anthology_prefix(program_name)
        if anthology_added:
            reasons.append("E-H other 标记为 anthology 且包含多位作者，统一添加 [アンソロジー]")

    # title 来源与本地内容完全一致、仅分隔符不同（: / | / _）时，优先采用数据库明确标点。
    if (candidate and not (event_conflict or series_identity_conflict or censor_state_conflict or generic_source_overwrite_risk)
            and not swapped and not unverified_creator_candidate and not primary_identity_conflict
            and _separator_insensitive_key(candidate) == _separator_insensitive_key(program_name)):
        if _normalize_compare(candidate) != _normalize_compare(program_name):
            program_name = candidate
            reasons.append("数据库标题与本地仅分隔符不同，采用来源标题中的明确标点")
            if is_chinese_translation and not is_cosplay:
                program_name, _ = _ensure_chinese_marker(program_name)
            if has_no_text_language:
                program_name, _ = _ensure_no_text_marker(program_name)

    # 画集只保护平台/ID/日期主体；若本地与 record.title 仅安全分隔符不同，
    # 仍允许把数据库的 : / | / ? 等转换成 Windows 可用的全角等价符号。
    if strong_nonstandard and record.title:
        safe_source_title = _normalize_structure_spacing(sanitize_windows_name(record.title))
        if _separator_insensitive_key(safe_source_title) == _separator_insensitive_key(program_name):
            if _normalize_compare(safe_source_title) != _normalize_compare(program_name):
                program_name = safe_source_title
                reasons.append("画集结构一致，仅采用来源标题中的安全标点")

    # 疑似 Windows/人工复制产生的 _2/_3 / (2)/(3) 不自动删除。
    if copy_suffix_number:
        raw_suffix = copy_suffix_raw.strip()
        if not program_name.rstrip().endswith(raw_suffix):
            if copy_suffix_raw.startswith("_"):
                # 下划线清洗可能把 `_2` 误解释成正文 `- 2`，先清掉误片段。
                program_name = re.sub(
                    r"\s*-\s*" + re.escape(copy_suffix_number) + r"(?=\s*(?:\[[^\[\]]+\]\s*)+$)",
                    " ", program_name,
                )
                program_name = re.sub(r"\s*-\s*" + re.escape(copy_suffix_number) + r"\s*$", "", program_name).rstrip()
                program_name = re.sub(r"\s+", " ", program_name).strip() + copy_suffix_raw
            else:
                # normalize_name_structure 可能已把 `(2)` 留在 metadata 之前；先移除内部副本号，再放回文件名最末尾。
                program_name = re.sub(
                    r"\s+\(" + re.escape(copy_suffix_number) + r"\)(?=\s*(?:\[[^\[\]]+\]\s*)+$)",
                    " ",
                    program_name,
                )
                program_name = re.sub(r"\s+", " ", program_name).strip() + " " + raw_suffix
        reasons.append(f"保留疑似本地重复副本后缀 {raw_suffix}，不将其解释为标题正文")

    # Digital / DL版 是可选 metadata：数据库来源不再向本地名称主动补充。
    # 已有原作块可在同一来源/tag 佐证下做安全标点规范；没有原作块时绝不新增。
    if not is_cosplay and not strong_nonstandard:
        # 已有原作块可在同一来源/tag 佐证下做安全标点规范；没有原作块时绝不新增。
        source_for_parody = record.title_jpn or record.title or ""
        program_name, parody_normalized = _normalize_existing_parody_from_source(
            program_name, source_for_parody, parody_tags
        )
        if parody_normalized:
            reasons.append("本地与来源均已有且可验证同一原作块，采用来源写法并做 Windows 安全标点规范")

    # 活动名设置必须在所有 title/title_jpn 合并之后再执行，防止后续重建把 Cxx 等重新加回来。
    if remove_event_prefix:
        cleaned_event = strip_event_prefix(program_name, remove=True)
        if cleaned_event != program_name:
            program_name = cleaned_event
            reasons.append("最终输出阶段按设置移除活动/展会标记")

    program_name = _cleanup_extracted_marker_residue(_normalize_structure_spacing(program_name))
    program_name, surplus_closer_removed = _drop_surplus_closing_delimiters(program_name)
    if surplus_closer_removed:
        reasons.append("移除明显多余且无对应左括号的右括号")
    program_name, metadata_deduped = _dedupe_structured_metadata(program_name)
    if metadata_deduped:
        reasons.append("最终输出统一去重重复的语言/版本/进度/翻译 metadata")
    program_name, creator_token_deduped = _dedupe_structured_creator_token(program_name)
    if creator_token_deduped:
        reasons.append("移除 creator 已结构化后在标题正文首尾重复出现的同名 token")
    program_name, parody_spacing_fixed = _ensure_trailing_parody_spacing(program_name, parody_tags)
    if parody_spacing_fixed:
        reasons.append("已验证的原作块与标题正文之间统一保留一个空格")
    if copy_suffix_number and copy_suffix_raw.startswith("_"):
        program_name = re.sub(rf"\s+_{re.escape(copy_suffix_number)}\s*$", f"_{copy_suffix_number}", program_name)

    if not is_cosplay and not strong_nonstandard and _has_unbalanced_delimiters(program_name):
        msg = "最终建议仍存在未配对的括号/引号结构，不能安全自动采用，需要人工确认"
        if msg not in warnings:
            warnings.append(msg)
        safe_fallback = normalized_current if not _has_unbalanced_delimiters(normalized_current) else current
        if program_name != safe_fallback:
            program_name = safe_fallback
            reasons.append("最终建议结构校验失败，撤回高风险替换并保留当前安全名称")

    if _only_cosmetic_punctuation_change(current, program_name):
        program_name = current
        reasons.append("建议与当前名称仅有空格/标点美化差异，保留本地原写法")

    # “纯美化保留原写法”是最后阶段规则，因此结构安全必须在它之后再兜底一次。
    program_name, surplus_closer_removed_final = _drop_surplus_closing_delimiters(program_name)
    if surplus_closer_removed_final:
        reasons.append("最终安全检查移除明显多余的右括号")
    if not is_cosplay and not strong_nonstandard and _has_unbalanced_delimiters(program_name):
        msg = "最终建议仍存在未配对的括号/引号结构，不能安全自动采用，需要人工确认"
        if msg not in warnings:
            warnings.append(msg)

    # 语义警告沿用 V0.2.25 的保守触发边界，但允许“新确认的结构硬规则”执行后仍保留复核状态。
    # 这样 #3485 这类只增加结构空格的项目不会丢失 AI 风险；而下划线清洗、中文/AI 标记补全
    # 等原本就会形成普通建议修改的项目，不会因为本轮结构修复被成批误送 AI。
    candidate_exact = bool(candidate and _normalize_compare(current) == _normalize_compare(candidate))
    similarity = 0.0
    if candidate:
        similarity = SequenceMatcher(None, _normalize_compare(current), _normalize_compare(candidate)).ratio()
    review_gate_kept_local = bool(
        _normalize_compare(program_name) == _normalize_compare(current)
        or _legacy_cosmetic_change_for_review_gate(current, program_name)
    )

    if (
        candidate
        and not candidate_reliable
        and not is_cosplay
        and not strong_nonstandard
        and review_gate_kept_local
        and similarity < 0.70
        and _normalize_compare(current) != _normalize_compare(candidate)
    ):
        msg = "来源候选与当前文件名差异较大且候选可信度不足；程序保持当前名称但需要语义复核"
        if msg not in warnings:
            warnings.append(msg)

    if (
        candidate
        and not candidate_reliable
        and not is_cosplay
        and not strong_nonstandard
        and review_gate_kept_local
        and 0.70 <= similarity < 0.995
        and (
            _review_body_key(current) != _review_body_key(candidate)
            or (
                _review_body_raw(current) != _review_body_raw(candidate)
                and (("_" in _review_body_raw(current) and "&" in _review_body_raw(candidate))
                     or ("&" in _review_body_raw(current) and "_" in _review_body_raw(candidate)))
            )
        )
        and (artist_tags or group_tags or _author_like_leading_bracket(current))
    ):
        msg = "候选正文与当前文件名存在高相似但实质差异，来源不足以自动覆盖，需要人工确认"
        if msg not in warnings:
            warnings.append(msg)

    if strong_nonstandard:
        reasons.append("识别为画集：保护本地平台、ID、日期与版本结构，不使用数据库标题整体覆盖")

    # 需要理解“同一作者/同一作品/版本身份”的语义问题不再继续堆正则，
    # 单独分流到“AI未审”；普通文件完整性/重复/页数风险仍留人工复核。
    llm_review_reasons: list[str] = []
    for w in warnings:
        if (
            "缺少原版标题" in w
            or "可靠 creator 信息明显冲突" in w
            or "未验证 creator 候选" in w
            or "creator 子作者与 title_jpn 不一致" in w
            or "候选正文与当前文件名存在高相似但实质差异" in w
            or "来源候选与当前文件名差异较大且候选可信度不足" in w
            or "正式副标题" in w
            or "title / title_jpn 疑似反置" in w
            or "系列/进度范围不一致" in w
            or "系列/卷号/话数存在实质变化" in w
            or "活动/展会前缀与来源标题不一致" in w
            or "合集/范围扩大" in w
            or "疑似错配" in w
            or "修正状态存在实质差异" in w
        ):
            llm_review_reasons.append(w)
    llm_review_reasons = _dedupe(llm_review_reasons)
    semantic_review_needed = bool(llm_review_reasons)

    # V0.2.22 起：AI 路由是独立维度，不再依赖“当前分类”。
    # 这里只回答“是否仍存在本地硬规则无法确定的命名语义问题”。
    # Cosplay / 画集 / 重复候选等内容属性可以与 needs_ai 同时存在。
    # 页数差、疑似重复副本编号、复合目录等“文件完整性/去重”风险不属于 AI 重命名任务。
    needs_ai_reasons: list[str] = list(llm_review_reasons)
    for w in warnings:
        if (
            "未找到可靠 artist/group tag" in w
            or "多种内容语言" in w
            or "动态/更新中画廊" in w
            or "画廊关联或元数据可能不一致" in w
            or "未配对的括号/引号结构" in w
            or "上传者描述" in w
            or "大量下划线" in w
            or "本地与来源的修正状态存在实质差异" in w
            or "疑似错配" in w
        ):
            needs_ai_reasons.append(w)
    needs_ai_reasons = _dedupe(needs_ai_reasons)
    needs_ai = bool(needs_ai_reasons)

    # 是否属于“源数据/版本状态明显需要人工确认”。
    source_needs_review = gallery_match_risk or any(
        (
            "缺少原版标题" in w
            or "可靠 creator 信息明显冲突" in w
            or "多种内容语言" in w
            or "动态/更新中画廊" in w
            or "正式副标题" in w
            or "画廊关联或元数据可能不一致" in w
            or "未配对的括号/引号结构" in w
            or "未验证 creator 候选" in w
            or "系列/进度范围不一致" in w
            or "疑似本地重复副本编号" in w
            or "当前文件名同时存在 Decensored" in w
            or "pageCount 差距较大" in w
            or "pageCount 差距极大" in w
            or "creator 子作者与 title_jpn 不一致" in w
            or "候选正文与当前文件名存在高相似但实质差异" in w
            or "来源候选与当前文件名差异较大且候选可信度不足" in w
            or "系列/卷号/话数存在实质变化" in w
            or "活动/展会前缀与来源标题不一致" in w
            or "合集/范围扩大" in w
            or "本地与来源的修正状态存在实质差异" in w
            or "疑似错配" in w
            or (("上传者描述" in w or "大量下划线" in w) and not candidate_reliable)
        )
        for w in warnings
    )

    # V0.2.23：主分类只表示“处理建议”；Cosplay / 画集改为可叠加属性。
    attrs = [x for x in (item.attributes or []) if x not in {ATTRIBUTE_ARCHIVE, ATTRIBUTE_COSPLAY, ATTRIBUTE_GALLERY_CONFLICT}]
    if strong_nonstandard:
        attrs.append(ATTRIBUTE_ARCHIVE)
    if is_cosplay:
        attrs.append(ATTRIBUTE_COSPLAY)
    for attribute, enabled in (item.manual_attribute_overrides or {}).items():
        if enabled and attribute not in attrs:
            attrs.append(attribute)
        elif not enabled and attribute in attrs:
            attrs.remove(attribute)
    item.attributes = list(dict.fromkeys(attrs))

    output_changed = program_name.strip() != current.strip()
    unreliable_but_unchanged = bool(not candidate_reliable and not output_changed)
    if is_cosplay:
        new_category = CATEGORY_REVIEW
    elif source_needs_review:
        new_category = CATEGORY_REVIEW
    elif semantic_review_needed:
        new_category = CATEGORY_LLM_REVIEW
    elif needs_ai:
        new_category = CATEGORY_LLM_REVIEW
    elif output_changed:
        new_category = CATEGORY_SUGGESTED
    elif not swapped:
        new_category = CATEGORY_UNCHANGED
    else:
        new_category = CATEGORY_REVIEW
    if item.ai_special_flow and new_category == CATEGORY_LLM_REVIEW and item.ai_status == "AI已审":
        new_category = CATEGORY_AI_REVIEWED

    # A validated AI suggestion remains the current program version until a
    # changed input is explicitly reviewed again; ordinary restore must not
    # silently recalculate it away.
    reviewed_input = (item.ai_review_result or {}).get("input_snapshot")
    if reviewed_input:
        from .ai_review import business_input
        if business_input(item) == reviewed_input:
            program_name = item.program_suggested_name
    item.program_suggested_name = program_name
    item.program_category = new_category
    item.needs_ai = bool(needs_ai)
    if item.manual_confirmed and item.confirmed_name:
        item.suggested_name = item.confirmed_name
    elif item.name_source == "program" or not item.manual_name:
        item.name_source = "program"
        item.suggested_name = program_name
    if preserve_manual_category and item.manual_category:
        item.manual_category_value = item.manual_category_value or item.category
        item.category = item.manual_category_value
    else:
        item.category = new_category

    confidence = "高" if (
        new_category in {CATEGORY_SUGGESTED, CATEGORY_UNCHANGED}
        and not source_needs_review
    ) else ("待AI复核" if new_category == CATEGORY_LLM_REVIEW else "待人工复核")

    # 只记录“字段来自哪里”，不要求用户给 10 万本手工打可信度分。
    field_sources: dict[str, str] = {}
    if candidate_source:
        field_sources["title"] = candidate_source
    else:
        field_sources["title"] = "当前本地名称"
    if artist_tags:
        field_sources["artist"] = "E-H artist tag"
    if group_tags:
        field_sources["group"] = "E-H group tag"
    if is_chinese:
        field_sources["translation"] = "E-H language:chinese"
    elif explicit_translated_cn_marker:
        field_sources["translation"] = "标题/文件名明确中国翻訳标记"
    elif explicit_translation_word:
        field_sources["translation"] = "标题/文件名明确翻译/汉化说明"
    if has_uncensored_tag:
        field_sources["censorship"] = "E-H other:uncensored"
    elif _censor_states(current):
        field_sources["censorship"] = "当前文件名修正状态标记"
    elif _censor_states(record.title_jpn or record.title or ""):
        field_sources["censorship"] = "数据库标题修正状态标记"
    if item.manual_confirmed:
        field_sources["final_name"] = "人工确认"

    analysis = {
        "analyzer_version": ANALYZER_VERSION,
        "title_kind": title_kind,
        "title_jpn_kind": title_jpn_kind,
        "title_fields_swapped_suspected": swapped,
        "metadata_candidate": candidate,
        "metadata_candidate_source": candidate_source,
        "metadata_candidate_reliable": candidate_reliable,
        "metadata_similarity": round(similarity, 4),
        "leading_name_block": _author_like_leading_bracket(current),
        "artist_tags": artist_tags,
        "group_tags": group_tags,
        "parody_tags": parody_tags,
        "language_tags": language_tags,
        "other_tags": other_tags,
        "is_chinese": is_chinese,
        "is_translated": is_translated,
        "is_chinese_translation": is_chinese_translation,
        "chinese_original": chinese_original,
        "creator_field_conflict": creator_conflict,
        "creator_parenthesis_fixed": creator_parenthesis_fixed,
        "bare_native_creator_fixed": bare_native_creator_fixed,
        "decorative_native_creator_fixed": decorative_native_creator_fixed,
        "local_creator_conflict": local_creator_conflict,
        "creator_placeholder_override": creator_placeholder_override,
        "current_dot_creator_fixed": current_dot_creator_fixed,
        "missing_native_creator": missing_native_creator,
        "creator_fill_block": creator_fill_block,
        "leading_parenthesis": convention_block,
        "leading_parenthesis_type": convention_type,
        "has_chinese_marker": _has_explicit_chinese_block(current),
        "has_no_text_language": has_no_text_language,
        "gallery_match_risk": gallery_match_risk,
        "unreliable_but_unchanged": unreliable_but_unchanged,
        "title_candidate_can_extend_local": title_candidate_can_extend_local,
        "creator_bridge_without_tags": creator_bridge_without_tags,
        "rough_translation": rough_translation,
        "ai_generated": ai_generated,
        "has_uncensored_tag": has_uncensored_tag,
        "extraneous_ads": extraneous_ads,
        "page_diff": diff,
        "page_diff_ratio": ratio,
        "page_diff_level": page_level,
        "effective_local_page_count": effective_local_page_count,
        "composite_manga_dir": composite_manga_dir,
        "direct_image_count": (item.extra or {}).get("direct_image_count") if isinstance(item.extra, dict) else None,
        "nested_image_count": (item.extra or {}).get("nested_image_count") if isinstance(item.extra, dict) else None,
        "nested_dir_count": (item.extra or {}).get("nested_dir_count") if isinstance(item.extra, dict) else None,
        "auto_nonstandard": strong_nonstandard,
        "auto_cosplay": is_cosplay,
        "attributes": list(item.attributes),
        "dynamic_progress_mismatch": progress_mismatch,
        "source_progress_conflict": source_progress_conflict,
        "series_identity_conflict": series_identity_conflict,
        "event_conflict": event_conflict,
        "censor_state_conflict": censor_state_conflict,
        "primary_identity_conflict": primary_identity_conflict,
        "generic_source_overwrite_risk": generic_source_overwrite_risk,
        "copy_suffix_risk": copy_suffix_risk,
        "copy_suffix_raw": copy_suffix_raw,
        "needs_ai": needs_ai,
        "needs_ai_reasons": needs_ai_reasons,
        "llm_review_reasons": llm_review_reasons,  # 兼容 V0.2.21 会话/报告字段
        "local_progress_signatures": local_progress,
        "remote_progress_signatures": remote_progress,
        "local_extra_title_text": local_extra_title_text,
        "confidence": confidence,
        "field_sources": field_sources,
        "remove_event_prefix": remove_event_prefix,
        "reasons": _dedupe(reasons),
        "warnings": _dedupe(warnings),
        "flags": _dedupe(flags),
    }
    item.extra = dict(item.extra or {})
    item.extra["needs_ai"] = bool(item.needs_ai)
    item.extra["analysis"] = analysis

    compact: list[str] = []
    for warning in analysis["warnings"]:
        compact.append(warning)
        if len(compact) >= 2:
            break
    if len(analysis["warnings"]) > 2:
        compact.append(f"另有 {len(analysis['warnings']) - 2} 项分析警告")
    item.warning = "；".join(compact)
    return item

def _gallery_identity(url: str) -> str:
    value = (url or "").strip()
    m = re.search(r"/g/(\d+)/([0-9a-f]+)/?", value, re.IGNORECASE)
    if m:
        return f"{m.group(1)}/{m.group(2).casefold()}"
    return value.casefold().rstrip("/")


def _reliable_history_group(group: list[WorkItem]) -> bool:
    """Accept only explicit progress revisions of one otherwise identical name."""
    forms = []
    for item in group:
        name = item.original_name.casefold()
        markers = re.findall(r"(?<!\d)\d+\s*[-–~～]\s*\d+(?!\d)|\b(?:ongoing|completed|old|complete)\b", name)
        if not markers:
            return False
        body = re.sub(r"(?<!\d)\d+\s*[-–~～]\s*\d+(?!\d)|\b(?:ongoing|completed|old|complete)\b", "", name)
        body = re.sub(r"[\s\W_]+", "", body)
        if len(body) < 8:
            return False
        forms.append((body, tuple(markers)))
    return len({body for body, _ in forms}) == 1 and len({marks for _, marks in forms}) > 1


def _refresh_compact_warning(item: WorkItem) -> None:
    analysis = (item.extra or {}).get("analysis", {})
    warnings = list(analysis.get("warnings") or [])
    compact = warnings[:2]
    if len(warnings) > 2:
        compact.append(f"另有 {len(warnings) - 2} 项分析警告")
    item.warning = "；".join(compact)


def analyze_items(
    items: list[WorkItem],
    preserve_manual_category: bool = True,
    options: dict | None = None,
) -> list[WorkItem]:
    for item in items:
        analyze_item(item, preserve_manual_category=preserve_manual_category, options=options)

    # 全局一致性检查：一个 E-H Gallery 同时关联多个不同本地目录时，不猜哪一本正确，只送复核。
    by_gallery: dict[str, list[WorkItem]] = {}
    for item in items:
        if not item.record or not item.record.url:
            continue
        key = _gallery_identity(item.record.url)
        if key:
            by_gallery.setdefault(key, []).append(item)

    for key, group in by_gallery.items():
        distinct_paths = {x.original_path.casefold() for x in group if x.original_path}
        if len(distinct_paths) <= 1:
            continue
        if _reliable_history_group(group):
            continue
        for item in group:
            analysis = (item.extra or {}).setdefault("analysis", {})
            warnings = list(analysis.get("warnings") or [])
            msg = f"画廊关联冲突：同一 E-H Gallery（{key}）关联多个本地项目，当前无法可靠确认历史版关系"
            if msg not in warnings:
                warnings.append(msg)
            analysis["warnings"] = warnings
            flags = list(analysis.get("flags") or [])
            if "gallery_conflict" not in flags:
                flags.append("gallery_conflict")
            analysis["flags"] = flags
            analysis["source_trust"] = "untrusted"
            analysis["source_trust_reason"] = msg
            analysis["hard_risks"] = list(dict.fromkeys(list(analysis.get("hard_risks") or []) + [msg]))
            item.attributes = list(dict.fromkeys(list(item.attributes or []) + [ATTRIBUTE_GALLERY_CONFLICT]))
            item.program_category = CATEGORY_REVIEW
            if not item.manual_category:
                item.category = CATEGORY_REVIEW
            _refresh_compact_warning(item)
    return items
