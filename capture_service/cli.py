"""只读重放合成的来源交互并展示当前接收状态。

inspect  跑一遍冻结交互并给出总览（默认内存库，--db 可持久化分段边界）
resume   展示每个码的确认边界、下一步游标与缺口分类
chain    从确认边界重放单个码的证据链，并回指每条证据的原文收据
"""
import argparse
import json

from .collector import Collector
from .ledger import connect
from .transport import Replay
from .view import code_chain, overview


def _open(path):
    return connect(path if path else ':memory:')


def _run(manifest, db_path, collection, max_pages=None, drain=True):
    replay = Replay(manifest)
    db = _open(db_path)
    collector = Collector(replay, db, collection)
    if drain:
        collector.drain(max_pages=max_pages, continue_on_quarantine=True)
    return replay, db, collector


def main():
    parser = argparse.ArgumentParser(description='追溯码分段收集与证据链重放')
    sub = parser.add_subparsers(dest='command', required=True)

    def add_common(p):
        p.add_argument('manifest')
        p.add_argument('--db', help='持久化 SQLite 路径；缺省为内存库')
        p.add_argument('--collection', default='trace-batch')

    p_inspect = sub.add_parser('inspect', help='重放冻结交互并输出总览')
    add_common(p_inspect)
    p_inspect.add_argument('--max-pages', type=int, default=None)

    p_resume = sub.add_parser('resume', help='从确认边界恢复收集并展示缺口')
    add_common(p_resume)
    p_resume.add_argument('--view-only', action='store_true',
                          help='只查看持久化边界，不消费冻结交互')

    p_chain = sub.add_parser('chain', help='重放单个码的证据链')
    add_common(p_chain)
    p_chain.add_argument('code')

    args = parser.parse_args()
    if args.command == 'inspect':
        _replay, db, collector = _run(
            args.manifest, args.db, args.collection,
            max_pages=getattr(args, 'max_pages', None))
    elif args.command == 'resume':
        # 恢复：先打开持久化边界，再按边界定位冻结交互续跑。
        replay = Replay(args.manifest)
        db = _open(args.db)
        collector = Collector(replay, db, args.collection)
        if not getattr(args, 'view_only', False):
            collector.resume_drain()
    else:
        _replay, db, collector = _run(
            args.manifest, args.db, args.collection)

    if args.command == 'inspect':
        result = overview(db, args.collection)
    elif args.command == 'resume':
        result = {
            'collection': args.collection,
            'pending': collector.pending_codes(),
            'results': collector.results,
        }
    else:
        result = code_chain(db, args.collection, args.code)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    db.close()


if __name__ == '__main__':
    main()
