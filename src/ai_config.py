"""Saved profiles and provider-specific Chat Completions options."""
from __future__ import annotations

import copy
import json
import re
import urllib.parse
import uuid

from .ai_schema import review_response_format

PROVIDERS = {"deepseek": "DeepSeek", "qwen": "Qwen", "generic": "其他兼容接口"}
THINKING_MODES = {"default": "接口默认", "enabled": "开启", "disabled": "关闭"}
QWEN_TOTAL_BUDGET = 32768
DEEPSEEK_MODELS = {'deepseek-flash','deepseek-v4-flash','deepseek-v4-flash-vision-exp','deepseek-v4-pro'}
QWEN_MODELS = {'qwen3.7-flash','qwen3.7-flash-2026-07-15','qwen3.7-max',
    'qwen3.7-max-2026-05-20','qwen3.7-max-2026-06-08','qwen3.7-plus',
    'qwen3.7-plus-2026-05-26','qwen3.8-flash','qwen3.8-max','qwen3.8-max-0902'}
QWEN_HOSTS = {'dashscope.aliyuncs.com','dashscope-intl.aliyuncs.com','dashscope-us.aliyuncs.com','maas.qianwenaiapi.com'}


def automatic_provider(config: dict) -> str:
    """Automatic parameters apply only to matching official endpoints and models."""
    host = (urllib.parse.urlsplit(str(config.get('endpoint',''))).hostname or '').lower()
    model = str(config.get('model','')).strip().lower()
    selected = config.get('provider','auto')
    if selected in {'auto','deepseek'} and host == 'api.deepseek.com' and model in DEEPSEEK_MODELS:
        return 'deepseek'
    if selected in {'auto','qwen'} and (host in QWEN_HOSTS or host.endswith('.maas.aliyuncs.com')) and model in QWEN_MODELS:
        return 'qwen'
    return 'generic'


def default_max_tokens(config: dict) -> int:
    return {'deepseek':65536,'qwen':QWEN_TOTAL_BUDGET}.get(automatic_provider(config),16000)


def parse_custom_parameters(value) -> dict:
    def pairs(rows):
        result = {}
        for key, child in rows:
            if key in result: raise ValueError('自定义请求参数有重复字段：'+key)
            result[key] = child
        return result
    if isinstance(value,str):
        try:
            value = json.loads(value or '{}',object_pairs_hook=pairs,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError('自定义请求参数不能含NaN或Infinity')))
        except (json.JSONDecodeError,TypeError) as exc:
            raise ValueError('自定义请求参数须填写完整JSON对象') from exc
    if not isinstance(value,dict) or any(not isinstance(k,str) or not k.strip() for k in value):
        raise ValueError('自定义请求参数须填写JSON对象')
    if {'model','messages','stream','key','endpoint','provider','pricing',*RUNTIME_DEFAULTS} & value.keys():
        raise ValueError('模型、消息及本机运行设置不能写入自定义请求参数')
    try: json.dumps(value,allow_nan=False)
    except (ValueError,TypeError): raise ValueError('自定义请求参数包含无效JSON值') from None
    for key in ('max_tokens','max_completion_tokens'):
        if key in value and (type(value[key]) is not int or value[key] < 1):
            raise ValueError(key+'必须为正整数')
    if 'max_tokens' in value and 'max_completion_tokens' in value:
        raise ValueError('自定义请求参数只填写一种Token上限字段')
    return copy.deepcopy(value)


