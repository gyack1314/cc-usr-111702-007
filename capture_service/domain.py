"""解释追溯码的分页与流通事件。

事件身份是 event_id：同一身份只追加证据，绝不被较新接收的页面覆盖。
event_time 是业务时间，received_at 是本地可见时间，两者分别留证。
退货、拆包属于流通环节，永远不会建立或改写批次生产关联。
"""
import hashlib
import json
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

# 只有生产侧事件可以建立“追溯码 -> 批号”的绑定。
# pack 是上市许可持有人的赋码包装，属于生产侧；repack 是流通再包装，不属于。
PRODUCTION_KINDS = frozenset({'produce', 'pack'})
RETURN_KINDS = frozenset({'return'})
UNPACK_KINDS = frozenset({'unpack'})
PACKAGING_KINDS = frozenset({'pack', 'repack'})
KNOWN_KINDS = PRODUCTION_KINDS | RETURN_KINDS | UNPACK_KINDS | frozenset({
    'repack', 'inbound', 'outbound', 'ship', 'transfer',
})

_EVENT_REQUIRED = ('event_id', 'kind', 'batch', 'parent', 'event_time', 'received_at')


def parse_page(raw):
    """严格解析一页来源原文，失败抛 ValueError 而不是猜测结构。"""
    try:
        document = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError('来源原文不是合法 JSON: %s' % exc) from exc
    if not isinstance(document, dict):
        raise ValueError('来源原文顶层不是对象')
    code = document.get('code')
    page = document.get('page')
    if not isinstance(code, str) or not code:
        raise ValueError('来源原文缺少追溯码 code')
    if not isinstance(page, (str, int)) or isinstance(page, bool) or str(page) == '':
        raise ValueError('来源原文缺少分页号 page')
    next_token = document.get('next')
    if next_token is not None and not isinstance(next_token, str):
        raise ValueError('续页令牌 next 只能是字符串或 null')
    raw_events = document.get('events')
    if not isinstance(raw_events, list):
        raise ValueError('来源原文 events 不是数组')
    events = []
    for index, event in enumerate(raw_events):
        if not isinstance(event, dict):
            raise ValueError('第 %d 个事件不是对象' % index)
        for field in _EVENT_REQUIRED:
            value = event.get(field)
            if not isinstance(value, str) or not value:
                raise ValueError('事件 %s 缺少必填字段 %s' % (event.get('event_id', '#' + str(index)), field))
        node = event.get('node')
        if node is not None and (not isinstance(node, str) or not node):
            raise ValueError('事件 %s 的企业节点 node 非法' % event['event_id'])
        if event['kind'] not in KNOWN_KINDS:
            raise ValueError('事件 %s 的类型 %s 不在约定内' % (event['event_id'], event['kind']))
        events.append(normalize_event(event))
    return {
        'code': code,
        'page_id': str(page),
        'next_token': next_token,
        'closed': next_token is None,
        'events': events,
    }


def normalize_event(event):
    """保留全部留证字段；node 缺省为 None，不凭空补企业节点。"""
    return {
        'event_id': event['event_id'],
        'kind': event['kind'],
        'batch': event['batch'],
        'parent': event['parent'],
        'node': event.get('node'),
        'event_time': event['event_time'],
        'received_at': event['received_at'],
    }


def classify(kind):
    if kind in PRODUCTION_KINDS:
        return 'production'
    if kind in RETURN_KINDS:
        return 'return'
    if kind in UNPACK_KINDS:
        return 'unpack'
    return 'circulation'


def event_content_hash(event):
    """同一 event_id 的内容指纹：任一字段变化都会冲突，包括接收时间。"""
    body = json.dumps(normalize_event(event), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


def page_content_hash(parsed):
    """整页指纹，用于发现同一分页号返回了不同内容。"""
    body = json.dumps(
        {'page_id': parsed['page_id'], 'next_token': parsed['next_token'],
         'events': [event_content_hash(e) for e in parsed['events']]},
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


def split_scope(url):
    """拆出冻结查询范围、追溯码与当前游标。

    返回 (scope_url, code, cursor)：scope_url 保留 code 等固定查询、
    仅去掉 page 游标；cursor 为 None 表示首页请求。
    游标令牌本身按平台原文透传，不解释结构。
    """
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise ValueError('来源地址非法')
    if parsed.username or parsed.password:
        raise ValueError('来源地址不得携带凭据')
    query = parse_qsl(parsed.query, keep_blank_values=True)
    code = None
    cursor = None
    kept = []
    for key, value in query:
        if key == 'code':
            code = value
            kept.append((key, value))
        elif key == 'page':
            cursor = value
        else:
            kept.append((key, value))
    if not code:
        raise ValueError('查询范围缺少追溯码 code')
    scope_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                            urlencode(kept), parsed.fragment))
    return scope_url, code, cursor


def join_cursor(scope_url, next_token):
    """把平台给出的续页令牌接到冻结范围上，令牌原文不做改写。"""
    separator = '&' if urlsplit(scope_url).query else '?'
    return scope_url + separator + next_token


def assess_chain(code, events, pages, quarantines, segment):
    """从已确认的采集边界评估单个码的证据链。

    缺口分两类，绝不混同：
    - platform_pending：分段尚未闭合，平台还有续页没有返回；
    - review_pending：原文已收到但存在冲突/失效，需人工核验，不得推断下游。
    """
    reasons = []
    gaps = []

    established_batch = None
    for event in sorted(events, key=lambda e: (e['event_time'], e['event_id'])):
        kind = event['kind']
        if classify(kind) == 'production':
            if established_batch is None:
                established_batch = event['batch']
            elif event['batch'] != established_batch:
                reasons.append('生产侧事件声明了不同批号: %s != %s'
                               % (event['batch'], established_batch))
            continue
        # 流通事件（退货/拆包/出入库）不建立也不改写批号绑定。
        if established_batch is not None and event['batch'] != established_batch:
            reasons.append('%s 事件 %s 携带批号 %s 与已确认生产批号 %s 冲突'
                           % (classify(kind), event['event_id'],
                              event['batch'], established_batch))
        if classify(kind) == 'return':
            reasons.append('退货事件 %s 之后的去向平台尚未闭环，不推断下游'
                           % event['event_id'])

    for item in quarantines:
        reasons.append('待核原文: %s（%s）' % (item['reason'], item['detail']))
        gaps.append({'kind': 'review_pending', 'reason': item['reason'],
                     'detail': item['detail'], 'receipt_sha256': item['receipt_sha256']})

    if segment is not None and not segment['closed']:
        if segment['blocked']:
            gaps.append({'kind': 'review_pending', 'reason': segment['blocked_reason'],
                         'scope': segment['next_cursor'], 'cursor': segment['cursor']})
        elif segment['next_cursor']:
            gaps.append({'kind': 'platform_pending', 'scope': segment['next_cursor'],
                         'cursor': segment['cursor']})

    publishable = (
        segment is not None
        and segment['closed']
        and not segment['blocked']
        and not quarantines
        and not reasons
    )
    return {
        'code': code,
        'publishable': publishable,
        'reasons': reasons,
        'gaps': gaps,
        'event_count': len(events),
        'page_count': len(pages),
        'quarantine_count': len(quarantines),
        'established_batch': established_batch,
    }
