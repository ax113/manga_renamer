"""Per-attempt costs using a saved tariff. Unknown usage stays unknown."""
from __future__ import annotations
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

PRICE_DATE = '2026-10-04'
HOLIDAY_SOURCE = 'https://www.gov.cn/zhengce/zhengceku/202511/content_7047091.htm'
QWEN_SOURCE = 'https://help.aliyun.com/zh/model-studio/qwen3-7-flash'
DS_SOURCE = 'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/'
# Dates affecting DS peaks in 2026. Outside the documented year, require an explicit schedule.
HOLIDAYS_2026 = {'2026-01-01','2026-01-02','2026-01-03','2026-02-15','2026-02-16','2026-02-17',
 '2026-02-18','2026-02-19','2026-02-20','2026-02-21','2026-02-22','2026-02-23',
 '2026-04-04','2026-04-05','2026-04-06','2026-05-01','2026-05-02','2026-05-03','2026-05-04','2026-05-05',
 '2026-06-19','2026-06-20','2026-06-21','2026-09-25','2026-09-26','2026-09-27',
 '2026-10-01','2026-10-02','2026-10-03','2026-10-04','2026-10-05','2026-10-06','2026-10-07'}


def official_tariff(config, kind):
    from urllib.parse import urlsplit
    model = str(config.get('model', '')).lower()
    host = (urlsplit(str(config.get('endpoint', ''))).hostname or '').lower()
    if kind == 'qwen37_beijing':
        if model not in {'qwen3.7-flash','qwen3.7-flash-2026-07-15'} or not (host in {'dashscope.aliyuncs.com','maas.qianwenaiapi.com'} or host.endswith('.cn-beijing.maas.aliyuncs.com')):
            raise ValueError('此预设仅适用已核验官方地域的Qwen3.7-Flash接口；其他渠道请手动填写')
        tiers = [{'max_input':32768,'input':'0.2','cached':'0.04','output':'0.8'},
                 {'max_input':262144,'input':'0.6','cached':'0.12','output':'2.4'},
                 {'max_input':1000000,'input':'1.2','cached':'0.24','output':'4.8'}]
        return {'kind':'tiered','currency':'CNY','tiers':tiers,'source':QWEN_SOURCE,'checked_at':PRICE_DATE,'label':'Qwen3.7-Flash'}
    if kind == 'deepseek':
        if host != 'api.deepseek.com':
            raise ValueError('此预设仅适用DeepSeek官方接口；其他渠道请手动填写')
        if model in {'deepseek-flash','deepseek-v4-flash','deepseek-v4-flash-vision-exp'}:
            peak = {'input':'2','cached':'0.04','output':'8'}
        elif model == 'deepseek-v4-pro':
            peak = {'input':'9','cached':'0.30','output':'27'}
        else:
            raise ValueError('此模型不在已核验的DeepSeek价格预设内，请手动填写')
        return {'kind':'ds_peak','currency':'CNY','tiers':[{'max_input':1000000,**peak}],
                'schedule_year':2026,'holidays':sorted(HOLIDAYS_2026),'holiday_source':HOLIDAY_SOURCE,'source':DS_SOURCE,'checked_at':PRICE_DATE,'label':'DeepSeek官方 峰谷价'}
    raise ValueError('无效的价格预设')


def price_preset(kind):
    """Load a named estimate independently of the connection being edited."""
    if kind == 'qwen37_beijing':
        return official_tariff({'endpoint':'https://dashscope.aliyuncs.com/v1',
                                'model':'qwen3.7-flash'}, kind)
    if kind in {'deepseek', 'deepseek_pro'}:
        model = 'deepseek-v4-pro' if kind == 'deepseek_pro' else 'deepseek-v4-flash'
        tariff = official_tariff({'endpoint':'https://api.deepseek.com','model':model}, 'deepseek')
        tariff['label'] = 'DeepSeek V4-' + ('Pro' if kind == 'deepseek_pro' else 'Flash') + ' 峰谷价'
        return tariff
    raise ValueError('无效的价格预设')


