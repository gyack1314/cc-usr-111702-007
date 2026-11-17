"""核对来源、解释、分段账本、恢复收集与证据链的完整业务链。"""
import json
import tempfile
import unittest
from pathlib import Path

from capture_service.collector import Collector
from capture_service.domain import classify, split_scope
from capture_service.ledger import connect, evaluate, receipt_raw
from capture_service.transport import Replay
from capture_service.view import code_chain, overview

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
COLLECTION = 'trace-batch'


class FakeClock:
    """确定性接收时间，避免测试依赖系统时钟。"""

    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1
        return '2026-11-17T%02d:00:00Z' % (10 + self.n)


def drain(manifest, db=None, **kwargs):
    replay = Replay(DATA / manifest)
    db = db or connect(':memory:')
    collector = Collector(replay, db, COLLECTION, clock=FakeClock())
    collector.drain(continue_on_quarantine=True, **kwargs)
    return replay, db, collector


class ReceiptAndChain(unittest.TestCase):
    def test_normal_chain_is_append_only_and_not_publishable_due_to_return(self):
        _replay, db, _collector = drain('normal.json')
        view = overview(db, COLLECTION)
        chain = view['chains'][0]
        # 退货事件后去向未闭环：不可发布，但原因是业务未决而非资料缺失。
        self.assertFalse(view['publishable'])
        self.assertTrue(any('退货事件 E-2' in reason for reason in view['reasons']))
        self.assertEqual(chain['event_count'], 2)
        self.assertEqual(chain['page_count'], 2)
        self.assertTrue(chain['collection_boundary']['closed'])
        # 证据链按业务时间排序：return(E-2,09:00) 排在 pack(E-1,10:00) 前，
        # 即使它是更晚才接收到的页面——接收顺序由 receive_seq 另行留证。
        self.assertEqual([e['event_id'] for e in chain['evidence_chain']],
                         ['E-2', 'E-1'])
        self.assertEqual([e['page_id'] for e in chain['evidence_chain']],
                         ['2', '1'])
        # 每条证据都能回指字节原文。
        for event in chain['evidence_chain']:
            raw = receipt_raw(db, COLLECTION, event['receipt_sha256'])
            self.assertIsNotNone(raw)
            self.assertIn(event['event_id'].encode(), raw['raw'])
        # 退货与包装不会被当成新批次生产：pack 在生产侧建立批号，
        # return 只是流通环节，不产生新的批次绑定。
        self.assertEqual(chain['established_batch'], 'B-01')
        self.assertEqual(
            {e['event_id']: e['kind_class'] for e in chain['evidence_chain']},
            {'E-1': 'production', 'E-2': 'return'},
        )
        db.close()

    def test_every_record_has_source_receipt(self):
        _replay, db, _collector = drain('normal.json')
        view = overview(db, COLLECTION)
        self.assertTrue(all(r['source_sha256'] for r in view['records']))
        self.assertEqual(
            db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0], 2)
        db.close()

    def test_authorization_scope(self):
        replay = Replay(DATA / 'normal.json')
        request = replay.steps[0]['request']
        with self.assertRaises(ValueError):
            replay.request(request['method'], request['url'],
                           credential_scope='https://elsewhere.example.test')
        self.assertEqual(replay.position, 0)


class ConflictQuarantine(unittest.TestCase):
    def test_conflicting_page_is_quarantined_but_original_is_retained(self):
        replay = Replay(DATA / 'conflict.json')
        db = connect(':memory:')
        collector = Collector(replay, db, COLLECTION, clock=FakeClock())
        results = collector.drain(continue_on_quarantine=True)

        self.assertTrue(results[1].get('quarantined'))
        # 旧状态不被覆盖：E-1 仍是 B-01。
        row = db.execute(
            "SELECT batch FROM trace_events WHERE event_id='E-1'").fetchone()
        self.assertEqual(row[0], 'B-01')
        # 两份原文都留证，冲突原文进隔离区而不是回滚。
        self.assertEqual(
            db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0], 2)
        self.assertEqual(
            db.execute('SELECT COUNT(*) FROM quarantine').fetchone()[0], 1)
        quarantined = db.execute(
            'SELECT receipt_sha256,reason FROM quarantine').fetchone()
        self.assertEqual(quarantined[1], '同一分页内容冲突')
        raw = receipt_raw(db, COLLECTION, quarantined[0])
        self.assertEqual(json.loads(raw['raw'])['events'][0]['batch'], 'B-02')
        # 待核范围明确上报，且不推断下游：链不可发布，缺口标为 review_pending。
        report = evaluate(db, COLLECTION, 'BX-1')
        self.assertFalse(report['publishable'])
        self.assertTrue(any(g['kind'] == 'review_pending' for g in report['gaps']))
        db.close()


