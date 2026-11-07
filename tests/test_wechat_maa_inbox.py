"""Incoming text contract, safe identity binding and loss-aware cursor polling."""
from copy import deepcopy
import hashlib
import json

import pytest
import redis

from wemaa.errors import BizError
from wemaa.services import maa, api as service, inbox
from tests.test_wechat_maa_api import cache, seed, request, client, AUTH, BASE


def incoming(identity=1, seen=990, text='原文\n保留换行'):
    return {'id':f'{identity:032x}','conversation_id':'b'*64,'seen':seen,'source_snapshot_status':'append',
            'payload':{'direction':'incoming','message_kind':'text','text':text,'source':'maa_native_clipboard',
                       'visual':{'verified':True},'text_copy':{'verified':True,'method':'native_context_menu',
                           'sha256':hashlib.sha256(text.encode()).hexdigest()},'private_diagnostic':'hidden'}}


def observation(messages=None):
    row=seed()
    row['conversations']=[{'id':'b'*64,'title':'测试好友','manual_target_observed':True,
        'private_contact':{'contact_id':'wx:test_user','wechat_id':'test_user','profile_verified':True,'verified_at':985},
        'messages':messages if messages is not None else [incoming()]}]
    return row


def read(after=0,limit=100):
    return service.received_messages('test_user',None,after,limit)


def test_original_text_privacy_repeat_sync_and_occurrence_ids(cache):
    row=observation([incoming(1,text='相同文字'),incoming(2,text='相同文字'),incoming(3,text='原文\n换行')])
    maa.store_telemetry(row);first=read();maa.store_telemetry(row);again=read()
    assert first['items']==again['items'] and len(first['items'])==3
    assert len({r['id'] for r in first['items']})==3
    assert first['items'][2]['text']=='原文\n换行'
    assert 'private_diagnostic' not in json.dumps(first) and 'conversation_id' not in json.dumps(first)
    assert first['coverage']=='observed_only' and first['polling_enabled'] is False
    assert read(first['next_cursor'])['items']==[]


def partial_message(identity=1,text='部分成功的原文'):
    message=incoming(identity,text=text)
    message.update(source_snapshot_status='review',snapshot_id=123,ordinal=2)
    message['message_receipt']={'kind':'unique_visual_append','event_id':message['id']+'a'*32,
        'snapshot_id':123,'ordinal':2,'first_seen':message['seen']}
    return message


def test_partial_batch_exposes_only_individually_verified_occurrences(cache):
    good=partial_message(1)
    bad=partial_message(2);bad['payload']['text_copy']['verified']=False
    long=partial_message(3,text='原'*2001)
    row=observation([good,bad,long]);maa.store_telemetry(row)
    result=read()
    assert [r['text'] for r in result['items']]==['部分成功的原文','原'*2001]
    bad['payload']['text_copy']['verified']=True
    maa.store_telemetry(row)
    fresh=read(result['next_cursor'])
    assert len(fresh['items'])==1 and fresh['items'][0]['id']==bad['id']
    maa.store_telemetry(row)
    assert read(fresh['next_cursor'])['items']==[]


@pytest.mark.parametrize('change',['event','snapshot','ordinal','seen','kind','baseline','missing'])
def test_partial_receipt_rejects_unbound_or_historical_proof(cache,change):
    message=partial_message();receipt=message['message_receipt']
    if change=='event':receipt['event_id']='f'*64
    elif change=='snapshot':receipt['snapshot_id']=124
    elif change=='ordinal':receipt['ordinal']=3
    elif change=='seen':receipt['first_seen']=991
    elif change=='kind':receipt['kind']='guessed'
    elif change=='baseline':message['source_snapshot_status']='baseline'
    else:message.pop('message_receipt')
    maa.store_telemetry(observation([message]))
    assert read()['items']==[]


def test_incremental_pages_and_new_receipt_after_restart(cache):
    row=observation([incoming(n,seen=980+n) for n in range(1,6)]);maa.store_telemetry(row)
    first=read(limit=2);second=read(first['next_cursor'],2);third=read(second['next_cursor'],2)
    assert [len(x['items']) for x in (first,second,third)]==[2,2,1]
    assert first['has_more'] and second['has_more'] and not third['has_more']
    assert [m['cursor'] for x in (first,second,third) for m in x['items']]==[1,2,3,4,5]
    row['conversations'][0]['messages'].append(incoming(6,seen=996));maa.store_telemetry(row)
    assert [x['cursor'] for x in read(third['next_cursor'])['items']]==[6]
    assert inbox.read({'contact_id':'wx:someone_else','wechat_id':'someone_else'},0,100)['items']==[]


