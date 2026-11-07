import json
import uuid
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from wemaa.database import Base
from wemaa.errors import BizError
from wemaa.models import WechatSchedule
from wemaa.services import schedules as s, maa
from tests.helpers import MemoryRedis

@pytest.fixture
def env(monkeypatch):
    engine = create_engine('sqlite://')
    WechatSchedule.__table__.create(engine)
    db = Session(engine)
    cache = MemoryRedis()
    monkeypatch.setattr(maa, 'client', lambda: cache)
    monkeypatch.setattr(maa.settings, 'WECHAT_MAA_REPLY_ENABLED', True)
    monkeypatch.setattr(s.time, 'time', lambda: 1000.)
    record = {'received_at':1000., 'telemetry': {'runtime': {'worker_state':'Running', 'progress':{'manual_commands_enabled':True}}, 'conversations': [{'id':'a'*64,'title':'测试对象','manual_target_observed':True,'avatar_asset':'b'*64}]}}
    cache.set(maa.PREFIX+'latest', json.dumps(record))
    monkeypatch.setattr(maa, 'is_stale', lambda r: r is None)
    yield db, cache, record
    db.close()

def body(**extra):
    return dict(conversation_id='a'*64, text='测试消息', send_at=1060., request_id=str(uuid.uuid4()), **extra)

def test_create_idempotent_edit_cancel(env):
    db, _, _ = env
    data=body(); row=s.save(db,data,1);db.commit()
    assert s.save(db,data,1).id==row.id
    data.update(text='修改', revision=1)
    s.save(db,data,1,row.id);db.commit()
    with pytest.raises(BizError): s.cancel(db,row.id,1)
    s.cancel(db,row.id,2);db.commit()
    assert row.status=='cancelled'

def test_due_once_verified_receipt(env, monkeypatch):
    db, cache, _=env
    row=s.save(db,body(),1);db.commit()
    s.dispatch_due(db);assert row.status=='pending'
    monkeypatch.setattr(s.time,'time',lambda:1061.)
    s.dispatch_due(db);assert row.status=='dispatched'
    cmd=maa.next_command(); assert cmd['params']['text']=='测试消息'
    s.dispatch_due(db);assert maa.next_command() is None
    maa.record_result(cmd['id'],{'status':'succeeded','detail':{'status':'sent','outgoing_verified':True}})
    s.sync_results(db);assert row.status=='sent'
    cache.values.clear(); assert s.serialize(row)['status']=='sent'

def test_uncertain_terminal(env, monkeypatch):
    db, cache, _=env
    row=s.save(db,body(),1);db.commit()
    monkeypatch.setattr(s.time,'time',lambda:1061.)
    s.dispatch_due(db);cmd=maa.next_command()
    maa.record_result(cmd['id'],{'status':'failed','detail':{'status':'uncertain'}})
    s.dispatch_due(db);assert row.status=='uncertain'
    assert maa.next_command() is None

def test_offline_expires_and_changed_identity_blocks(env, monkeypatch):
    db, cache, record=env
    row=s.save(db,body(),1);db.commit()
    monkeypatch.setattr(s.time,'time',lambda:1061.)
    cache.values.pop(maa.PREFIX+'latest');s.dispatch_due(db)
    assert row.status=='pending' and row.detail['reason']
    monkeypatch.setattr(s.time,'time',lambda:1361.)
    s.dispatch_due(db);assert row.status=='expired'
    monkeypatch.setattr(s.time,'time',lambda:1000.)
    cache.set(maa.PREFIX+'latest',json.dumps(record))
    row=s.save(db,body(),1);db.commit()
    record['telemetry']['conversations'][0]['avatar_asset']='c'*64
    cache.set(maa.PREFIX+'latest',json.dumps(record))
    monkeypatch.setattr(s.time,'time',lambda:1061.)
    s.dispatch_due(db);assert row.status=='failed' and row.command_id is None

