"""串行事件回执、重试和暂存时间边界。"""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.helpers import MemoryRedis
from wemaa.services import replies
from wemaa.serial_reply import can_reply, settle


class Cache(MemoryRedis):
    def delete(self, key):
        return self.values.pop(key, None) is not None


@pytest.fixture
def cache(monkeypatch):
    value = Cache()
    monkeypatch.setattr(replies, 'client', lambda: value)
    monkeypatch.setattr(replies.settings, 'WECHAT_MAA_REPLY_ENABLED', True)
    from wemaa.runtime import runtime
    monkeypatch.setattr(runtime,'admit_identity',AsyncMock(return_value={}))
    return value


def body(event='a', **updates):
    return dict(gateway_id=replies.GATEWAY_ID, event_id=event * 64,
                conversation_id='b' * 64, conversation_title='Demo User', conversation_type='private',
                sender_name='Demo User', sender_verified=True, direction='incoming', stable_ocr=True,
                observed_at=time.time(), text='你好', context=[], serial_reply=True, **updates)


def message(event='a'):
    return SimpleNamespace(platform='wechat', message_info=SimpleNamespace(additional_config={
        'wemaa_event_id': event * 64, 'wemaa_serial_reply': True}))


def test_core_acceptance_is_not_send_completion(cache):
    row, created = replies._begin(replies.validate_prepare(body()))
    assert created
    replies._finish(row, 'generating', reason='WEMAA_CORE_ACCEPTED')
    assert replies.core_status('a' * 64)['status'] == 'generating'
    with pytest.raises(Exception, match='同一会话正在生成回复'):
        replies._begin(replies.validate_prepare(body('c')))
    replies.complete_core_turn('a' * 64, 'sent', 'WEMAA_REPLY_VERIFIED')
    assert replies.core_status('a' * 64)['status'] == 'sent'
    assert replies._begin(replies.validate_prepare(body('c')))[1]


@pytest.mark.parametrize('status', ['failed', 'uncertain'])
def test_unknown_or_failed_send_never_releases_conversation(cache, status):
    row, _ = replies._begin(replies.validate_prepare(body()))
    replies.complete_core_turn(row['event_id'], status, 'TEST')
    with pytest.raises(Exception, match='同一会话正在生成回复'):
        replies._begin(replies.validate_prepare(body('c')))
    # 晚到的普通静默清理不能抹掉原始失败或不确定结果。
    assert replies.complete_core_turn(row['event_id'], 'no_reply', 'LATE')['status'] == status


def test_buffered_messages_keep_original_time_and_require_matching_completed_parent(cache):
    now = time.time()
    parent = body(); parent['observed_at'] = now - 700
    row, _ = replies._begin(parent)
    saved = json.loads(cache.get(replies.event_key('a' * 64)))
    saved['created_at'] = now - 700
    cache.set(replies.event_key('a' * 64), replies.dumps(saved))
    delayed = body('c', buffered_after='a' * 64); delayed['observed_at'] = now - 650
    with pytest.raises(Exception, match='最近五分钟|上一回合'):
        replies.validate_prepare(delayed)
    replies.complete_core_turn(row['event_id'], 'sent', 'TEST')
    assert replies.validate_prepare(delayed)['observed_at'] == now - 650
    wrong = dict(delayed, conversation_id='d' * 64)
    with pytest.raises(Exception, match='最近五分钟|上一回合'):
        replies.validate_prepare(wrong)
    too_old = dict(delayed, observed_at=now - 90000)
    with pytest.raises(Exception, match='最近五分钟|上一回合'):
        replies.validate_prepare(too_old)


def test_retry_after_five_minutes_does_not_regenerate_or_change_original_message(cache):
    request = body()
    row, _ = replies._begin(replies.validate_prepare(request))
    future = time.time() + 700
    normalized = replies.validate_prepare(request, now=future)
    assert replies._begin(normalized)[1] is False
    with pytest.raises(Exception, match='消息或身份不可修改'):
        replies._begin(dict(normalized, text='changed'))


def test_recovered_parent_admission_time_is_not_its_original_observation(cache):
    now=time.time()
    parent=body();parent['observed_at']=now-100
    row,_=replies._begin(replies.validate_prepare(parent))
    replies.complete_core_turn(row['event_id'],'sent','TEST')
    # 子消息在父消息被延迟接收前已观察到；判断原时间顺序而非 HTTP 入站时间。
    child=body('c',buffered_after='a'*64);child['observed_at']=now-60
    assert replies.validate_prepare(child,now=now+600)['observed_at']==now-60
    child['observed_at']=now-200
    with pytest.raises(Exception,match='最近五分钟|上一回合'):
        replies.validate_prepare(child,now=now+600)


