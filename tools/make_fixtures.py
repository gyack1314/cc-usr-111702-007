"""生成冻结来源夹具：合成 JSON 原文与带 sha256 的重放清单。

仅用于测试，不访问真实监管站或企业系统。
在项目根目录执行：python tools/make_fixtures.py
"""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
ORIGIN = 'https://trace.example.test'
EMPTY_SHA = hashlib.sha256(b'').hexdigest()


def write_bin(name, document):
    raw = json.dumps(document, ensure_ascii=False, indent=2).encode('utf-8')
    (DATA / name).write_bytes(raw)
    return raw


def step(url, name, raw, status=200):
    return {
        'request': {
            'method': 'GET',
            'url': url,
            'body_sha256': EMPTY_SHA,
        },
        'response': {
            'file': name,
            'status': status,
            'bytes': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest(),
        },
    }


def write_manifest(name, allowed, steps):
    (DATA / name).write_text(
        json.dumps({'allowed_origins': allowed, 'steps': steps},
                   ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )


def url(code, token):
    base = ORIGIN + '/events?code=' + code
    return base if token is None else base + '&' + token


# BX-2：完整闭合链——生产、出库、入库、退货、重新入库；退货使链条不可发布。
bx2_p1 = write_bin('source-bx2-1.bin', {
    'code': 'BX-2', 'page': 1, 'next': 'page=2',
    'events': [
        {'event_id': 'BX2-E1', 'kind': 'produce', 'batch': 'B-02',
         'parent': 'BOX-2', 'node': 'N-PROD',
         'event_time': '2026-11-15T08:00:00Z', 'received_at': '2026-11-16T08:00:00Z'},
        {'event_id': 'BX2-E2', 'kind': 'pack', 'batch': 'B-02',
         'parent': 'BOX-2', 'node': 'N-PROD',
         'event_time': '2026-11-15T09:00:00Z', 'received_at': '2026-11-16T08:05:00Z'},
        {'event_id': 'BX2-E3', 'kind': 'outbound', 'batch': 'B-02',
         'parent': 'BOX-2', 'node': 'N-WHOLESALE',
         'event_time': '2026-11-15T12:00:00Z', 'received_at': '2026-11-16T09:00:00Z'},
    ],
})
bx2_p2 = write_bin('source-bx2-2.bin', {
    'code': 'BX-2', 'page': 2, 'next': None,
    'events': [
        {'event_id': 'BX2-E4', 'kind': 'inbound', 'batch': 'B-02',
         'parent': 'BOX-2', 'node': 'N-CLINIC',
         'event_time': '2026-11-15T15:00:00Z', 'received_at': '2026-11-16T10:00:00Z'},
        {'event_id': 'BX2-E5', 'kind': 'return', 'batch': 'B-02',
         'parent': 'BOX-2', 'node': 'N-CLINIC',
         'event_time': '2026-11-15T16:00:00Z', 'received_at': '2026-11-17T11:30:00Z'},
        {'event_id': 'BX2-E6', 'kind': 'inbound', 'batch': 'B-02',
         'parent': 'BOX-2', 'node': 'N-WHOLESALE',
         'event_time': '2026-11-15T18:00:00Z', 'received_at': '2026-11-17T11:31:00Z'},
    ],
})
write_manifest('multicode.json', [ORIGIN], [
    step(url('BX-1', 'page=1'), 'source-1.bin',
         (DATA / 'source-1.bin').read_bytes()),
    step(url('BX-1', 'page=2'), 'source-2.bin',
         (DATA / 'source-2.bin').read_bytes()),
    step(url('BX-2', 'page=1'), 'source-bx2-1.bin', bx2_p1),
    step(url('BX-2', 'page=2'), 'source-bx2-2.bin', bx2_p2),
])

# BX-3：第二段分页令牌先失效（HTTP 410），新冻结交互再给出有效续页。
bx3_p1 = write_bin('source-bx3-1.bin', {
    'code': 'BX-3', 'page': 1, 'next': 'page=2',
    'events': [
        {'event_id': 'BX3-E1', 'kind': 'produce', 'batch': 'B-03',
         'parent': 'BOX-3', 'node': 'N-PROD',
         'event_time': '2026-11-14T08:00:00Z', 'received_at': '2026-11-15T08:00:00Z'},
    ],
})
bx3_dead = write_bin('source-bx3-dead.bin', {
    'error': 'cursor_expired', 'cursor': 'page=2',
})
bx3_p2 = write_bin('source-bx3-2.bin', {
    'code': 'BX-3', 'page': 2, 'next': None,
    'events': [
        {'event_id': 'BX3-E2', 'kind': 'unpack', 'batch': 'B-03',
         'parent': 'BOX-3', 'node': 'N-WHOLESALE',
         'event_time': '2026-11-14T10:00:00Z', 'received_at': '2026-11-17T12:00:00Z'},
    ],
})
write_manifest('token-dead.json', [ORIGIN], [
    step(url('BX-3', 'page=1'), 'source-bx3-1.bin', bx3_p1),
    step(url('BX-3', 'page=2'), 'source-bx3-dead.bin', bx3_dead, status=410),
])
write_manifest('token-recover.json', [ORIGIN], [
    step(url('BX-3', 'page=1'), 'source-bx3-1.bin', bx3_p1),
    step(url('BX-3', 'page=2'), 'source-bx3-dead.bin', bx3_dead, status=410),
    step(url('BX-3', 'page=2'), 'source-bx3-2.bin', bx3_p2),
])

# BX-4：来源正文不是合法 JSON，必须隔离原文并停在边界。
malformed = b'{ "code": "BX-4", "page": 1, "events": [ broken }'
(DATA / 'source-bx4-bad.bin').write_bytes(malformed)
write_manifest('malformed.json', [ORIGIN], [
    step(url('BX-4', 'page=1'), 'source-bx4-bad.bin', malformed),
])

# BX-5：第二页重新声明同一 event_id 但内容不同（事件级冲突，分页号本身不重复）。
bx5_p1 = write_bin('source-bx5-1.bin', {
    'code': 'BX-5', 'page': 1, 'next': 'page=2',
    'events': [
        {'event_id': 'BX5-E1', 'kind': 'produce', 'batch': 'B-05',
         'parent': 'BOX-5', 'node': 'N-PROD',
         'event_time': '2026-11-13T08:00:00Z', 'received_at': '2026-11-14T08:00:00Z'},
    ],
})
bx5_p2 = write_bin('source-bx5-2.bin', {
    'code': 'BX-5', 'page': 2, 'next': None,
    'events': [
        # 同一 event_id，企业节点与接收时间被改动——必须隔离，旧证据不被覆盖。
        {'event_id': 'BX5-E1', 'kind': 'produce', 'batch': 'B-05',
         'parent': 'BOX-5', 'node': 'N-OTHER',
         'event_time': '2026-11-13T08:00:00Z', 'received_at': '2026-11-17T09:00:00Z'},
    ],
})
write_manifest('event-conflict.json', [ORIGIN], [
    step(url('BX-5', 'page=1'), 'source-bx5-1.bin', bx5_p1),
    step(url('BX-5', 'page=2'), 'source-bx5-2.bin', bx5_p2),
])

print('fixtures written to', DATA)
