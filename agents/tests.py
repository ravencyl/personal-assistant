from django.test import TestCase

from agents.services import QoderAgentService


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _DescFakeClient:
    """模拟平台事件接口（order=desc）：最新事件在前，翻页向更旧走

    真平台口径（2026-09-22 实测）：GET /sessions/{id}/events 每页最多
    100 条、支持 order=desc（最新在前）；翻页参数是 `page=<next_page 游标>`，
    游标指向当前页最后一条（即当前页里最旧的），下一页从它更旧继续。
    不传 order 的第一页永远从会话创建事件开始（最旧优先）——那条路是
    2026-09-22 线上故障的根因，现在代码永远走 desc，假客户端只模拟 desc，
    并在收到非 desc 请求时直接断言失败。
    """

    def __init__(self, events, page_size=100):
        self.events = events  # 按创建顺序排列（旧 → 新）
        self.page_size = page_size
        self.calls = []

    def get(self, path, params=None):
        params = dict(params or {})
        if path.endswith('/events'):
            self.calls.append(params)
            assert params.get('order') == 'desc', \
                '事件拉取必须走 order=desc，否则老会话会重演回线上故障'
            limit = min(int(params.get('limit', 100)), self.page_size)
            newest = list(reversed(self.events))
            start = 0
            cursor = params.get('page')
            if cursor:
                for i, e in enumerate(newest):
                    if e['id'] == cursor:
                        start = i + 1
                        break
            data = newest[start:start + limit]
            end = start + len(data)
            has_more = end < len(newest)
            return _FakeResponse({
                'data': data,
                'has_more': has_more,
                'next_page': data[-1]['id'] if data and has_more else None,
            })
        # get_session
        return _FakeResponse({'status': 'idle'})


def _event(i, etype, text=''):
    e = {'id': f'evt_{i:04d}', 'type': etype}
    if text:
        e['content'] = [{'type': 'text', 'text': text}]
    return e


class EventPaginationTest(TestCase):
    """事件拉取必须永远拿到「本轮」，不能被历史淹没（2026-09-22 线上故障）

    故障：旧实现不传 order，平台从最旧开始给 100 条。老会话事件总数超过
    100 后，本轮的 user.message 和回复全落在窗外，poll_turn 从此永远返回
    empty——线上表现为对话 42 连续多轮「（AI 这轮没有返回内容，可能已
    超时）」，而模型其实每轮都在平台上回了。

    修复：永远 order=desc 从最新取窗口，找到本轮 user.message 即收口。
    本轮回复一定比本轮用户消息新，所以正常一页（100 条，平台上限）必含
    完整本轮，**与会话总长度无关**——不靠猜页数（页数上限终归会被够长的
    对话顶穿）。只有「一轮产生的事件比一页还多」这种病态才向更旧翻页，
    防死循环靠「游标必须前进」守卫 + max_walks 保险丝。
    """

    def setUp(self):
        self.svc = QoderAgentService()

    def _install(self, events, page_size=100):
        client = _DescFakeClient(events, page_size=page_size)
        self.svc._client = client
        return client

    def test_very_long_session_gets_latest_reply_in_one_page(self):
        """事故场景回放：500 条历史之后才是本轮，desc 一页就该拿到"""
        events = [_event(i, 'session.usage') for i in range(500)]
        events.append(_event(500, 'user.message', 'Manner 咖啡，35元'))
        events.append(_event(501, 'agent.message', '已记：Manner 咖啡 ¥35'))
        client = self._install(events)
        result = self.svc.poll_turn('sess_x')
        self.assertEqual(result['state'], 'ready',
                         '老会话拿不到最新回复 = 回到线上那个 bug')
        self.assertEqual(result['text'], '已记：Manner 咖啡 ¥35')
        self.assertEqual(len(client.calls), 1,
                         '正常一轮一页就该收口，与会话总长度无关')
        self.assertEqual(client.calls[0]['order'], 'desc')
        self.assertNotIn('page', client.calls[0])

    def test_latest_turn_wins_and_old_replies_do_not_leak(self):
        """多轮会话只取最新一轮：更早轮次的回复不得串进来"""
        events = [
            _event(0, 'user.message', '第一问'),
            _event(1, 'agent.message', '第一答'),
            _event(2, 'user.message', '第二问'),
            _event(3, 'agent.message', '第二答'),
        ]
        self._install(events)
        result = self.svc.poll_turn('sess_x')
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['text'], '第二答')
        self.assertNotIn('第一答', result['text'])

    def test_reply_fragments_are_joined_in_reading_order(self):
        """回复拆成多条事件时按时间顺序拼接（desc 收集是新→旧，要反转回来）"""
        events = [
            _event(0, 'user.message', '问题'),
            _event(1, 'agent.message', '第一段'),
            _event(2, 'agent.message', '第二段'),
            _event(3, 'agent.message', '第三段'),
        ]
        self._install(events)
        result = self.svc.poll_turn('sess_x')
        self.assertEqual(result['text'], '第一段\n第二段\n第三段')

    def test_window_without_user_message_walks_to_older_pages(self):
        """病态：一页窗口里没有 user.message（一轮事件比一页还多），向更旧翻页收口"""
        events = [_event(0, 'user.message', '问题')]
        events += [_event(i, 'agent.message', f'片段{i}') for i in range(1, 151)]
        client = self._install(events, page_size=100)
        result = self.svc.poll_turn('sess_x')
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(
            result['text'],
            '\n'.join(f'片段{i}' for i in range(1, 151)),
            '跨页收集后必须恢复阅读顺序')
        self.assertEqual(len(client.calls), 2)
        self.assertNotIn('page', client.calls[0])
        self.assertIn('page', client.calls[1])

    def test_no_user_message_anywhere_returns_empty_not_old_reply(self):
        """翻遍也找不到本轮 user.message：返回 empty，绝不把旧轮回复顶给用户"""
        events = [_event(i, 'agent.message', f'旧答{i}') for i in range(250)]
        self._install(events)
        result = self.svc.poll_turn('sess_x')
        self.assertEqual(result['state'], 'empty')
        self.assertEqual(result['text'], '')

    def test_stuck_cursor_does_not_loop_forever(self):
        """平台游标异常（next_page 不前进）时必须熔断，不许死循环"""
        events = [_event(i, 'agent.message', f'旧答{i}') for i in range(250)]

        class _StuckClient(_DescFakeClient):
            def get(self, path, params=None):
                resp = super().get(path, params)
                payload = resp._payload
                payload['has_more'] = True
                payload['next_page'] = 'evt_0000'
                return resp

        client = _StuckClient(events)
        self.svc._client = client
        result = self.svc.poll_turn('sess_x')
        # 游标第一拍与 page(None) 不同前进了一步，第二拍原地踏步被守卫拦下
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(result['state'], 'empty',
                         '熔断后返回窗口内判定结果，不抛异常不挂死')
