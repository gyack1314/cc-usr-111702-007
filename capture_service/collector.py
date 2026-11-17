"""可恢复的分段收集器。

收集边界（scope + 游标）持久化在账本 segments 表里：
- drain() 顺序消费冻结交互，已确认页面靠幂等重放跳过；
- resume() 只从每个码最后确认的游标继续，令牌失效不会越过边界；
- 任何一页被隔离，该码立即停在当前边界，绝不推断后续去向。
"""
from datetime import datetime, timezone

from .domain import join_cursor, split_scope
from .ledger import receive_page, segment, segments


def utc_now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class Collector:
    def __init__(self, transport, db, collection, clock=utc_now_iso):
        self.transport = transport
        self.db = db
        self.collection = collection
        self.clock = clock
        self.results = []

    def drain(self, max_pages=None, continue_on_quarantine=False):
        """消费传输器上的冻结步骤。

        首次收集与恢复共用同一路径：重放命中已确认分页时，账本幂等忽略，
        游标仍从已确认边界读取。max_pages 用于分段落地，防止一次跑过边界。
        """
        processed = 0
        while processed < (max_pages if max_pages is not None else 10 ** 9):
            if self.transport.position >= len(self.transport.steps):
                break
            step = self.transport.steps[self.transport.position]
            request = step['request']
            raw, receipt = self.transport.request(
                request['method'], request['url'],
                credential_scope=self.transport.origin_of(request['url']),
            )
            result = receive_page(self.db, self.collection, raw, receipt, self.clock())
            result['step'] = receipt['step']
            self.results.append(result)
            processed += 1
            if result.get('quarantined') and not continue_on_quarantine:
                break
        return self.results

    def resume(self, code):
        """从码的确认采集边界继续，返回下一步应请求的 URL 或 None。"""
        state = segment(self.db, self.collection, code)
        if state is None:
            raise KeyError('收集范围内没有该码的分段: %s' % code)
        if state['closed']:
            return None
        if state['blocked']:
            # 失效令牌下不重复撞同一页；恢复必须由新的冻结交互提供新令牌。
            return state['next_cursor']
        return state['next_cursor']

    def pending_codes(self):
        """所有尚未闭合的码，区分平台未返回与待核两种状态。"""
        pending = []
        for state in segments(self.db, self.collection):
            if state['closed']:
                continue
            pending.append({
                'code': state['code'],
                'status': 'review_pending' if state['blocked'] else 'platform_pending',
                'cursor': state['cursor'],
                'next_cursor': state['next_cursor'],
                'blocked_reason': state['blocked_reason'],
            })
        return pending

    def resume_drain(self):
        """从每个码的确认边界继续消费冻结交互。

        已确认页面通过 find_step 定位并跳过（位置前移但不重复请求），
        只有边界之后的步骤会真正发起重放；失效令牌码停在待核边界。
        """
        for state in segments(self.db, self.collection):
            if state['closed']:
                continue
            target = state['next_cursor']
            if not target:
                continue
            try:
                index = self.transport.find_step('GET', target)
            except EOFError:
                # 当前冻结交互没有该码的续页：边界原样保留，交回待办。
                continue
            if self.transport.position <= index:
                self.transport.position = index
                self.drain(continue_on_quarantine=True)
        return self.results

    def request_next(self, code):
        """按恢复边界发起下一次冻结请求；已闭合返回 None。"""
        url = self.resume(code)
        if url is None:
            return None
        raw, receipt = self.transport.request(
            'GET', url, credential_scope=self.transport.origin_of(url),
        )
        result = receive_page(self.db, self.collection, raw, receipt, self.clock())
        self.results.append(result)
        return result


def plan_first_request(url):
    """从任意带 page 的 URL 还原首页请求与冻结范围。"""
    scope_url, code, _cursor = split_scope(url)
    return scope_url, code, join_cursor(scope_url, 'page=1')
