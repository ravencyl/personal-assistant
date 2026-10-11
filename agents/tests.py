import time
from unittest.mock import patch

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


# ==================== SSE 通知器（长轮询收口，Phase 1.2） ====================
#
# wait_turn_finished 只当「通知器」：回答本轮结束了没有 / 进行到哪一步，
# 永远不解析正文（正文归 poll_turn 的 REST desc 窗口）。它的失败形态必须是
# 「没收到信号」（空串，调用方退回普通轮询），而不是抛异常弄挂一个正常轮次。
# 下面用假流把这四种形态各钉一次：终止信号 / 进度信号 / 预算到点 / 各种坏流。

class _FakeSSEStream:
    def __init__(self, lines, status_code=200, iter_exc=None):
        self._lines = lines
        self.status_code = status_code
        self._iter_exc = iter_exc

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def iter_lines(self):
        if self._iter_exc:
            raise self._iter_exc
        return iter(self._lines)


class _FakeSSEClient:
    """模拟 wait_turn_finished 里的 `httpx.Client(...) as client, client.stream(...)`

    注意它测的是**短命流**：通知器必须为每次等待新建连接并在退出时关掉
    （closed 断言），绝不能复用 _get_client() 那个常驻连接 —— 平台 idle 时
    不自动关流（F4 实测），复用常驻客户端会把一条挂 25s 的流吊在全站共用的
    连接池上。
    """

    def __init__(self, lines, status_code=200, stream_exc=None, iter_exc=None):
        self.lines = lines
        self.status_code = status_code
        self.stream_exc = stream_exc
        self.iter_exc = iter_exc
        self.stream_args = None
        self.init_kwargs = None
        self.closed = False

    def __call__(self, *args, **kwargs):
        # 顶替 httpx.Client(...) 构造函数：返回自身供 with 使用
        self.init_kwargs = kwargs
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.closed = True
        return False

    def stream(self, method, path):
        self.stream_args = (method, path)
        if self.stream_exc:
            raise self.stream_exc
        return _FakeSSEStream(self.lines, self.status_code, self.iter_exc)


class TurnFinishedNotifierTest(TestCase):
    """agents.services.wait_turn_finished：SSE 帧 → 结束信号 / 进度阶段 / 空串"""

    def _svc(self):
        return QoderAgentService(access_token='t', base_url='https://plat.test/api')

    def _run(self, lines, budget=5.0, **fake_kw):
        fake = _FakeSSEClient(lines, **fake_kw)
        with patch('agents.services.httpx.Client', fake):
            result = self._svc().wait_turn_finished('sess_w', budget=budget)
        return result, fake

    def test_terminal_frame_returns_finished_and_closes_the_stream(self):
        """session.status_idle 成组帧（id/event/data 三行，2026-10-11 实测形态）"""
        result, fake = self._run([
            'id: evt_0101',
            'event: session.status_idle',
            'data: {"type": "session.status_idle", "id": "evt_0101"}',
        ])
        self.assertEqual(result, 'finished')
        self.assertEqual(fake.stream_args, ('GET', '/sessions/sess_w/events/stream'))
        self.assertTrue(fake.closed, '短命流必须用完就关，不能吊着平台连接')

    def test_agent_message_alone_does_not_end_the_turn(self):
        """多步轮次里 agent.message 会提前出现：只认 status_idle，否则没写完的轮次被提前收尾"""
        result, _ = self._run([
            'event: span.model_request_start',
            'event: agent.tool_use',
            'event: agent.message',
            'data: {"type": "agent.message", "content": []}',
            'event: session.status_idle',
        ])
        self.assertEqual(result, 'finished')
        self.assertFalse(QoderAgentService.is_turn_terminal_event('agent.message'))

    def test_progress_only_stream_carries_last_phase_to_the_budget(self):
        """流上只有进度：到预算带最近阶段回去（前端据此换文案并挂下一拍）"""
        def slow_lines():
            yield 'event: span.model_request_start'
            time.sleep(0.3)
            yield 'event: agent.tool_use'

        result, _ = self._run(slow_lines(), budget=0.1)
        self.assertEqual(result, 'thinking',
                         '第二拍已越过预算但第一拍的阶段不能丢 —— 进度文案靠它续着')

    def test_quiet_stream_returns_empty_not_an_error(self):
        """流安静地结束/没解析出事件：空串 =「没信号」，调用方退回普通轮询"""
        result, _ = self._run([': ping', '', 'id: evt_1'])
        self.assertEqual(result, '')

    def test_handshake_failure_returns_empty(self):
        """流建不起来（非 200）：不抛，只当没收到信号"""
        result, _ = self._run(['event: session.status_idle'], status_code=403)
        self.assertEqual(result, '')

    def test_broken_stream_anywhere_returns_empty(self):
        """连接抛异常与读到一半断流，两种坏法都必须被吞掉"""
        result, _ = self._run([], stream_exc=ConnectionError('reset by peer'))
        self.assertEqual(result, '')
        result, _ = self._run(['event: x'], iter_exc=ConnectionError('halfway break'))
        self.assertEqual(result, '')

    def test_parse_sse_chunk_field_precedence_and_garbage(self):
        """解析器只认 event 字段，缺省从 data JSON 的 type 兜底；认不出就空串"""
        p = QoderAgentService.parse_sse_chunk
        self.assertEqual(p('event: session.status_idle'), 'session.status_idle')
        self.assertEqual(p('id: evt_1\nevent: agent.message\ndata: {"type": "x"}'),
                         'agent.message', 'event 字段优先于 data，不能被兜底通道顶掉')
        self.assertEqual(p('data: {"type": "session.status_running"}'),
                         'session.status_running')
        self.assertEqual(p(': ping'), '')
        self.assertEqual(p('data: not-json{'), '')
        self.assertEqual(p('id: evt_1'), '')
        self.assertEqual(p(''), '')

    def test_phase_from_event_maps_only_progress_semantics(self):
        """进度阶段映射：模型请求 → thinking，任何 tool 事件 → tooling，其余不编阶段"""
        ph = QoderAgentService.phase_from_event
        self.assertEqual(ph('span.model_request_start'), 'thinking')
        self.assertEqual(ph('agent.tool_use'), 'tooling')
        self.assertEqual(ph('agent.tool_result'), 'tooling')
        self.assertEqual(ph('agent.message'), '')
        self.assertEqual(ph('session.status_running'), '')
