"""External API contract; all queues, identities and receipts are simulated."""
from copy import deepcopy
from pathlib import Path
import json
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
import redis

from wemaa import api_routes as api, routes as machine_api
from wemaa.errors import BizError
from wemaa.services import maa, api as service
from tests.helpers import MemoryRedis, observation

TOKEN = 'a' * 64
AUTH = {'Authorization': 'Bearer ' + TOKEN}
BASE = '/api/v1/wechat-maa-api'


@pytest.fixture()
def cache(monkeypatch):
    cache = MemoryRedis()
    monkeypatch.setattr(maa, 'client', lambda: cache)
    monkeypatch.setattr(maa.time, 'time', lambda: 1000.0)
    monkeypatch.setattr(maa.settings, 'WECHAT_MAA_REPLY_ENABLED', True)
    monkeypatch.setattr(maa.settings, 'WECHAT_MAA_API_TOKEN', TOKEN)
    monkeypatch.setattr(maa.settings, 'WECHAT_MAA_TOKEN', 'machine-only')
    return cache


def seed():
    row = observation()
    row['runtime'].update(worker_state='Running', progress={'manual_commands_enabled': True, 'polling_enabled': False})
    row['scans'] = [{'id': 'a'*32, 'scope': 'personal', 'status': 'complete', 'coverage_valid': True,
                     'bottom_verified': True, 'finished': 990}]
    row['contacts'] = [{'id': 'wx:test_user', 'wechat_id': 'test_user', 'display_name': '测试好友',
                       'list_name': '测试备注', 'scope': 'personal', 'scan_id': 'a'*32, 'ordinal': 0,
                       'identity_status': 'ocr_wechat_id', 'profile_asset': 'b'*64,
                       'facts': {'scope': 'personal', 'list_avatar_sha': 'c'*64, 'private_profile': 'not-public'}}]
    maa.store_telemetry(row)
    return row


def request(**updates):
    return {'request_id': str(uuid.uuid4()), 'recipient': {'wechat_id': 'test_user'}, 'text': '你好', **updates}


def client():
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    app.include_router(machine_api.machine, prefix='/api/v1/wechat-maa')
    return TestClient(app)


def test_token_isolation_disabled_and_bounded_json(cache, monkeypatch):
    with client() as c:
        for auth in ({}, {'Authorization': 'Bearer machine-only'}, {'Authorization': 'Bearer wrong'}, {'Authorization': 'Bearer ' + TOKEN + ' '}):
            assert c.get(BASE+'/contacts', headers=auth).status_code == 401
        assert c.get('/api/v1/wechat-maa/commands/next', headers=AUTH).status_code == 401
        assert c.put('/api/v1/wechat-maa/telemetry', headers=AUTH, json=observation()).status_code == 401
        assert c.post(BASE+'/messages', headers={**AUTH, 'Content-Length': '16385'}, content='{}').status_code == 413
        assert c.post(BASE+'/messages', headers=AUTH, content='not json').status_code == 400
        monkeypatch.setattr(maa.settings, 'WECHAT_MAA_API_TOKEN', '')
        assert c.get(BASE+'/contacts', headers=AUTH).status_code == 503


def test_summary_exact_target_async_and_duplicate_before_offline(cache):
    seed();body=request()
    with client() as c:
        summary=c.get(BASE+'/contacts?keyword=测试&page_size=1', headers=AUTH).json()['data']
        assert summary['total']==1 and summary['items'][0]['contact_id']=='wx:test_user'
        assert 'facts' not in summary['items'][0] and 'profile_asset' not in summary['items'][0]
        first=c.post(BASE+'/messages',headers=AUTH,json=body)
        assert first.status_code==202
        data=first.json()['data'];assert data['status']=='queued' and data['expires_at']==1300
        cache.values.pop(maa.PREFIX+'latest')
        repeated=c.post(BASE+'/messages',headers=AUTH,json=body)
        assert repeated.status_code==200 and repeated.json()['data']['message_id']==data['message_id']
        assert len(cache.lists[maa.PREFIX+'queue'])==1
        changed=c.post(BASE+'/messages',headers=AUTH,json={**body,'text':'另一条'})
        assert changed.status_code==409
        assert c.get(BASE+'/messages/'+data['message_id'],headers=AUTH).status_code==200


@pytest.mark.parametrize('patch', [
    {'text':''}, {'text':' '*3}, {'text':'x'*401}, {'text':'a\nb'}, {'text':'a\tb'},
    {'text':'a\x7fb'}, {'text':'a\x85b'}, {'text':'a\u2028b'}, {'text':1}, {'request_id':'not-uuid'}, {'recipient':{}},
    {'recipient':{'wechat_id':'test_user','contact_id':'wx:test_user'}}, {'recipient':{'name':'测试好友'}},
    {'recipient':{'contact_id':1}}, {'extra':'not-supported'}, {'recipient':{'wechat_id':''}},
])
def test_invalid_request_never_enqueues(cache, patch):
    seed()
    with client() as c:
        assert c.post(BASE+'/messages',headers=AUTH,json=request(**patch)).status_code==400
    assert not cache.lists.get(maa.PREFIX+'queue')


def test_resolution_rejects_nickname_provisional_wrong_scope_and_ambiguity(cache):
    row=seed()
    for recipient in ({'wechat_id':'测试好友'},{'wechat_id':'test'},{'contact_id':'unknown'}):
        with pytest.raises(BizError) as error: service.create_message(request(recipient=recipient))
        assert error.value.status_code==404
    for patch in ({'identity_status':'provisional'}, {'facts':{}}, {'scope':'enterprise'}, {'scan_id':'d'*32}):
        changed=deepcopy(row);changed['contacts'][0].update(patch);maa.store_telemetry(changed)
        with pytest.raises(BizError):service.create_message(request())
    changed=deepcopy(row);changed['contacts'].append(dict(changed['contacts'][0],id='conflicting-contact',ordinal=1));maa.store_telemetry(changed)
    with pytest.raises(BizError) as error:service.create_message(request())
    assert error.value.status_code==409


