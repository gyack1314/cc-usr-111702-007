"""分段收集账本：收据原文与来源化实体在同一事务内追加留证。

设计原则：
- 只追加、不覆盖。较新接收到的页面不能改写旧事件的去向。
- 冲突不回滚原文：问题原文进入 quarantine 并标注待核范围，已确认边界不动。
- 分段（segments）冻结查询范围与游标，收集器可以从最后确认边界继续。
"""
import hashlib
import sqlite3

from .domain import (
    assess_chain,
    classify,
    event_content_hash,
    join_cursor,
    page_content_hash,
    parse_page,
    split_scope,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts(
  collection TEXT NOT NULL,
  body_sha256 TEXT NOT NULL,
  source_url TEXT NOT NULL,
  status INTEGER NOT NULL,
  received_at TEXT NOT NULL,
  step INTEGER,
  raw BLOB NOT NULL,
  PRIMARY KEY(collection, body_sha256));

CREATE TABLE IF NOT EXISTS segments(
  collection TEXT NOT NULL,
  code TEXT NOT NULL,
  scope_url TEXT NOT NULL,
  cursor TEXT,
  next_cursor TEXT,
  closed INTEGER NOT NULL DEFAULT 0,
  blocked INTEGER NOT NULL DEFAULT 0,
  blocked_reason TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(collection, code));

CREATE TABLE IF NOT EXISTS page_receipts(
  collection TEXT NOT NULL,
  code TEXT NOT NULL,
  page_id TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  receipt_sha256 TEXT NOT NULL,
  next_token TEXT,
  receive_seq INTEGER NOT NULL,
  PRIMARY KEY(collection, code, page_id),
  FOREIGN KEY(collection, receipt_sha256) REFERENCES receipts(collection, body_sha256));

CREATE TABLE IF NOT EXISTS trace_events(
  collection TEXT NOT NULL,
  code TEXT NOT NULL,
  event_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  kind_class TEXT NOT NULL,
  batch TEXT NOT NULL,
  parent_code TEXT NOT NULL,
  node TEXT,
  event_time TEXT NOT NULL,
  received_at TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  receipt_sha256 TEXT NOT NULL,
  page_id TEXT NOT NULL,
  receive_seq INTEGER NOT NULL,
  PRIMARY KEY(collection, code, event_id),
  FOREIGN KEY(collection, receipt_sha256) REFERENCES receipts(collection, body_sha256));

CREATE TABLE IF NOT EXISTS quarantine(
  collection TEXT NOT NULL,
  code TEXT,
  reason TEXT NOT NULL,
  detail TEXT NOT NULL,
  source_url TEXT NOT NULL,
  receipt_sha256 TEXT NOT NULL,
  received_at TEXT NOT NULL,
  PRIMARY KEY(collection, receipt_sha256),
  FOREIGN KEY(collection, receipt_sha256) REFERENCES receipts(collection, body_sha256));
"""


def connect(path):
    c = sqlite3.connect(path)
    c.execute('PRAGMA foreign_keys=ON')
    c.executescript(SCHEMA)
    return c


def _store_receipt(c, collection, raw, receipt, received_at):
    digest = hashlib.sha256(raw).hexdigest()
    if digest != receipt['body_sha256']:
        raise ValueError('响应收据与原文不一致')
    c.execute(
        'INSERT OR IGNORE INTO receipts VALUES(?,?,?,?,?,?,?)',
        (collection, digest, receipt['url'], receipt['status'],
         received_at, receipt.get('step'), raw),
    )
    return digest


def _next_seq(c, collection):
    row = c.execute(
        'SELECT COALESCE(MAX(receive_seq), 0) FROM ('
        ' SELECT receive_seq FROM page_receipts WHERE collection=?'
        ' UNION ALL SELECT receive_seq FROM trace_events WHERE collection=?)',
        (collection, collection),
    ).fetchone()
    return row[0] + 1


def _quarantine(c, collection, code, reason, detail, url, digest, received_at):
    c.execute(
        'INSERT OR IGNORE INTO quarantine VALUES(?,?,?,?,?,?,?)',
        (collection, code, reason, detail, url, digest, received_at),
    )


def receive_page(c, collection, raw, receipt, received_at):
    """接收一页来源，返回结构化处理结果。

    非 200 响应与解析失败：原文留证并隔离，分段游标保持不动等待恢复；
    同码内容冲突：问题原文留证并隔离，已确认事件不被覆盖。
    """
    digest = hashlib.sha256(raw).hexdigest()
    if digest != receipt['body_sha256']:
        raise ValueError('响应收据与原文不一致')
    scope_url, code, cursor = split_scope(receipt['url'])

    with c:
        _store_receipt(c, collection, raw, receipt, received_at)

        if receipt['status'] != 200:
            reason = '分页令牌失效或来源错误(HTTP %s)' % receipt['status']
            _quarantine(c, collection, code, reason, receipt['url'],
                        receipt['url'], digest, received_at)
            c.execute(
                'INSERT INTO segments(collection,code,scope_url,cursor,next_cursor,'
                'closed,blocked,blocked_reason,updated_at) VALUES(?,?,?,?,?,0,1,?,?) '
                'ON CONFLICT(collection,code) DO UPDATE SET blocked=1,'
                'blocked_reason=excluded.blocked_reason,updated_at=excluded.updated_at',
                (collection, code, scope_url, cursor, cursor, reason, received_at),
            )
            return {'code': code, 'stored': False, 'quarantined': reason,
                    'receipt_sha256': digest}

        try:
            parsed = parse_page(raw)
        except ValueError as exc:
            reason = '来源原文无法解析'
            _quarantine(c, collection, code, reason, str(exc),
                        receipt['url'], digest, received_at)
            c.execute(
                'INSERT INTO segments(collection,code,scope_url,cursor,next_cursor,'
                'closed,blocked,blocked_reason,updated_at) VALUES(?,?,?,?,?,0,1,?,?) '
                'ON CONFLICT(collection,code) DO UPDATE SET blocked=1,'
                'blocked_reason=excluded.blocked_reason,updated_at=excluded.updated_at',
                (collection, code, scope_url, cursor, cursor, reason, received_at),
            )
            return {'code': code, 'stored': False, 'quarantined': reason,
                    'receipt_sha256': digest}

        if parsed['code'] != code:
            reason = '响应追溯码与冻结范围不一致'
            detail = '范围=%s 响应=%s' % (code, parsed['code'])
            _quarantine(c, collection, code, reason, detail,
                        receipt['url'], digest, received_at)
            return {'code': code, 'stored': False, 'quarantined': reason,
                    'receipt_sha256': digest}

        seq = _next_seq(c, collection)
        content_hash = page_content_hash(parsed)
        prior_page = c.execute(
            'SELECT content_sha256 FROM page_receipts WHERE collection=? AND code=? AND page_id=?',
            (collection, code, parsed['page_id']),
        ).fetchone()

        c.execute(
            'INSERT INTO segments(collection,code,scope_url,cursor,next_cursor,'
            'closed,blocked,blocked_reason,updated_at) VALUES(?,?,?,?,?,0,0,NULL,?) '
            'ON CONFLICT(collection,code) DO UPDATE SET blocked=0,'
            'blocked_reason=NULL,updated_at=excluded.updated_at',
            (collection, code, scope_url, cursor, cursor, received_at),
        )

        if prior_page and prior_page[0] != content_hash:
            reason = '同一分页内容冲突'
            detail = 'code=%s page=%s 既有指纹=%s 新指纹=%s' % (
                code, parsed['page_id'], prior_page[0], content_hash)
            _quarantine(c, collection, code, reason, detail,
                        receipt['url'], digest, received_at)
            return {'code': code, 'stored': False, 'quarantined': reason,
                    'receipt_sha256': digest}

        if prior_page:
            # 幂等重放：同一原文不重复计数，边界维持已确认状态。
            return {'code': code, 'stored': False, 'duplicate': True,
                    'receipt_sha256': digest}

        c.execute(
            'INSERT INTO page_receipts VALUES(?,?,?,?,?,?,?)',
            (collection, code, parsed['page_id'], content_hash, digest,
             parsed['next_token'], seq),
        )

        conflicts = []
        for event in parsed['events']:
            h = event_content_hash(event)
            prior = c.execute(
                'SELECT content_sha256 FROM trace_events WHERE collection=? AND code=? AND event_id=?',
                (collection, code, event['event_id']),
            ).fetchone()
            if prior:
                if prior[0] != h:
                    conflicts.append(event['event_id'])
                continue
            c.execute(
                'INSERT INTO trace_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (collection, code, event['event_id'], event['kind'],
                 classify(event['kind']), event['batch'], event['parent'],
                 event['node'], event['event_time'], event['received_at'],
                 h, digest, parsed['page_id'], seq),
            )

        if conflicts:
            reason = '同码事件内容冲突'
            detail = 'code=%s events=%s' % (code, ','.join(sorted(conflicts)))
            _quarantine(c, collection, code, reason, detail,
                        receipt['url'], digest, received_at)
            result = {'code': code, 'stored': True, 'quarantined': reason,
                      'conflicting_events': sorted(conflicts),
                      'receipt_sha256': digest}
        else:
            result = {'code': code, 'stored': True, 'events': len(parsed['events']),
                      'closed': parsed['closed'], 'receipt_sha256': digest}

        # 只从确认页推进边界；next 令牌按平台原文冻结，不自行解释。
        c.execute(
            'UPDATE segments SET cursor=?, next_cursor=?, closed=?, updated_at=? '
            'WHERE collection=? AND code=?',
            (cursor,
             None if parsed['closed'] else _advance_cursor(scope_url, parsed['next_token']),
             1 if parsed['closed'] else 0, received_at, collection, code),
        )
        return result


def _advance_cursor(scope_url, next_token):
    if next_token is None:
        return None
    return join_cursor(scope_url, next_token)


def segment(c, collection, code):
    row = c.execute(
        'SELECT code,scope_url,cursor,next_cursor,closed,blocked,blocked_reason '
        'FROM segments WHERE collection=? AND code=?', (collection, code),
    ).fetchone()
    if row is None:
        return None
    return {'code': row[0], 'scope_url': row[1], 'cursor': row[2],
            'next_cursor': row[3], 'closed': bool(row[4]),
            'blocked': bool(row[5]), 'blocked_reason': row[6]}


def segments(c, collection):
    return [segment(c, collection, row[0])
            for row in c.execute(
                'SELECT code FROM segments WHERE collection=? ORDER BY code',
                (collection,))]


def chain_events(c, collection, code):
    return [dict(event_id=row[0], kind=row[1], kind_class=row[2], batch=row[3],
                 parent_code=row[4], node=row[5], event_time=row[6],
                 received_at=row[7], content_sha256=row[8],
                 receipt_sha256=row[9], page_id=row[10], receive_seq=row[11])
            for row in c.execute(
                'SELECT event_id,kind,kind_class,batch,parent_code,node,event_time,'
                'received_at,content_sha256,receipt_sha256,page_id,receive_seq '
                'FROM trace_events WHERE collection=? AND code=? '
                'ORDER BY event_time,event_id', (collection, code))]


def chain_pages(c, collection, code):
    return [dict(page_id=row[0], content_sha256=row[1], receipt_sha256=row[2],
                 next_token=row[3], receive_seq=row[4])
            for row in c.execute(
                'SELECT page_id,content_sha256,receipt_sha256,next_token,receive_seq '
                'FROM page_receipts WHERE collection=? AND code=? '
                'ORDER BY receive_seq', (collection, code))]


def chain_quarantine(c, collection, code):
    return [dict(reason=row[0], detail=row[1], source_url=row[2],
                 receipt_sha256=row[3], received_at=row[4])
            for row in c.execute(
                'SELECT reason,detail,source_url,receipt_sha256,received_at '
                'FROM quarantine WHERE collection=? AND code=? '
                'ORDER BY received_at', (collection, code))]


def receipt_raw(c, collection, digest):
    row = c.execute(
        'SELECT raw,source_url,status,received_at FROM receipts '
        'WHERE collection=? AND body_sha256=?', (collection, digest),
    ).fetchone()
    if row is None:
        return None
    return {'raw': row[0], 'source_url': row[1], 'status': row[2],
            'received_at': row[3]}


def evaluate(c, collection, code):
    return assess_chain(
        code,
        chain_events(c, collection, code),
        chain_pages(c, collection, code),
        chain_quarantine(c, collection, code),
        segment(c, collection, code),
    )
