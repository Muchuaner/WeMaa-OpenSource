"""实际工具批次必须在串行回复完成后终止，不能执行第二次 reply。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.core.tooling import ToolExecutionResult
from src.llm_models.payload_content.tool_option import ToolCall
from src.maisaka.reasoning_engine import MaisakaReasoningEngine


@pytest.mark.asyncio
@pytest.mark.parametrize('success', [True, False])
async def test_serial_reply_settles_receipt_and_stops_remaining_tools(monkeypatch, success):
    registry = SimpleNamespace(
        list_tools=AsyncMock(return_value=[]),
        invoke=AsyncMock(return_value=ToolExecutionResult(tool_name='reply', success=success,
                        content='已发送' if success else '', error_message='' if success else '结果不确定')))
    runtime = SimpleNamespace(
        _tool_registry=registry, session_id='serial-session', log_prefix='[test]',
        chat_stream=SimpleNamespace(platform='wechat', is_group_session=False, group_id='', user_id='user'),
        is_action_tool_currently_available=lambda _: True,
        _update_stage_status=Mock(), _reset_consecutive_wait_count=Mock(),
        _end_planner_continuation=Mock(), _enter_stop_state=Mock())
    engine = MaisakaReasoningEngine(runtime)
    engine._record_tool_execution_effects = AsyncMock()
    engine._append_tool_execution_result = Mock()
    engine._append_tool_post_history_messages = Mock()
    trigger = SimpleNamespace(platform='wechat', message_info=SimpleNamespace(additional_config={
        'wemaa_serial_reply': True, 'wemaa_event_id': 'a' * 64}))
    engine._wemaa_serial_trigger = trigger
    settle = AsyncMock()
    monkeypatch.setattr('wemaa.serial_reply.settle', settle)
    paused, reason, _, _ = await engine._handle_tool_calls([
        ToolCall(call_id='first', func_name='reply'),
        ToolCall(call_id='duplicate', func_name='reply')], '思考')
    assert paused and reason == 'wechat_serial_reply_complete'
    assert registry.invoke.await_count == 1
    assert settle.await_args.args[:2] == (trigger, 'sent' if success else 'uncertain')
    runtime._enter_stop_state.assert_called_once()
