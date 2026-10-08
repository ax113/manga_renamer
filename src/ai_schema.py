"""Qwen's documented JSON Schema subset; the shared local validator stays final."""
from __future__ import annotations

import json


SCHEMA_VERSION = "manga_review_complete_v2"

REVIEW_GUIDANCE = (
    "\n返回前逐本核对：先用 CONTROL.local_id 定位当前输入，再独立处理这本漫画。"
    "resolved_reasons/unresolved_reasons 只能复制这一条 STATE.review_reasons 的原文，"
    "不能拿同组其他漫画的原因代替；每个原因恰好归入一个数组。reason 写本条的事实与判断，不能用‘同上’代替。"
    "每条都返回 suggested_name 字符串：CHANGE 写完整可用名称，不得只在 reason 中描述修改；"
    "KEEP/UNCERTAIN 写空字符串。无法给出可靠完整修改名时用 UNCERTAIN，并说明缺少什么证据。"
    "\n重申已有规则边界：remove_event_prefix=true 只是允许删除已明确识别的活动/展会前缀，"
    "不授权删除普通日期或标题。AI Generated 表示绘图生成，不能据此判为中文原创或否定可靠的中文翻译证据；"
    "[AI翻译] 和明确中文翻译身份按原规则分别判断。"
    "不得把 SOURCE 的字段名当作语言结论，应查看字段的实际内容；"
    "保留本地 Digital/DL 等来源身份，不能仅因来源标题或‘通常更准确’而删除或互换。"
)


def review_response_format(inputs: list[dict]) -> dict:
    # Enum constrains spelling, not relationships between fields. The existing
    # validator must still enforce each comic's exact four-field identity,
    # uniqueness, reason coverage and safe application. Do not repair responses.
    controls = [entry["CONTROL"] for entry in inputs]
    reason_map = {entry["CONTROL"]["local_id"]: entry["STATE"]["review_reasons"] for entry in inputs}
    allowed_reasons = list(dict.fromkeys(reason for reasons in reason_map.values() for reason in reasons))
    reason_items = {"type": "string"}
    if allowed_reasons:
        reason_items["enum"] = allowed_reasons
    properties = {
        key: {"type": "integer" if key == "review_round" else "string",
              "enum": list(dict.fromkeys(ctrl[key] for ctrl in controls))}
        for key in ("task_id", "review_round", "local_id", "input_fingerprint")
    }
    properties.update({
        "review_complete": {"type": "boolean", "enum": [True]},
        "name_decision": {"type": "string", "enum": ["KEEP", "CHANGE", "UNCERTAIN"]},
        "reason": {"type": "string", "description": "独立说明本条漫画的事实与判断，不写‘同上’。"},
        "resolved_reasons": {"type": "array", "items": dict(reason_items),
                             "description": "只使用本条 local_id 对应的待审原因原文，不套用其他漫画；已解决的原因。"},
        "unresolved_reasons": {"type": "array", "items": dict(reason_items),
                               "description": "只使用本条 local_id 对应的待审原因原文，不套用其他漫画；未解决的原因。"},
        "suggested_name": {"type": "string",
                           "description": "必填。CHANGE 时提供完整建议名，不能只描述修改；KEEP/UNCERTAIN 写空字符串。"},
        "additional_notes": {"type": "string"},
    })
    required = [key for key in properties if key != "additional_notes"]
    return {"type": "json_schema", "json_schema": {
        "name": SCHEMA_VERSION, "strict": True,
        "schema": {"type": "object",
                   "description": "逐本待审原因索引（键为 local_id），只引用当前漫画对应的原因：" + json.dumps(reason_map, ensure_ascii=False, separators=(",", ":")),
                   "properties": {
            "results": {"type": "array", "items": {
                "type": "object", "properties": properties,
                "required": required, "additionalProperties": False,
            }},
        }, "required": ["results"], "additionalProperties": False},
    }}