class ResumableCollection(unittest.TestCase):
    def test_partial_collection_freezes_cursor_and_reports_platform_pending(self):
        replay = Replay(DATA / 'normal.json')
        db = connect(':memory:')
        collector = Collector(replay, db, COLLECTION, clock=FakeClock())
        collector.drain(max_pages=1)

        pending = collector.pending_codes()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['code'], 'BX-1')
        self.assertEqual(pending[0]['status'], 'platform_pending')
        self.assertTrue(pending[0]['next_cursor'].endswith('page=2'))

        report = evaluate(db, COLLECTION, 'BX-1')
        self.assertFalse(report['publishable'])
        self.assertEqual(
            [g['kind'] for g in report['gaps']], ['platform_pending'])
        self.assertEqual(report['event_count'], 1)

        # 用持久化库模拟进程重启：分段边界保留，重放已确认页幂等跳过。
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / 'segments.db')
            db_file = connect(db_path)
            replay1 = Replay(DATA / 'normal.json')
            Collector(replay1, db_file, COLLECTION, clock=FakeClock()).drain(max_pages=1)
            self.assertEqual(
                db_file.execute('SELECT COUNT(*) FROM page_receipts').fetchone()[0], 1)
            db_file.close()

            db_file = connect(db_path)
            replay2 = Replay(DATA / 'normal.json')
            Collector(replay2, db_file, COLLECTION, clock=FakeClock()).drain(
                continue_on_quarantine=True)
            chain = code_chain(db_file, COLLECTION, 'BX-1')
            self.assertTrue(chain['collection_boundary']['closed'])
            self.assertEqual(chain['event_count'], 2)
            # 重放首页没有产生重复分页或重复事件。
            self.assertEqual(
                db_file.execute('SELECT COUNT(*) FROM page_receipts').fetchone()[0], 2)
            self.assertEqual(
                db_file.execute('SELECT COUNT(*) FROM trace_events').fetchone()[0], 2)
            db_file.close()
        db.close()

    def test_expired_cursor_blocks_boundary_and_recovery_unblocks_it(self):
        # 第一段冻结交互：第二页令牌失效。
        _replay, db, collector = drain('token-dead.json')
        state = collector.pending_codes()[0]
        self.assertEqual(state['status'], 'review_pending')
        self.assertTrue(state['next_cursor'].endswith('page=2'))
        chain = code_chain(db, COLLECTION, 'BX-3')
        self.assertEqual(chain['event_count'], 1)
        self.assertEqual(chain['quarantine_count'], 1)
        self.assertFalse(chain['collection_boundary']['closed'])
        # 失效响应原文保留。
        self.assertEqual(
            db.execute('SELECT status FROM receipts '
                       "WHERE source_url LIKE '%page=2'").fetchone()[0], 410)

        # 新冻结交互带有效续页：resume_drain 按边界 URL 键控定位，
        # 跳过已确认首页，只从失效边界续跑。
        replay2 = Replay(DATA / 'token-recover.json')
        collector2 = Collector(replay2, db, COLLECTION, clock=FakeClock())
        results = collector2.resume_drain()
        # 首页被定位跳过，实际重放的是 410 与有效第二页两个步骤。
        self.assertEqual(replay2.position, 3)
        self.assertEqual(
            [r.get('quarantined') for r in results][0],
            '分页令牌失效或来源错误(HTTP 410)')
        chain = code_chain(db, COLLECTION, 'BX-3')
        self.assertTrue(chain['collection_boundary']['closed'])
        self.assertEqual(chain['event_count'], 2)
        # 拆包属于流通环节，不会被当成新批次生产。
        kinds = {e['event_id']: e['kind_class'] for e in chain['evidence_chain']}
        self.assertEqual(kinds['BX3-E2'], 'unpack')
        self.assertEqual(chain['established_batch'], 'B-03')
        # 410 隔离记录仍在，链条因待核历史不可直接发布。
        self.assertEqual(chain['quarantine_count'], 1)
        self.assertFalse(chain['publishable'])
        db.close()

    def test_malformed_body_is_quarantined_without_guessing(self):
        _replay, db, collector = drain('malformed.json')
        self.assertEqual(collector.pending_codes()[0]['status'], 'review_pending')
        self.assertEqual(
            db.execute('SELECT COUNT(*) FROM trace_events').fetchone()[0], 0)
        self.assertEqual(
            db.execute('SELECT COUNT(*) FROM quarantine').fetchone()[0], 1)
        # 原文仍可取出核验。
        digest = db.execute('SELECT receipt_sha256 FROM quarantine').fetchone()[0]
        self.assertTrue(receipt_raw(db, COLLECTION, digest)['raw'].startswith(b'{'))
        db.close()

    def test_full_replay_is_idempotent_and_keeps_single_boundary(self):
        replay1 = Replay(DATA / 'multicode.json')
        db = connect(':memory:')
        clock = FakeClock()
        Collector(replay1, db, COLLECTION, clock=clock).drain(
            continue_on_quarantine=True)
        receipts_after_first = db.execute(
            'SELECT COUNT(*) FROM receipts').fetchone()[0]
        events_after_first = db.execute(
            'SELECT COUNT(*) FROM trace_events').fetchone()[0]

        # 同一冻结交互从头再跑一遍：已确认分页全部幂等跳过。
        replay2 = Replay(DATA / 'multicode.json')
        results = Collector(replay2, db, COLLECTION, clock=clock).drain(
            continue_on_quarantine=True)
        self.assertTrue(all(r.get('duplicate') for r in results))
        self.assertEqual(
            db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0],
            receipts_after_first)
        self.assertEqual(
            db.execute('SELECT COUNT(*) FROM trace_events').fetchone()[0],
            events_after_first)
        self.assertEqual(
            db.execute('SELECT COUNT(*) FROM segments').fetchone()[0], 2)
        self.assertEqual(
            db.execute('SELECT COUNT(*) FROM page_receipts').fetchone()[0], 4)
        db.close()