def test_enqueue_commit_gap_and_edit_gate(env, monkeypatch):
    db, _, _=env
    row=s.save(db,body(),1);db.commit()
    command,_=maa.enqueue({'action':'send_message','params':{'conversation_id':row.conversation_id,'text':row.text},'request_id':s.request_id(row)},1)
    with pytest.raises(BizError): s.cancel(db,row.id,1)
    with pytest.raises(BizError): s.save(db,{**body(),'revision':1},1,row.id)
    monkeypatch.setattr(s.time,'time',lambda:1061.)
    s.dispatch_due(db);assert row.command_id==command['id']
    assert maa.next_command()['id']==command['id'] and maa.next_command() is None

@pytest.mark.parametrize('at',[float('nan'),float('inf'),1000.,9999999999.])
def test_invalid_time(env,at):
    with pytest.raises(BizError): s.save(env[0],{**body(),'send_at':at},1)

def test_weekly_timezone_mutually_exclusive_and_skip_missed(env):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    at=datetime(2026,10,1,9,0,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
    config={'weekdays':[0,3],'time':'09:00'}
    assert s.next_occurrence(config,at)==datetime(2026,10,5,9,0,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
    with pytest.raises(BizError):s.save(env[0],{**body(),'recurrence':config},1)
    for config in ({'weekdays':[],'time':'09:00'},{'weekdays':[True],'time':'09:00'},{'weekdays':[0],'time':'24:00'}):
        with pytest.raises(BizError):s.recurrence_config({'recurrence':config})

def test_greeting_weekly_once_receipt_and_no_uncertain_restart(env,monkeypatch):
    db,cache,_=env
    data=body();data.pop('send_at');data['recurrence']={'weekdays':list(range(7)),'time':'09:00'}
    data['messages']=[{'kind':'greeting','category':'daily'}]
    row=s.save(db,data,1);db.commit();at=row.send_at
    monkeypatch.setattr(s.time,'time',lambda:at+1)
    s.dispatch_due(db);cmd=maa.next_command()
    assert cmd['params']['text'] in s.GREETINGS['daily']
    text=cmd['params']['text'];s.dispatch_due(db);assert maa.next_command() is None
    maa.record_result(cmd['id'],{'status':'succeeded','detail':{'status':'sent'}})
    s.dispatch_due(db);assert row.status=='pending' and row.send_at>at and row.revision==2
    assert row.detail['history'][0]['messages'][0]['resolved_text']==text
    assert 'command_id' not in row.messages[0]
    monkeypatch.setattr(s.time,'time',lambda:row.send_at+1)
    s.dispatch_due(db);cmd=maa.next_command()
    maa.record_result(cmd['id'],{'status':'failed','detail':{'status':'uncertain'}})
    s.dispatch_due(db);assert row.status=='uncertain'
    s.dispatch_due(db);assert maa.next_command() is None

def test_recoverable_delete_cancels_pending_and_blocks_claimed(env):
    db,_,_=env
    row=s.save(db,body(),1);db.commit()
    s.delete(db,row.id,1);db.commit()
    assert row.deleted_at and row.status=='cancelled'
    row=s.save(db,body(),1);db.commit()
    maa.enqueue({'action':'send_message','params':{'conversation_id':row.conversation_id,'text':row.text},'request_id':s.request_id(row)},1)
    with pytest.raises(BizError):s.delete(db,row.id,1)

def test_group_avatar_change_matches_but_private_change_does_not():
    assert s.same_target({'title':'群(55)','group_name':'群','avatar_asset':'new'},{'title':'群(54)','avatar_asset':'old'})
    assert not s.same_target({'title':'Demo User','avatar_asset':'new'},{'title':'Demo User','avatar_asset':'old'})


@pytest.fixture
def media_env(env,monkeypatch):
    from wemaa.models import WechatDelivery
    from wemaa.services import media as wechat_creation
    from types import SimpleNamespace
    db,cache,record=env
    WechatDelivery.__table__.create(db.bind)
    monkeypatch.setattr(maa.settings,'WECHAT_MAA_CREATION_ENABLED',True)
    def owner(db,user,aid):
        if aid not in (11,12):raise maa.not_found('素材无权限')
        return SimpleNamespace(id=aid,type='image' if aid==11 else 'video',mime='image/png' if aid==11 else 'video/mp4',name='image.png' if aid==11 else 'video.mp4')
    monkeypatch.setattr(wechat_creation,'owner_asset',owner)
    return env

def mixed():
    return {**body(),'messages':[{'kind':'text','text':'第一条'},{'kind':'image','asset_id':11},{'kind':'video','asset_id':12}]}

def test_mixed_order_receipts_and_restart(media_env,monkeypatch):
    from wemaa.models import WechatDelivery
    from sqlalchemy import select
    db,cache,_=media_env
    row=s.save(db,mixed(),1);db.commit()
    monkeypatch.setattr(s.time,'time',lambda:1061.)
    s.dispatch_due(db);cmd=maa.next_command()
    assert cmd['params']['text']=='第一条' and row.status=='sending'
    assert db.scalars(select(WechatDelivery)).all()==[]
    s.dispatch_due(db);assert maa.next_command() is None
    maa.record_result(cmd['id'],{'status':'succeeded','detail':{'status':'sent'}})
    s.dispatch_due(db)
    items=row.messages;assert items[0]['status']=='sent' and items[1]['delivery_id'] and items[2]['status']=='pending'
    did=items[1]['delivery_id'];db.expire_all();s.dispatch_due(db)
    assert row.messages[1]['delivery_id']==did and len(db.scalars(select(WechatDelivery)).all())==1
    delivery=db.get(WechatDelivery,did);delivery.status='sent';db.commit()
    s.dispatch_due(db);assert row.messages[2]['delivery_id']
    d=db.get(WechatDelivery,row.messages[2]['delivery_id']);d.status='sent';db.commit()
    s.dispatch_due(db);assert row.status=='sent' and row.detail['sent_count']==3
    assert [m['status'] for m in row.messages]==['sent']*3
    cache.values.clear();assert s.serialize(row)['status']=='sent'

def test_media_uncertain_stops_rest(media_env,monkeypatch):
    from wemaa.models import WechatDelivery
    db,_,_=media_env
    data=mixed();data['messages']=data['messages'][1:]
    row=s.save(db,data,1);db.commit()
    monkeypatch.setattr(s.time,'time',lambda:1061.)
    s.dispatch_due(db);did=row.messages[0]['delivery_id']
    d=db.get(WechatDelivery,did);d.status='uncertain';db.commit()
    s.dispatch_due(db);assert row.status=='uncertain' and row.messages[1]['status']=='skipped'
    s.dispatch_due(db);assert row.messages[0]['delivery_id']==did

def test_text_uncertain_stops_media(media_env,monkeypatch):
    db,_,_=media_env
    row=s.save(db,mixed(),1);db.commit()
    monkeypatch.setattr(s.time,'time',lambda:1061.)
    s.dispatch_due(db);cmd=maa.next_command()
    maa.record_result(cmd['id'],{'status':'failed','detail':{'status':'uncertain'}})
    s.dispatch_due(db)
    assert row.status=='uncertain' and [m['status'] for m in row.messages]==['uncertain','skipped','skipped']

def test_media_queued_expiry_cancels_permission(media_env,monkeypatch):
    from wemaa.models import WechatDelivery
    db,_,_=media_env
    row=s.save(db,{**body(),'messages':[{'kind':'video','asset_id':12}]},1);db.commit()
    monkeypatch.setattr(s.time,'time',lambda:1061.)
    s.dispatch_due(db)
    monkeypatch.setattr(s.time,'time',lambda:1362.)
    s.dispatch_due(db)
    assert row.status=='expired' and db.get(WechatDelivery,row.messages[0]['delivery_id']).status=='cancelled'

@pytest.mark.parametrize('messages',[[],[{'kind':'image','asset_id':99}],[{'kind':'video','asset_id':11}],[{'kind':'text','text':' '}],[{'kind':'text','text':'ok'}]*21])
def test_batch_validation(media_env,messages):
    with pytest.raises(BizError):s.save(media_env[0],{**body(),'messages':messages},1)

def test_cancel_batch_marks_each_message_and_never_dispatches(media_env,monkeypatch):
    db,_,_=media_env
    row=s.save(db,mixed(),1);db.commit()
    s.cancel(db,row.id,1);db.commit()
    assert [m['status'] for m in row.messages]==['cancelled']*3
    monkeypatch.setattr(s.time,'time',lambda:1061.)
    s.dispatch_due(db);assert maa.next_command() is None
    assert s.serialize(row)['status']=='cancelled'
