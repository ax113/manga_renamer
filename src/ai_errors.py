"""Provider errors retain their code while sharing bounded client behavior."""
from __future__ import annotations

from .run_log import _redact


def error_details(body: dict, http_status: int | None, request_id: str = "", *, provider: str = "generic") -> dict:
    error = body.get("error")
    error = error if isinstance(error, dict) else body
    code = _redact(str(error.get("code") or error.get("type") or ""))[:100]
    message = _redact(str(error.get("message") or (body.get("error") if isinstance(body.get("error"), str) else "") or ""))
    rid = _redact(str(body.get("request_id") or body.get("requestId") or request_id or body.get("id") or ""))[:150]
    clue = (code + " " + message).lower()
    transient, global_error = False, False
    # Qwen documents insufficient_quota under HTTP 429 as a TPM/TPS limit;
    # other providers can use that same code for exhausted account credit.
    qwen_rate_quota = provider == "qwen" and http_status == 429 and code.lower() == "insufficient_quota"
    exhausted = (any(x in clue for x in ("arrearage", "quota_exhausted", "free_tier", "freetier", "out_of_service", "balance", "payment", "quota exhausted", "free allocated quota exceeded"))
                 or ("insufficient_quota" in clue and not qwen_rate_quota))
    if any(x in clue for x in ("datainspectionfailed", "data_inspection_failed", "content_filter", "inappropriate", "safety_check")):
        category = "接口内容检查未通过"
    elif exhausted or http_status == 402:
        category, global_error = "接口账户余额或额度不足", True
    elif http_status in (401, 403) or any(x in clue for x in ("invalid_api_key", "invalidapikey", "accessdenied", "unauthorized", "forbidden")):
        category, global_error = "接口认证或权限错误", True
    elif any(x in clue for x in ("context_length_exceeded", "range of input length", "input length", "maximum context")):
        category = "接口输入超过模型长度上限"
    elif http_status == 429 or any(x in clue for x in ("throttling", "rate_limit", "limitrequests", "limit_requests", "resourceexhausted", "too many requests")):
        category, transient = "接口暂时限流", True
    elif http_status in (400, 404, 422) or any(x in clue for x in ("invalidparameter", "invalid_parameter", "invalid_request", "model_not_found", "not_support", "does not support")):
        category, global_error = "接口地址、模型或参数不支持", True
    elif (http_status is not None and (http_status >= 500 or http_status == 408)) or any(x in clue for x in ("internalerror", "internal_error", "serviceunavailable", "requesttimeout")):
        category, transient = "接口服务暂时异常", True
    else:
        category = "接口返回错误"
    marker = "｜".join(x for x in (f"HTTP {http_status}" if http_status else "", code) if x)
    text = category + (f"（{marker}）" if marker else "")
    if message:
        text += "：" + " ".join(message.split())[:240]
    return dict(message=text, api_code=code, provider_message=message[:1500], request_id=rid,
                transient=transient, global_error=global_error, http_status=http_status)
