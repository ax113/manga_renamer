"""Usage supplied by the provider, with missing values kept explicit."""
from __future__ import annotations

FIELDS = ("input", "cache_hit", "cache_miss", "output", "reasoning", "answer")


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def response_usage(response: dict) -> dict:
    usage = response.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    prompt_detail = usage.get("prompt_tokens_details")
    prompt_detail = prompt_detail if isinstance(prompt_detail, dict) else {}
    output_detail = usage.get("completion_tokens_details")
    output_detail = output_detail if isinstance(output_detail, dict) else {}
    incoming = _count(usage.get("prompt_tokens"))
    hit = _count(usage.get("prompt_cache_hit_tokens", prompt_detail.get("cached_tokens")))
    miss = _count(usage.get("prompt_cache_miss_tokens"))
    if miss is None and incoming is not None and hit is not None and hit <= incoming:
        miss = incoming - hit
    output = _count(usage.get("completion_tokens"))
    reasoning = _count(output_detail.get("reasoning_tokens", usage.get("reasoning_tokens")))
    answer = output - reasoning if output is not None and reasoning is not None and reasoning <= output else None
    result = dict(input=incoming, cache_hit=hit, cache_miss=miss, output=output,
                  reasoning=reasoning, answer=answer)
    cache_write = _count(usage.get("cache_creation_input_tokens", prompt_detail.get("cache_creation_tokens")))
    if cache_write is not None:
        result["cache_write"] = cache_write
    return result


def aggregate_usage(requests: list[dict]) -> dict:
    result = {}
    for field in FIELDS:
        values = [_count((row.get("usage") or {}).get(field)) for row in requests]
        known = [value for value in values if value is not None]
        result[field] = {"known_total": sum(known), "missing_requests": len(values) - len(known),
                         "total": sum(known) if known and len(known) == len(values) else None}
    pairs = [(row.get("usage", {}).get("input"), row.get("usage", {}).get("cache_hit")) for row in requests]
    pairs = [(incoming, hit) for incoming, hit in pairs if _count(incoming) is not None
             and _count(hit) is not None and incoming >= hit]
    denominator = sum(incoming for incoming, _ in pairs)
    result["cache_rate"] = sum(hit for _, hit in pairs) / denominator if denominator else None
    result["cache_rate_requests"] = len(pairs)
    return result


def usage_lines(requests: list[dict]) -> list[str]:
    totals = aggregate_usage(requests)
    def value(field):
        item = totals[field]
        if item["total"] is not None:
            return f"{item['total']:,}"
        if item["missing_requests"] == len(requests):
            return "未提供"
        return f"已知 {item['known_total']:,}（缺 {item['missing_requests']} 次）"
    rate = totals["cache_rate"]
    rate_text = f"{rate:.1%}" if rate is not None else "未提供"
    if rate is not None and totals["cache_rate_requests"] != len(requests):
        rate_text += "（仅已知请求）"
    return [f"实际请求：{len(requests)} 次（含失败与重试）",
            f"输入 token：{value('input')}｜缓存命中：{value('cache_hit')}｜未命中：{value('cache_miss')}｜命中率：{rate_text}",
            f"输出 token：{value('output')}｜思考：{value('reasoning')}｜正式答案：{value('answer')}（输出减思考）"]
