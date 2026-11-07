"""Exercise WeMaa against the real core transport types, without a GUI send."""
from types import SimpleNamespace
from datetime import datetime
from unittest.mock import AsyncMock

import asyncio
import json
import sys
import time

import pytest

from src.common.data_models.message_component_data_model import MessageSequence, TextComponent
from src.platform_io import DeliveryStatus
from wemaa.config import settings
from wemaa.driver import MaaDriver
from wemaa.identity import IdentityRegistry


@pytest.fixture
def rig(tmp_path, monkeypatch):
    registry = IdentityRegistry(tmp_path/'identities.sqlite3')
    contacts = [{'id':'wx:'+name, 'wechat_id':name, 'display_name':'Same name',
                 'scope':'personal', 'identity_status':'ocr_wechat_id', 'profile_asset':'a'*64}
                for name in ('friend_one', 'friend_two')]
    registry.sync({'contacts':contacts, 'conversations':[]})
    runtime = SimpleNamespace(registry=registry, sync_contacts=AsyncMock())
    session = SimpleNamespace(user_id=registry.user_id('friend_two'), group_id=None,
                              platform='wechat', account_id=settings.WEMAA_ACCOUNT_ID)
    incoming = SimpleNamespace(timestamp=datetime.fromtimestamp(100),
        message_info=SimpleNamespace(additional_config={'wemaa_event_id':'a'*64}))
    manager = SimpleNamespace(get_existing_session_by_session_id=lambda _:session,
                              last_messages={'test-session': incoming})
    monkeypatch.setitem(sys.modules, 'src.chat.message_receive.chat_manager',
                        SimpleNamespace(chat_manager=manager))
    monkeypatch.setattr(settings, 'WECHAT_MAA_REPLY_ENABLED', True)
    driver = MaaDriver(runtime)
    calls = []
    def enqueue(body, _user):
        calls.append(body)
        return {'id':'f'*32, 'expires_at':time.time()+30}, True
    monkeypatch.setattr('wemaa.driver.maa.enqueue', enqueue)
    monkeypatch.setattr(driver, 'wait_command', AsyncMock(return_value={'status':'sent'}))
    message = SimpleNamespace(message_id='test-output', session_id='test-session',
                              raw_message=MessageSequence([TextComponent(text='acceptance')]))
    return driver, session, message, contacts, calls


def test_unopened_friend_uses_exact_contact_identity(rig):
    driver, session, message, _, calls = rig
    result=asyncio.run(driver.send_message(message, driver.descriptor.route_key))
    assert result.status == DeliveryStatus.SENT
    assert calls[0]['action'] == 'send_contact_message'
    assert calls[0]['params'] == {'contact_id':'wx:friend_two', 'wechat_id':'friend_two', 'text':'acceptance'}
    assert driver.runtime.registry.summary()['routable'] == 0
    asyncio.run(driver.send_message(message, driver.descriptor.route_key))
    assert calls[0]['request_id'] == calls[1]['request_id']


@pytest.mark.parametrize('field,value', [('platform','other'),('account_id','other-account')])
def test_wrong_account_cannot_enqueue(rig, field, value):
    driver, session, message, _, calls = rig
    setattr(session, field, value)
    result=asyncio.run(driver.send_message(message, driver.descriptor.route_key))
    assert result.status == DeliveryStatus.FAILED
    assert result.error == 'CORE_SESSION_ACCOUNT_MISMATCH'
    assert calls == []


def test_group_keeps_explicit_conversation_route(rig):
    driver, session, message, _, calls = rig
    session.group_id='maa_'+'b'*64
    assert asyncio.run(driver.send_message(message, driver.descriptor.route_key)).status == DeliveryStatus.SENT
    assert calls[0]['params']['conversation_id'] == 'b'*64
    assert calls[0]['action'] == 'send_message'


def test_unknown_delivery_never_retries(rig, monkeypatch):
    driver, _, message, _, calls = rig
    monkeypatch.setattr(driver, 'wait_command', AsyncMock(side_effect=RuntimeError('COMMAND_OUTCOME_UNKNOWN_NO_RETRY')))
    result=asyncio.run(driver.send_message(message, driver.descriptor.route_key))
    assert result.status == DeliveryStatus.FAILED and len(calls)==1
    assert result.metadata['retry'] is False
    # A planner retry creates a new output message ID. It must not create a
    # second GUI command for the same source after an unknown physical result.
    message.message_id = 'planner-regenerated-output'
    result = asyncio.run(driver.send_message(message, driver.descriptor.route_key))
    assert result.status == DeliveryStatus.FAILED and len(calls) == 1
    assert result.error == 'PREVIOUS_DELIVERY_UNCERTAIN_WAIT_FOR_NEW_INBOUND'
    from src.chat.message_receive.chat_manager import chat_manager
    chat_manager.last_messages['test-session'].timestamp = datetime.fromtimestamp(101)
    chat_manager.last_messages['test-session'].message_info.additional_config['wemaa_event_id'] = 'b'*64
    monkeypatch.setattr(driver, 'wait_command', AsyncMock(return_value={'status':'sent'}))
    assert asyncio.run(driver.send_message(message, driver.descriptor.route_key)).status == DeliveryStatus.SENT
    assert len(calls) == 2


def test_disabled_driver_cannot_enqueue(rig, monkeypatch):
    driver, _, message, _, calls = rig
    monkeypatch.setattr(settings, 'WECHAT_MAA_REPLY_ENABLED', False)
    assert asyncio.run(driver.send_message(message, driver.descriptor.route_key)).status == DeliveryStatus.FAILED
    assert not calls


@pytest.mark.parametrize('wx,expected', [('friend_two','c'*64),('friend_one','PRIVATE_OPEN_IDENTITY_MISMATCH')])
def test_first_media_requires_independent_profile_route(rig, monkeypatch, wx, expected):
    driver, session, _, contacts, calls = rig
    monkeypatch.setattr(driver,'wait_command',AsyncMock(return_value={'status':'opened','conversation_id':'c'*64}))
    async def sync():
        driver.runtime.registry.sync({'contacts':contacts, 'conversations':[{
            'id':'c'*64, 'private_contact':{'contact_id':'wx:'+wx, 'wechat_id':wx, 'profile_verified':True}}]})
    driver.runtime.sync_contacts=sync
    if expected.startswith('PRIVATE_'):
        with pytest.raises(ValueError,match=expected):
            asyncio.run(driver.media_target(session,'test-image'))
    else:
        assert asyncio.run(driver.media_target(session,'test-image')) == expected
    assert len(calls)==1 and calls[0]['action']=='open_contact'
    assert calls[0]['params']=={'contact_id':'wx:friend_two'}


@pytest.mark.parametrize('status,detail', [('succeeded',{'status':'opened'}),('failed',{'status':'uncertain'}),('expired',{})])
def test_only_actual_sent_receipt_is_accepted(monkeypatch, status, detail):
    cache=SimpleNamespace(get=lambda _:json.dumps({'status':status,'detail':detail}))
    monkeypatch.setattr('wemaa.driver.maa.client',lambda:cache)
    with pytest.raises(RuntimeError,match='COMMAND_NOT_VERIFIED_NO_RETRY'):
        asyncio.run(MaaDriver.wait_command({'id':'f'*32,'expires_at':time.time()+10}))
