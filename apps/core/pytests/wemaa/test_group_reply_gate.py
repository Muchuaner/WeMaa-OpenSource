"""使用真实核心入口验证静默消息不触发规划器、命令或最近来信更新。"""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import asyncio

import pytest

from src.chat.message_receive.bot import ChatBot, chat_manager
from src.chat.message_receive.message import SessionMessage
from src.common.data_models.mai_message_data_model import GroupInfo, MessageInfo, UserInfo
from src.common.data_models.message_component_data_model import MessageSequence, TextComponent
from src.common.utils.utils_message import MessageUtils
from wemaa.group_reply import ReplyDecision


@pytest.mark.parametrize('group,decision,quiet', [
    (True, ReplyDecision(False, 'broadcast'), True),
    (True, ReplyDecision(False, 'classification_failed'), True),
    (True, ReplyDecision(True, 'addressed_request'), False),
    (False, ReplyDecision(False, 'broadcast'), False),
])
def test_core_routes_only_actionable_group_messages(monkeypatch, group, decision, quiet):
    message = SessionMessage(message_id='maa_' + 'a' * 64, timestamp=datetime.now(), platform='wechat')
    message.message_info = MessageInfo(user_info=UserInfo('u1', '测试用户'),
        group_info=GroupInfo('maa_' + 'b' * 64, '测试群') if group else None,
        additional_config={'platform_io_account_id': 'win-wechat-maa', 'wemaa_event_id': 'a' * 64,
                           'wemaa_group_reply': decision.metadata()})
    message.raw_message = MessageSequence([TextComponent(text='群里的一条消息')])
    message.processed_plain_text = '群里的一条消息'
    monkeypatch.setattr(message, 'process', AsyncMock())
    bot = ChatBot()
    monkeypatch.setattr(bot, 'handle_notice_message', AsyncMock())
    monkeypatch.setattr(bot, '_invoke_message_hook', AsyncMock(return_value=(SimpleNamespace(aborted=False), message)))
    monkeypatch.setattr(bot, '_is_command_candidate', lambda _: False)
    monkeypatch.setattr(MessageUtils, 'check_ban_words', lambda _: (False, None))
    monkeypatch.setattr(MessageUtils, 'check_ban_regex', lambda _: (False, None))
    store, register, process = AsyncMock(), Mock(), AsyncMock()
    monkeypatch.setattr(MessageUtils, 'store_message_to_db_async', store)
    monkeypatch.setattr(chat_manager, 'register_message', register)
    monkeypatch.setattr(chat_manager, 'get_or_create_session', AsyncMock())
    lifecycle, clear, command = AsyncMock(return_value=False), AsyncMock(return_value=False), AsyncMock(return_value=(False, None, True))
    monkeypatch.setattr(bot, '_process_adapter_lifecycle_command', lifecycle)
    monkeypatch.setattr(bot, '_process_clear_context_command', clear)
    monkeypatch.setattr(bot, '_process_commands', command)
    monkeypatch.setattr(bot.heartflow_message_receiver, 'process_message', process)
    asyncio.run(bot.receive_message(message))
    if quiet:
        store.assert_awaited_once_with(message)
        register.assert_not_called()
        command.assert_not_awaited()
        process.assert_not_awaited()
    else:
        register.assert_called_once_with(message)
        command.assert_awaited_once()
        process.assert_awaited_once_with(message)


def test_restored_silent_message_cannot_be_used_as_reply_target(monkeypatch):
    from src.maisaka.builtin_tool import reply
    from src.maisaka.builtin_tool.context import BuiltinToolRuntimeContext

    target = SimpleNamespace(platform='wechat', message_info=SimpleNamespace(group_info=object(),
        additional_config={'wemaa_event_id': 'a' * 64,
                           'wemaa_group_reply': ReplyDecision(False, 'broadcast').metadata()}))
    runtime = SimpleNamespace(find_source_message_by_id=lambda _: target)
    tool_ctx = SimpleNamespace(runtime=runtime, build_failure_result=BuiltinToolRuntimeContext.build_failure_result)
    invocation = SimpleNamespace(arguments={'msg_id': 'historical-message'}, reasoning='', tool_name='reply')
    generator = Mock(side_effect=AssertionError('静默消息不应进入回复生成器'))
    monkeypatch.setattr(reply.replyer_manager, 'get_replyer', generator)
    result = asyncio.run(reply.handle_tool(tool_ctx, invocation))
    assert result.success is False
    assert '不需要机器人回复' in result.error_message
    generator.assert_not_called()