def validate_tariff(tariff):
    if not tariff:
        return {}
    if not isinstance(tariff, dict) or tariff.get('kind') not in {'flat','tiered','ds_peak'}:
        raise ValueError('费用价格格式无效')
    if tariff.get('currency') not in {'CNY','USD'}:
        raise ValueError('计价币种应为人民币或美元')
    tiers = tariff.get('tiers')
    if not isinstance(tiers, list) or not tiers:
        raise ValueError('请填写输入、缓存、输出价格')
    prior = 0
    for tier in tiers:
        limit = tier.get('max_input')
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= prior:
            raise ValueError('输入价格档上限必须递增')
        prior = limit
        for key in ('input','cached','output'):
            try:
                value = Decimal(str(tier[key]))
            except (KeyError, InvalidOperation):
                raise ValueError('请填写非负单价（每百万Token）') from None
            if not value.is_finite() or value < 0:
                raise ValueError('请填写非负单价（每百万Token）')
    return copy.deepcopy(tariff)


def preset_state(settings):
    saved = settings.get('ai_price_presets')
    if isinstance(saved,list):
        rows = []
        for row in copy.deepcopy(saved):
            tariff = row.get('pricing', {})
            # Split existing presets using their saved rates, including user edits.
            # Task/config snapshots are deliberately never rewritten here.
            if tariff.get('kind') == 'tiered':
                for i,tier in enumerate(tariff['tiers']):
                    name = row['name'] + f" 第{i+1}档（输入≤{tier['max_input']:,}）"
                    rows.append({'id':row['id']+f'_tier_{i+1}','name':name,
                                 'pricing':{**copy.deepcopy(tariff),'kind':'flat','label':name,'tiers':[copy.deepcopy(tier)]}})
            elif tariff.get('kind') == 'ds_peak':
                for peak in (True,False):
                    name = row['name'].replace('峰谷','').strip() + (' 峰' if peak else ' 谷')
                    tier = copy.deepcopy(tariff['tiers'][0])
                    if not peak:
                        for key in ('input','cached','output'): tier[key] = str(Decimal(tier[key])/2)
                    rows.append({'id':row['id']+('_peak' if peak else '_valley'),'name':name,
                                 'pricing':{**copy.deepcopy(tariff),'kind':'flat','label':name,'tiers':[tier]}})
            else: rows.append(row)
        return rows
    rows = []
    tariff = price_preset('qwen37_beijing')
    for i,tier in enumerate(tariff['tiers']):
        name = f"Qwen3.7-Flash 第{i+1}档（输入≤{tier['max_input']:,}）"
        fixed = {**copy.deepcopy(tariff),'kind':'flat','label':name,'tiers':[copy.deepcopy(tier)]}
        rows.append({'id':f'qwen37_tier_{i+1}','name':name,'pricing':fixed})
    for model in ('deepseek','deepseek_pro'):
        tariff = price_preset(model)
        for peak in (True,False):
            name = 'DeepSeek V4-' + ('Pro' if model=='deepseek_pro' else 'Flash') + (' 峰' if peak else ' 谷')
            tier = copy.deepcopy(tariff['tiers'][0])
            if not peak:
                for key in ('input','cached','output'): tier[key] = str(Decimal(tier[key])/2)
            fixed = {'kind':'flat','currency':'CNY','label':name,'tiers':[tier],
                     'source':tariff['source'],'checked_at':tariff['checked_at']}
            rows.append({'id':model+('_peak' if peak else '_valley'),'name':name,'pricing':fixed})
    return rows


def validate_presets(rows):
    if not isinstance(rows,list): raise ValueError('费用预设格式无效')
    ids = set()
    result = []
    for row in rows:
        if not isinstance(row,dict) or not isinstance(row.get('id'),str) or not row['id'] or row['id'] in ids:
            raise ValueError('费用预设编号无效或重复')
        name = str(row.get('name','')).strip()
        if not name or len(name)>80: raise ValueError('请填写1到80个字符的费用预设名称')
        ids.add(row['id'])
        pricing = validate_tariff(row.get('pricing',{}))
        if not pricing: raise ValueError('费用预设须填写价格')
        result.append({'id':row['id'],'name':name,'pricing':pricing})
    return result