def test_contact_id_busy_and_namespace_are_independent(cache):
    seed();body=request(recipient={'contact_id':'wx:test_user'})
    # The same UUID used by an administrator is a separate namespace.
    maa.enqueue({'action':'set_polling','params':{'enabled':False},'request_id':body['request_id']},1)
    admin=maa.next_command();maa.record_result(admin['id'],{'status':'succeeded','detail':{}})
    sent,created=service.create_message(body);assert created
    with pytest.raises(BizError) as error: service.create_message(request())
    assert error.value.status_code==409
    with client() as c:
        assert c.get(BASE+'/messages/'+admin['id'],headers=AUTH).status_code==404
    assert service.message(sent['message_id'])['status']=='queued'


@pytest.mark.parametrize('failure', ['offline','stale','worker','desktop','disabled','redis'])
def test_unavailable_is_503_without_task(cache, monkeypatch, failure):
    row=seed()
    if failure=='offline': cache.values.pop(maa.PREFIX+'latest')
    elif failure=='stale':monkeypatch.setattr(maa.time,'time',lambda:1091)
    elif failure=='worker':row['runtime']['worker_state']='Ready';maa.store_telemetry(row)
    elif failure=='desktop':row['runtime']['progress']['manual_commands_enabled']=False;maa.store_telemetry(row)
    elif failure=='disabled':monkeypatch.setattr(maa.settings,'WECHAT_MAA_REPLY_ENABLED',False)
    else:
        def unavailable():raise redis.ConnectionError('private host details')
        monkeypatch.setattr(maa,'client',unavailable)
    with client() as c: assert c.post(BASE+'/messages',headers=AUTH,json=request()).status_code==503
    assert not cache.lists.get(maa.PREFIX+'queue')


@pytest.mark.parametrize('detail,expected', [
    ({'status':'sent','evidence':{'title_verified':True,'outgoing_verified':True,'profile_verified':True},
      'diagnostic':{'send_click_attempted':True,'draft_retained':False,'history_before':['private']}},'sent'),
    ({'status':'sent','evidence':{'title_verified':True,'outgoing_verified':True}},'uncertain'),
    ({'status':'blocked','reason':'existing_or_unreadable_draft','diagnostic':{'send_click_attempted':False,'draft_retained':True}},'blocked'),
    ({'status':'uncertain','reason':'WORKER_INTERRUPTED_NO_RETRY'},'uncertain'),
    ({'status':'failed','reason':'PRIVATE_CONTACT_IDENTITY_MISMATCH','diagnostic':{'send_click_attempted':False}},'failed'),
    ({'error':'INTERRUPTED_EXECUTION_VERIFY_DESKTOP_BEFORE_RETRY'},'uncertain'),
])
def test_receipts_fail_closed_and_do_not_leak_or_redeliver(cache, detail, expected):
    seed();body=request();message,_=service.create_message(body);command=maa.next_command()
    assert service.message(message['message_id'])['status']=='processing'
    maa.record_result(command['id'],{'status':'succeeded' if detail.get('status')=='sent' else 'failed','detail':detail})
    result=service.message(command['id']);assert result['status']==expected
    assert 'private' not in json.dumps(result) and 'diagnostic' not in result and 'history_before' not in result
    assert service.create_message(body)[0]['message_id']==command['id']
    assert maa.next_command() is None


@pytest.mark.parametrize('outcome', ['sent','uncertain'])
def test_large_desktop_receipt_is_bounded_without_changing_verified_outcome(cache, monkeypatch, outcome):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'apps/adapter/maa_wechat'))
    from admin_agent import result_receipt
    seed();message,_=service.create_message(request());command=maa.next_command()
    detail={'status':outcome,'reason':'send_postcondition_not_verified' if outcome=='uncertain' else
            'empty_draft_and_new_exact_outgoing_verified',
            'evidence':{'title_verified':True,'outgoing_verified':outcome=='sent','profile_verified':True},
            'diagnostic':{'send_click_attempted':True,'draft_retained':False,
                          'history_before':[{'text':'本地完整诊断'*20000}]}}
    before=deepcopy(detail);status='succeeded' if outcome=='sent' else 'failed'
    with client() as c:
        path='/api/v1/wechat-maa/commands/'+command['id']+'/result'
        auth={'Authorization':'Bearer machine-only'}
        assert c.put(path,headers=auth,json={'status':status,'detail':detail}).status_code==413
        assert service.message(message['message_id'])['status']=='processing'
        receipt=result_receipt(status,detail)
        assert c.put(path,headers=auth,json=receipt).status_code==200
        assert c.put(path,headers=auth,json=receipt).status_code==200
    assert detail==before
    assert service.message(message['message_id'])['status']==outcome
    assert maa.next_command() is None


def test_unissued_expiry_vs_issued_unknown_result_and_query_404(cache, monkeypatch):
    seed();value,_=service.create_message(request());monkeypatch.setattr(maa.time,'time',lambda:1301)
    assert service.message(value['message_id'])['status']=='expired'
    assert maa.next_command() is None
    monkeypatch.setattr(maa.time,'time',lambda:1000);seed()
    value,_=service.create_message(request());maa.next_command();monkeypatch.setattr(maa.time,'time',lambda:1301)
    assert service.message(value['message_id'])['status']=='uncertain'
    assert maa.next_command() is None
    with client() as c:
        assert c.get(BASE+'/messages/'+'e'*32,headers=AUTH).status_code==404