def provider_for(config: dict) -> str:
    selected = config.get("provider", "auto")
    if selected != "auto":
        return selected
    host = (urllib.parse.urlsplit(str(config.get("endpoint", ""))).hostname or "").lower()
    if host == "api.deepseek.com":
        return "deepseek"
    if host in {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com", "dashscope-us.aliyuncs.com", "maas.qianwenaiapi.com"} or host.endswith(".maas.aliyuncs.com"):
        return "qwen"
    return "generic"


def default_thinking_label(config: dict) -> str:
    return '接口默认（开）' if automatic_provider(config) != 'generic' else '接口默认（未核验）'


def default_effort_label(config: dict) -> str:
    """Display only; request_options uses the saved parameter values."""
    provider = automatic_provider(config)
    if provider == 'generic': return '接口默认（未核验）'
    if thinking_mode(config) == 'disabled': return '接口默认（不适用）'
    if provider == 'deepseek': return '接口默认（高）'
    return '接口默认（更高）' if str(config.get('model','')).lower().startswith('qwen3.8-') else '接口默认（模型内置）'


def thinking_mode(config: dict) -> str:
    # Preserve r2's effective settings when importing its single configuration.
    return config.get("thinking_mode", "enabled" if provider_for(config) == "deepseek" else "default")


def modern_qwen(model: str) -> bool:
    return bool(re.fullmatch(r"qwen3\.(?:7-(?:flash|max|plus)|8-(?:flash|max))(?:-\d{4}(?:-\d{2}-\d{2})?)?", model.strip().lower()))


def request_options(config: dict, inputs: list[dict] | None = None) -> dict:
    provider, mode = automatic_provider(config), thinking_mode(config)
    if provider == "deepseek":
        options = {"max_tokens": 65536, "response_format": {"type": "json_object"}}
        if mode != "default":
            options["thinking"] = {"type": mode}
        if mode == "enabled":
            options["reasoning_effort"] = config.get("effort", "default") if config.get("effort", "default") != "default" else "high"
        elif mode == "default" and config.get("effort", "default") != "default":
            options["reasoning_effort"] = config["effort"]
        return _merge_options(config, options)
    if provider == "qwen":
        options = {"max_completion_tokens": QWEN_TOTAL_BUDGET} if modern_qwen(config.get("model", "")) else {"max_tokens": 16000}
        if mode != "default":
            options["enable_thinking"] = mode == "enabled"
        if mode != "disabled" and config.get("effort", "default") != "default":
            options["reasoning_effort"] = config["effort"]
        # Verified for 3.7/3.8 text Flash/Plus/Max, in both thinking modes.
        if modern_qwen(config.get("model", "")):
            options["response_format"] = review_response_format(inputs) if inputs else {"type": "json_object"}
        return _merge_options(config, options)
    return parse_custom_parameters(config.get('custom_parameters',{}))


def _merge_options(config, options):
    parameter = 'max_completion_tokens' if 'max_completion_tokens' in options else 'max_tokens'
    value = config.get('max_tokens', options[parameter])
    if type(value) is not int or value < 1:
        raise ValueError('最大Token必须为正整数')
    options[parameter] = value
    custom = {}  # Disabled custom fields never override adapted model options.
    if 'max_tokens' in custom: options.pop('max_completion_tokens',None)
    if 'max_completion_tokens' in custom: options.pop('max_tokens',None)
    options.update(custom)
    # Validate the effective limit after custom overrides, without changing defaults.
    provider = automatic_provider(config)
    if provider == 'deepseek' and 'max_tokens' in options and options['max_tokens'] > 393216:
        raise ValueError('DeepSeek官方最大Token不能超过393216')
    if provider == 'qwen' and options.get('max_completion_tokens',options.get('max_tokens',0)) > 131072:
        raise ValueError('当前内置Qwen型号的最大输出Token不能超过131072')
    if provider == 'qwen' and 'max_tokens' in options and options.get('enable_thinking',True) is not False and options['max_tokens'] > 32768:
        raise ValueError('Qwen思考模式的max_tokens不能超过32768；请使用max_completion_tokens')
    return options


def profile_state(settings: dict) -> tuple[list[dict], str]:
    saved = settings.get("api_profiles")
    if isinstance(saved, list):
        profiles = copy.deepcopy(saved)
        active = settings.get("api_active_profile", "")
        if not any(p.get("id") == active for p in profiles):
            active = profiles[0]["id"] if profiles else ""
        return profiles, active
    legacy = settings.get("api_config")
    if not isinstance(legacy, dict) or not legacy:
        return [], ""
    profile = {"id": "legacy", "name": "原有配置", "endpoint": legacy.get("endpoint", ""),
               "key": legacy.get("key", ""), "model": legacy.get("model", ""),
               "provider": legacy.get("provider", "auto"), "thinking_mode": thinking_mode(legacy)}
    return [profile], profile["id"]


def active_config(settings: dict) -> dict:
    profiles, active = profile_state(settings)
    return next((copy.deepcopy(p) for p in profiles if p.get("id") == active), {})


def saved_state(settings: dict, profiles: list[dict], active: str) -> dict:
    candidate = copy.deepcopy(settings)
    candidate["api_profiles"] = copy.deepcopy(profiles)
    candidate["api_active_profile"] = active
    # Keep the old active-config alias for earlier versions and existing callers.
    candidate["api_config"] = next((copy.deepcopy(p) for p in profiles if p["id"] == active), {})
    return candidate


def new_profile_id() -> str:
    return uuid.uuid4().hex


RUNTIME_DEFAULTS = {'batch_size':10,'timeout_seconds':600,'max_retries':2,'concurrency':2}
EFFORTS = {'default':'接口默认','low':'低','medium':'中','high':'高','xhigh':'更高','max':'最高'}


def runtime_values(config):
    result = {}
    for key, fallback in RUNTIME_DEFAULTS.items():
        value = config.get(key, fallback)
        if not isinstance(value, int) or isinstance(value, bool) or value < (0 if key=='max_retries' else 1):
            raise ValueError({'batch_size':'每请求本数','timeout_seconds':'超时秒数','max_retries':'重试次数','concurrency':'同时请求组数'}[key]+'必须为'+('非负整数' if key=='max_retries' else '正整数'))
        result[key] = value
    return result


def effort_choices(config):
    if automatic_provider(config) == 'deepseek':
        return ('default','low','high','max')
    if automatic_provider(config) == 'qwen' and str(config.get('model','')).lower().startswith('qwen3.8-'):
        return ('default','low','medium','xhigh')
    return ('default',)


def validate_advanced(config):
    from .ai_cost import validate_tariff
    runtime_values(config)
    effort = config.get('effort','default')
    if effort not in EFFORTS:
        raise ValueError('无效的思考强度')
    if automatic_provider(config) != 'generic' and thinking_mode(config) != 'disabled' and effort not in effort_choices(config):
        raise ValueError('该接口/模型未核实支持所选思考强度，请选择“接口默认”')
    request_options(config)
    validate_tariff(config.get('pricing', {}))