@pytest.mark.parametrize('change', ['binding','wrong_account','unverified_profile','enterprise','wrong_contact_facts','group','outgoing',
    'media','ocr','copy_failed','copy_method','copy_hash','visual','review','expired','future','wrong_conversation','bad_id'])
def test_unverified_foreign_and_nontext_messages_never_exposed(cache,change):
    row=observation();conversation=row['conversations'][0];message=conversation['messages'][0];payload=message['payload']
    if change=='binding':conversation.pop('private_contact')
    elif change=='wrong_account':conversation['private_contact']['wechat_id']='foreign_user'
    elif change=='unverified_profile':conversation['private_contact']['profile_verified']=False
    elif change=='enterprise':row['contacts'][0]['scope']='enterprise'
    elif change=='wrong_contact_facts':row['contacts'][0]['facts']['scope']='enterprise'
    elif change=='group':conversation['group_profile_status']='unverified'
    elif change=='outgoing':payload['direction']='outgoing'
    elif change=='media':payload['message_kind']='media'
    elif change=='ocr':payload['source']='maa_bubble_ocr'
    elif change=='copy_failed':payload['text_copy']['verified']=False
    elif change=='copy_method':payload['text_copy']['method']='guessed'
    elif change=='copy_hash':payload['text_copy']['sha256']='0'*64
    elif change=='visual':payload['visual']['verified']=False
    elif change=='review':message['source_snapshot_status']='review'
    elif change=='expired':message['seen']=1000-maa.HISTORY_TTL-1
    elif change=='future':message['seen']=1031
    elif change=='wrong_conversation':message['conversation_id']='c'*64
    else:message['id']='invalid'
    maa.store_telemetry(row)
    assert inbox.read({'contact_id':'wx:test_user','wechat_id':'test_user'},0,100)['items']==[]


def test_filter_token_scope_validation_and_read_does_not_enqueue(cache,monkeypatch):
    maa.store_telemetry(observation())
    with client() as c:
        for auth in ({},{'Authorization':'Bearer machine-only'},{'Authorization':'Bearer invalid'}):
            assert c.get(BASE+'/received-messages?wechat_id=test_user',headers=auth).status_code==401
        assert c.get(BASE+'/received-messages?contact_id=wx:test_user',headers=AUTH).json()['data']['items'][0]['text']=='原文\n保留换行'
        for query in ('','?wechat_id=test_user&contact_id=wx:test_user','?wechat_id=test_user&after=999'):
            assert c.get(BASE+'/received-messages'+query,headers=AUTH).status_code==400
        for query in ('?wechat_id=test_user&after=-1','?wechat_id=test_user&limit=201'):
            assert c.get(BASE+'/received-messages'+query,headers=AUTH).status_code==422
        assert c.get(BASE+'/received-messages?wechat_id=测试好友',headers=AUTH).status_code==404
        assert c.get(BASE+'/messages/'+'f'*32+'/replies',headers=AUTH).status_code==404
        monkeypatch.setattr(maa.settings,'WECHAT_MAA_API_TOKEN','')
        assert c.get(BASE+'/received-messages?wechat_id=test_user',headers=AUTH).status_code==503
    assert not cache.lists.get(maa.PREFIX+'queue')


def test_replies_are_api_only_after_issue_and_only_for_sent_task(cache,monkeypatch):
    row=observation([incoming(1,995)]);maa.store_telemetry(row)
    message,_=service.create_message(request());command=maa.next_command()
    assert service.replies(message['message_id'],0,100)['items']==[]
    monkeypatch.setattr(maa.time,'time',lambda:1002)
    row['conversations'][0]['messages'].append(incoming(2,1001,'很快的来信'))
    row['collected_at']=1002;maa.store_telemetry(row)
    detail={'status':'sent','conversation_id':'b'*64,'evidence':{'title_verified':True,'outgoing_verified':True,'profile_verified':True},
            'diagnostic':{'send_click_attempted':True,'draft_retained':False}}
    maa.record_result(command['id'],{'status':'succeeded','detail':detail})
    result=service.replies(message['message_id'],0,100)
    assert result['send_status']=='sent' and [m['text'] for m in result['items']]==['很快的来信']
    admin=deepcopy(json.loads(cache.get(maa._command_key(command['id']))));admin.pop('source')
    cache.set(maa._command_key('c'*32),json.dumps(admin))
    with pytest.raises(BizError) as error:service.replies('c'*32,0,100)
    assert error.value.status_code==404


