"""按本项目约定展示已接收资料、证据链与明确缺口。

每条证据都带 receipt_sha256，可回指 receipts 表中的字节原文；
缺口严格区分 platform_pending（平台尚未返回）与 review_pending（收到但待核）。
"""
from .ledger import (
    chain_events,
    chain_pages,
    chain_quarantine,
    evaluate,
    segment,
    segments,
)


def overview(c, collection):
    states = segments(c, collection)
    code_reports = [code_chain(c, collection, state['code']) for state in states]
    publishable = bool(code_reports) and all(r['publishable'] for r in code_reports)
    reasons = []
    for report in code_reports:
        reasons.extend('[%s] %s' % (report['code'], reason)
                       for reason in report['reasons'])
    return {
        'collection': collection,
        'publishable': publishable,
        'reasons': reasons,
        'event_count': sum(r['event_count'] for r in code_reports),
        'segments': [_segment_view(state) for state in states],
        'chains': code_reports,
        'records': _records(code_reports),
    }


def _segment_view(state):
    return {
        'code': state['code'],
        'scope_url': state['scope_url'],
        'cursor': state['cursor'],
        'next_cursor': state['next_cursor'],
        'closed': state['closed'],
        'blocked': state['blocked'],
        'blocked_reason': state['blocked_reason'],
    }


def _records(code_reports):
    records = []
    for report in code_reports:
        for page in report['pages']:
            records.append({'type': 'page', 'id': '%s/%s' % (report['code'], page['page_id']),
                            'version': page['receive_seq'], 'source_sha256': page['receipt_sha256']})
        for event in report['evidence_chain']:
            records.append({'type': 'event', 'id': '%s/%s' % (report['code'], event['event_id']),
                            'version': event['event_time'],
                            'source_sha256': event['receipt_sha256']})
    return records


def code_chain(c, collection, code):
    """从确认采集边界重放单个码的完整证据链。"""
    state = segment(c, collection, code)
    events = chain_events(c, collection, code)
    pages = chain_pages(c, collection, code)
    quarantines = chain_quarantine(c, collection, code)
    assessment = evaluate(c, collection, code)

    evidence_chain = [_event_view(event) for event in events]
    return {
        'code': code,
        'collection_boundary': _segment_view(state) if state else None,
        'established_batch': assessment['established_batch'],
        'publishable': assessment['publishable'],
        'reasons': assessment['reasons'],
        'gaps': assessment['gaps'],
        'event_count': assessment['event_count'],
        'page_count': assessment['page_count'],
        'quarantine_count': assessment['quarantine_count'],
        'pages': pages,
        'evidence_chain': evidence_chain,
        'quarantine': quarantines,
    }


def _event_view(event):
    return {
        'event_id': event['event_id'],
        'kind': event['kind'],
        'kind_class': event['kind_class'],
        'batch': event['batch'],
        'parent_code': event['parent_code'],
        'node': event['node'],
        'event_time': event['event_time'],
        'received_at': event['received_at'],
        'page_id': event['page_id'],
        'receive_seq': event['receive_seq'],
        'content_sha256': event['content_sha256'],
        'receipt_sha256': event['receipt_sha256'],
    }