@pytest.mark.parametrize('case',['sent','waiting','uncertain','wrong_chat','newer_parent'])
def test_delayed_reply_uses_only_verified_same_chat_creation_parent(cache,monkeypatch,case):
    now=time.time();request=body('c',buffered_after='d'*64);request['observed_at']=now-700
    parent={'serial_reply':True,'conversation_id':request['conversation_id'],
            'status':case if case in ('waiting','uncertain') else 'sent','created_at':now-5,
            'inbound':{'observed_at':now-800}}
    if case=='wrong_chat':parent['conversation_id']='e'*64
    if case=='newer_parent':parent['inbound']['observed_at']=now-600
    monkeypatch.setattr(replies,'_creation_buffer_parent',lambda *_:parent)
    if case=='sent':assert replies.validate_prepare(request)['observed_at']==now-700
    else:
        with pytest.raises(Exception,match='最近五分钟|上一回合'):replies.validate_prepare(request)


def test_fresh_buffered_child_also_waits_for_parent_receipt_before_admission(cache):
    row,_=replies._begin(replies.validate_prepare(body()))
    request=body('c',buffered_after=row['event_id'])
    with pytest.raises(Exception) as error:replies.validate_prepare(request)
    assert error.value.status_code==503 and error.value.detail['code']==50339
    assert cache.get(replies.event_key(request['event_id'])) is None


def test_status_poll_is_read_only_except_fail_closed_timeout(cache):
    row, _ = replies._begin(replies.validate_prepare(body()))
    saved = json.loads(cache.get(replies.event_key(row['event_id'])))
    saved['generation_deadline'] = time.time() - 1
    cache.set(replies.event_key(row['event_id']), replies.dumps(saved))
    assert replies.core_status(row['event_id'])['status'] == 'uncertain'


def test_reply_permission_is_bound_to_current_event_and_terminal_receipt(cache):
    row, _ = replies._begin(replies.validate_prepare(body()))
    assert asyncio.run(can_reply(message(), message()))
    assert not asyncio.run(can_reply(message('c'), message()))
    asyncio.run(settle(message(), 'sent', 'TEST'))
    assert not asyncio.run(can_reply(message(), message()))
    asyncio.run(settle(message(), 'no_reply', 'LATE'))
    assert replies.core_status(row['event_id'])['status'] == 'sent'


def test_prepare_keeps_event_pending_until_actual_core_receipt(cache, monkeypatch):
    from wemaa.runtime import runtime
    from wemaa.services import greetings
    async def ingest(_body,**_kwargs):
        return {'session_id': 'session'}
    async def call(coroutine):
        return await coroutine
    monkeypatch.setattr(runtime, 'ingest', ingest)
    monkeypatch.setattr(runtime, 'call', call)
    monkeypatch.setattr(greetings, 'inbound', lambda *_args: None)
    monkeypatch.setattr(replies, 'require_desktop_ready', lambda: None)
    response = asyncio.run(replies.prepare(body()))
    assert response['status'] == 'generating' and response['serial_reply'] is True
    replies.complete_core_turn('a' * 64, 'sent', 'TEST')
    assert replies.core_status('a' * 64)['status'] == 'sent'


@pytest.mark.parametrize('case', ['missing', 'stale', 'stopped', 'sending_disabled', 'manual_disabled'])
def test_new_turn_waits_before_journaling_or_model_when_desktop_unavailable(cache, monkeypatch, case):
    from wemaa.runtime import runtime
    from wemaa.services import maa
    record = {'received_at': time.time(), 'telemetry': {'collected_at': time.time(), 'runtime': {
        'worker_state': 'Running', 'trial_state': 'Ready', 'sending_enabled': True,
        'progress': {'manual_commands_enabled': True}}}}
    if case == 'missing':
        record = None
    elif case == 'stale':
        record['telemetry']['collected_at'] -= 100
    elif case == 'stopped':
        record['telemetry']['runtime']['worker_state'] = 'Ready'
    elif case == 'sending_disabled':
        record['telemetry']['runtime']['sending_enabled'] = False
    else:
        record['telemetry']['runtime']['progress']['manual_commands_enabled'] = False
    monkeypatch.setattr(maa, 'latest', lambda: record)
    ingest = AsyncMock()
    monkeypatch.setattr(runtime, 'ingest', ingest)
    with pytest.raises(Exception, match='尚未就绪') as exc:
        asyncio.run(replies.prepare(body()))
    assert exc.value.status_code == 503
    assert cache.get(replies.event_key('a' * 64)) is None
    ingest.assert_not_called()


def test_existing_turn_can_be_queried_while_desktop_is_unavailable(cache, monkeypatch):
    request = body()
    replies._begin(replies.validate_prepare(request))
    def unexpected_admission():
        raise AssertionError('已有事件不能重新进入就绪门禁')
    monkeypatch.setattr(replies, 'require_desktop_ready', unexpected_admission)
    assert asyncio.run(replies.prepare(request))['status'] == 'generating'