def test_retention_truncation_no_reimport_and_seq_survives_idle_expiry(cache,monkeypatch):
    monkeypatch.setattr(inbox,'MAX_RECORDS',2)
    row=observation([incoming(n,980+n) for n in range(1,5)]);maa.store_telemetry(row)
    result=read(after=1);assert result['cursor_expired'] and result['history_truncated']
    assert [m['cursor'] for m in result['items']]==[3,4]
    maa.store_telemetry(row);assert read()['latest_cursor']==4
    monkeypatch.setattr(maa.time,'time',lambda:1000+maa.HISTORY_TTL)
    assert read(after=3)['items']==[] and read(after=3)['cursor_expired']
    cache.values.pop(inbox.KEY)  # Simulate the seven-day idle TTL; sequence is durable.
    assert read(after=4)['next_cursor']==4
    row['collected_at']=1000+maa.HISTORY_TTL
    row['conversations'][0]['messages']=[incoming(5,1000+maa.HISTORY_TTL)]
    maa.store_telemetry(row);assert read(after=4)['items'][0]['cursor']==5


def test_stale_data_is_explicit_and_redis_errors_are_503(cache,monkeypatch):
    maa.store_telemetry(observation());monkeypatch.setattr(maa.time,'time',lambda:1100)
    assert read()['stale'] is True and len(read()['items'])==1
    def broken():raise redis.ConnectionError('private connection')
    monkeypatch.setattr(maa,'client',broken)
    with client() as c:assert c.get(BASE+'/received-messages?wechat_id=test_user',headers=AUTH).status_code==503


def test_actual_maa_baseline_fixture_to_public_inbox(cache,monkeypatch):
    """Fixture emitted by Maa Store -> private baseline -> admin_export tests."""
    from pathlib import Path
    fixture_path=Path(__file__).parent/'fixtures/wechat_maa_private_baseline_chain.json'
    if not fixture_path.is_file():
        from unittest import SkipTest
        raise SkipTest('私有视觉夹具不随开源版发布')
    chain=json.loads(fixture_path.read_text())
    monkeypatch.setattr(maa.time,'time',lambda:chain['now'])
    for stage in ('review','baseline'):
        maa.store_telemetry(chain[stage])
        assert read()['items']==[]
    maa.store_telemetry(chain['first'])
    first=read();assert [r['text'] for r in first['items']]==['全新唯一验收文字']
    assert first['items'][0]['id']==chain['first']['conversations'][0]['messages'][0]['id']
    maa.store_telemetry(chain['repeat']);assert read()['items']==first['items']
    maa.store_telemetry(chain['same_body'])
    both=read();assert [r['text'] for r in both['items']]==['全新唯一验收文字']*2
    assert len({r['id'] for r in both['items']})==2
    one=read(limit=1);two=read(one['next_cursor'],1)
    assert one['has_more'] and not two['has_more']
    assert [r['cursor'] for r in one['items']+two['items']]==[1,2]
    with client() as c:
        result=c.get(BASE+'/received-messages?wechat_id=test_user&after=0',headers=AUTH)
        assert result.status_code==200 and result.json()['data']['items']==both['items']
        assert c.get(BASE+'/received-messages?wechat_id=test_user&after=2',headers=AUTH).json()['data']['items']==[]
    assert not cache.lists.get(maa.PREFIX+'queue')


def test_replies_boundary_sent_is_not_read_or_per_message_relation(cache,monkeypatch):
    row=observation([]);maa.store_telemetry(row)
    task,_=service.create_message(request());command=maa.next_command()
    monkeypatch.setattr(maa.time,'time',lambda:1005)
    row['collected_at']=1005
    row['conversations'][0]['messages']=[incoming(1,999,'下发前'),incoming(2,1000,'下发时'),incoming(3,1001,'回执前快速来信')]
    maa.store_telemetry(row)
    assert service.replies(task['message_id'],0,100)['items']==[]
    detail={'status':'sent','conversation_id':'b'*64,'evidence':{'title_verified':True,'outgoing_verified':True,'profile_verified':True},'diagnostic':{'send_click_attempted':True,'draft_retained':False}}
    maa.record_result(command['id'],{'status':'succeeded','detail':detail})
    result=service.replies(task['message_id'],0,100)
    assert [r['text'] for r in result['items']]==['下发时','回执前快速来信']
    assert result['since']==1000 and result['send_status']=='sent'
    assert all('in_reply_to' not in r and 'read_at' not in r for r in result['items'])
    assert 'read_at' not in service.message(task['message_id'])
    first=service.replies(task['message_id'],0,1);last=service.replies(task['message_id'],first['next_cursor'],1)
    assert first['has_more'] and not last['has_more']
    assert first['items']+last['items']==result['items']


def test_review_and_baseline_payloads_cannot_be_imported_as_new_messages(cache):
    row=observation([])
    row['conversations'][0]['latest_snapshot']={'status':'review','payloads':[incoming(1)['payload']]}
    maa.store_telemetry(row);assert read()['items']==[]
    row['conversations'][0]['latest_snapshot']['status']='baseline'
    maa.store_telemetry(row);assert read()['items']==[]