def _peak(at, tariff):
    at = at.astimezone(timezone(timedelta(hours=8)))
    if at.year != tariff.get('schedule_year'):
        raise ValueError('计价日历未覆盖此年')
    minute = at.hour*60+at.minute
    return at.weekday()<5 and at.date().isoformat() not in tariff['holidays'] and (540<=minute<720 or 840<=minute<1080)


def _token(value):
    return isinstance(value,int) and not isinstance(value,bool) and value>=0


def estimate_cost(task):
    tariff = task.get('config', {}).get('pricing')
    if not tariff:
        return {'configured':False,'label':'未配置价格'}
    try:
        validate_tariff(tariff)
    except ValueError:
        return {'configured':False,'label':'计价依据无效，无法估算'}
    amount, missing, known_components = Decimal(0), 0, 0
    requests = task.get('requests', [])
    for req in requests:
        usage = req.get('usage') or {}
        incoming, output, hit = usage.get('input'), usage.get('output'), usage.get('cache_hit')
        tier = tariff['tiers'][0] if tariff['kind']=='flat' else next((t for t in tariff['tiers'] if _token(incoming) and incoming<=t['max_input']),None)
        if tier is None:
            missing += 1; continue
        multiplier = Decimal(1)
        if tariff['kind'] == 'ds_peak':
            try:
                start = datetime.fromisoformat(req['started_at'])
                end = datetime.fromisoformat(req.get('finished_at',req['started_at']))
                if start.tzinfo is None or end.tzinfo is None or end<start:
                    raise ValueError('请求时间缺少可靠时区')
                peak = _peak(start,tariff)
                # Any boundary within the interval leaves this attempt's fee unknown.
                cursor = start
                while cursor<=end:
                    if _peak(cursor,tariff)!=peak: raise ValueError('请求跨峰谷边界')
                    cursor += timedelta(minutes=1)
                    if cursor-start>timedelta(days=2): raise ValueError('跨日请求计价无法确认')
                if _peak(end,tariff)!=peak: raise ValueError('请求跨峰谷边界')
                multiplier = Decimal(1) if peak else Decimal('0.5')
            except (KeyError,ValueError,TypeError):
                missing += 1; continue
        unit = Decimal(1000000)
        input_rate,cached_rate = Decimal(str(tier['input'])),Decimal(str(tier['cached']))
        incomplete = False
        if _token(incoming) and not usage.get('cache_write'):
            if _token(hit) and hit<=incoming:
                amount += (Decimal(incoming-hit)*input_rate+Decimal(hit)*cached_rate)*multiplier/unit
                known_components += 1
            elif input_rate==cached_rate:
                amount += Decimal(incoming)*input_rate*multiplier/unit
                known_components += 1
            else:
                incomplete = True
        else:
            incomplete = True
        if _token(output):
            amount += Decimal(output)*Decimal(str(tier['output']))*multiplier/unit
            known_components += 1
        else:
            incomplete = True
        if incomplete: missing += 1
    returned = {(str(r.get('group')),r.get('attempt')) for r in requests}
    unreturned = sum(status.get('phase')=='requesting' and (str(group),status.get('attempt')) not in returned
                     for group,status in task.get('group_status',{}).items())
    missing += unreturned
    currency = tariff['currency']
    label = f"{amount:.6f} {'元' if currency=='CNY' else '美元'}"
    if not requests and not unreturned:
        label = '尚无请求用量（等待返回）'
    elif missing and not known_components:
        label = f'无法估算（{missing}次缺价格、用量或时段依据）'
    elif missing:
        label = '已知部分 '+label+f'（估算不完整：{missing}次缺依据）'
    elif task.get('state') in {'running','queued'}:
        label = '已返回用量 '+label+'（任务进行中）'
    return {'configured':True,'amount':str(amount) if known_components or (not requests and not unreturned) else None,'currency':currency,
            'incomplete_requests':missing,'unreturned_groups':unreturned,'label':label,'pricing':copy.deepcopy(tariff)}


def cost_line(task):
    return '本轮费用（估算）：'+estimate_cost(task)['label']