def test_identity_database_busy_waits_before_event_or_model(cache, monkeypatch):
    import sqlite3
    from wemaa.runtime import runtime
    async def call(coroutine):return await coroutine
    monkeypatch.setattr(runtime,'call',call)
    monkeypatch.setattr(runtime,'admit_identity',AsyncMock(side_effect=sqlite3.OperationalError('database is locked')))
    ingest=AsyncMock();monkeypatch.setattr(runtime,'ingest',ingest)
    monkeypatch.setattr(replies,'require_desktop_ready',lambda:None)
    with pytest.raises(Exception) as exc:asyncio.run(replies.prepare(body()))
    assert exc.value.detail['code']==50336
    assert cache.get(replies.event_key('a'*64)) is None
    ingest.assert_not_called()


def retry_permit(cache,request,**changes):
    row,_=replies._begin(replies.validate_prepare(request))
    replies.complete_core_turn(row['event_id'],'uncertain','WEMAA_CORE_OUTCOME_UNKNOWN')
    old=json.loads(cache.get(replies.event_key(row['event_id'])))
    old['operator_retry_ingress']={'kind':'verified_empty_core_ingress_v1',
        'expected_fingerprint':old['fingerprint'],'expires_at':time.time()+60,**changes}
    cache.set(replies.event_key(row['event_id']),replies.dumps(old))
    return old


def test_operator_pre_ingest_resume_keeps_time_and_dispatches_once(cache,monkeypatch):
    from wemaa.runtime import runtime
    from wemaa.services import greetings
    request=body();retry_permit(cache,request)
    async def call(coroutine):return await coroutine
    monkeypatch.setattr(runtime,'call',call)
    ingest=AsyncMock(return_value={'session_id':'session'})
    monkeypatch.setattr(runtime,'ingest',ingest)
    monkeypatch.setattr(replies,'core_ingest_absent',lambda _event:True)
    monkeypatch.setattr(replies,'require_desktop_ready',lambda:None)
    monkeypatch.setattr(greetings,'inbound',lambda *_args:None)
    assert asyncio.run(replies.prepare(request))['status']=='generating'
    assert asyncio.run(replies.prepare(request))['status']=='generating'
    ingest.assert_awaited_once()
    current=json.loads(cache.get(replies.event_key('a'*64)))
    assert current['inbound']['observed_at']==request['observed_at']
    assert current['operator_ingress_retry_count']==1
    assert 'operator_retry_ingress' not in current
    archived=json.loads(cache.lrange(replies.PREFIX+'operator-recovery-audit',0,0)[0])
    assert archived['previous']['status']=='uncertain'


@pytest.mark.parametrize('case',['core_or_delivery_present','expired','wrong_fingerprint','wrong_outcome'])
def test_operator_resume_never_bypasses_uncertainty_without_proof(cache,case):
    request=body();changes={}
    if case=='expired':changes['expires_at']=time.time()-1
    if case=='wrong_fingerprint':changes['expected_fingerprint']='wrong'
    old=retry_permit(cache,request,**changes)
    if case=='wrong_outcome':
        old['reason']='SEND_OUTCOME_UNKNOWN';cache.set(replies.event_key('a'*64),replies.dumps(old))
    with pytest.raises(Exception,match='保留原阻塞'):
        replies._claim_operator_ingress_retry(request,case!='core_or_delivery_present')
    assert replies.core_status('a'*64)['status']=='uncertain'


@pytest.mark.parametrize('failed', [False, True])
def test_group_classifier_failure_is_blocked_not_silence(cache, monkeypatch, failed):
    from wemaa.runtime import runtime
    from wemaa.services import greetings
    async def ingest(_body,**_kwargs):
        if failed:
            raise ValueError('WEMAA_GROUP_CLASSIFICATION_FAILED')
        return {'session_id': 'group', 'group_reply': {
            'reply': False, 'reason': 'classification_failed'}}
    async def call(coroutine):
        return await coroutine
    monkeypatch.setattr(runtime, 'ingest', ingest)
    monkeypatch.setattr(runtime, 'call', call)
    monkeypatch.setattr(greetings, 'inbound', lambda *_args: None)
    monkeypatch.setattr(replies, 'require_desktop_ready', lambda: None)
    request = body(); request['conversation_type'] = 'group'
    first = asyncio.run(replies.prepare(request))
    assert first['status'] == 'failed'
    assert first['reason'] == 'WEMAA_GROUP_CLASSIFICATION_FAILED'
    # 同一事件查询保留失败；下一消息仍被同会话 FIFO 拦住。
    assert asyncio.run(replies.prepare(request))['status'] == 'failed'
    with pytest.raises(Exception, match='同一会话正在生成回复'):
        replies._begin(replies.validate_prepare(body('c')))