class MultiCodeAndSemantics(unittest.TestCase):
    def test_event_level_conflict_keeps_first_evidence_and_quarantines_rewrite(self):
        _replay, db, collector = drain('event-conflict.json')
        self.assertTrue(collector.results[-1].get('quarantined'))
        # 旧证据的企业节点不被后到页面覆盖。
        row = db.execute(
            "SELECT node FROM trace_events WHERE event_id='BX5-E1'").fetchone()
        self.assertEqual(row[0], 'N-PROD')
        self.assertEqual(
            db.execute('SELECT COUNT(*) FROM trace_events').fetchone()[0], 1)
        quarantined = db.execute(
            'SELECT reason,detail FROM quarantine').fetchone()
        self.assertEqual(quarantined[0], '同码事件内容冲突')
        self.assertIn('BX5-E1', quarantined[1])
        report = evaluate(db, COLLECTION, 'BX-5')
        self.assertFalse(report['publishable'])
        self.assertTrue(any(g['kind'] == 'review_pending' for g in report['gaps']))
        db.close()

    def test_two_codes_have_independent_segments_and_provenance(self):
        _replay, db, collector = drain('multicode.json')
        self.assertEqual(
            sorted(row['code'] for row in collector.pending_codes()), [])
        bx1 = code_chain(db, COLLECTION, 'BX-1')
        bx2 = code_chain(db, COLLECTION, 'BX-2')
        self.assertTrue(bx1['collection_boundary']['closed'])
        self.assertTrue(bx2['collection_boundary']['closed'])
        # 包装码、批号、父子包装、企业节点、两种时间分别留证。
        e1 = next(e for e in bx2['evidence_chain'] if e['event_id'] == 'BX2-E1')
        self.assertEqual(e1['batch'], 'B-02')
        self.assertEqual(e1['parent_code'], 'BOX-2')
        self.assertEqual(e1['node'], 'N-PROD')
        self.assertNotEqual(e1['event_time'], e1['received_at'])
        # 退货补录晚于出库接收，但事件按业务时间归位，出库去向未被覆盖。
        ids = [e['event_id'] for e in bx2['evidence_chain']]
        self.assertEqual(ids, ['BX2-E1', 'BX2-E2', 'BX2-E3',
                               'BX2-E4', 'BX2-E5', 'BX2-E6'])
        receive_order = sorted(bx2['evidence_chain'], key=lambda e: e['receive_seq'])
        self.assertEqual(receive_order[-1]['event_id'], 'BX2-E6')
        db.close()

    def test_return_and_unpack_never_establish_production_batch(self):
        self.assertEqual(classify('return'), 'return')
        self.assertEqual(classify('unpack'), 'unpack')
        self.assertEqual(classify('outbound'), 'circulation')
        self.assertEqual(classify('repack'), 'circulation')
        self.assertEqual(classify('produce'), 'production')
        self.assertEqual(classify('pack'), 'production')

    def test_split_scope_freezes_query_and_cursor(self):
        scope, code, cursor = split_scope(
            'https://trace.example.test/events?code=BX-9&page=7')
        self.assertEqual(scope, 'https://trace.example.test/events?code=BX-9')
        self.assertEqual(code, 'BX-9')
        self.assertEqual(cursor, '7')
        with self.assertRaises(ValueError):
            split_scope('https://trace.example.test/events?page=1')


if __name__ == '__main__':
    unittest.main()
