"""即时创作回信必须逐条核验；生成任务状态不代表微信送达。"""
import json

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from wemaa.creation_turns import status
from wemaa.errors import BizError

EID, CID = 'a' * 64, 'b' * 64


@pytest.fixture
def db():
    with Session(create_engine('sqlite://')) as db:
        db.execute(text('CREATE TABLE wechat_creation_requests '
                        '(event_id TEXT,conversation_id TEXT,status TEXT,body TEXT,result TEXT,created_at REAL)'))
        db.execute(text('CREATE TABLE wechat_deliveries '
                        '(id TEXT,dedupe_key TEXT,status TEXT,detail TEXT,created_at REAL)'))
        db.execute(text('INSERT INTO wechat_creation_requests VALUES '
                        '(:event,:cid,:status,:body,:result,100)'),
                   {'event': EID, 'cid': CID, 'status': 'handled',
                    'body': json.dumps({'observed_at': 90}),
                    'result': json.dumps({'handled': True})})
        db.commit()
        yield db


def delivery(db, suffix='reply', state='sent', proof=True, id='d' * 32, event=EID):
    db.execute(text('INSERT INTO wechat_deliveries VALUES (:id,:key,:status,:detail,110)'),
               {'id': id, 'key': event + ':' + suffix, 'status': state,
                'detail': json.dumps({'evidence': {'title_verified': proof, 'outgoing_verified': proof}})})
    db.commit()


def test_exact_source_and_all_immediate_receipts_required(db):
    delivery(db)
    delivery(db, 'asset', state='sending', id='e' * 32)
    delivery(db, 'result:123', state='failed', id='f' * 32)
    delivery(db, event='z' * 64, state='failed', id='1' * 32)
    result = status(db, EID, CID)
    assert result['status'] == 'waiting'
    assert len(result['deliveries']) == 2
    db.execute(text("UPDATE wechat_deliveries SET status='sent' WHERE id=:id"), {'id': 'e' * 32})
    assert status(db, EID, CID)['status'] == 'sent'
    assert status(db, EID, CID)['observed_at'] == 90


@pytest.mark.parametrize('state', ['failed', 'uncertain', 'cancelled'])
def test_failed_delivery_blocks_even_if_other_reply_sent(db, state):
    delivery(db)
    delivery(db, 'asset', state=state, id='e' * 32)
    assert status(db, EID, CID)['status'] == 'uncertain'


def test_sent_without_native_evidence_never_releases(db):
    delivery(db, proof=False)
    assert status(db, EID, CID)['status'] == 'waiting'


def test_explicit_reference_cannot_substitute_another_events_receipt(db):
    delivery(db)
    delivery(db, event='z' * 64, id='f' * 32)
    db.execute(text('UPDATE wechat_creation_requests SET result=:value'),
               {'value': json.dumps({'handled': True, 'delivery_id': 'f' * 32})})
    assert status(db, EID, CID)['reason'] == 'CREATION_IMMEDIATE_REFERENCE_MISMATCH'


def test_missing_delivery_is_not_silence_and_wrong_chat_cannot_read(db):
    assert status(db, EID, CID)['status'] == 'uncertain'
    with pytest.raises(BizError) as error:
        status(db, EID, 'c' * 64)
    assert error.value.status_code == 404
    db.execute(text("UPDATE wechat_creation_requests SET status='processing',result='{}'"))
    assert status(db, EID, CID)['status'] == 'waiting'


def test_unavailable_tracking_never_looks_complete():
    with Session(create_engine('sqlite://')) as db, pytest.raises(BizError) as error:
        status(db, EID, CID)
    assert error.value.status_code == 503


@pytest.mark.parametrize('eid,cid', [('short', CID), (EID, 'bad'), (None, CID)])
def test_invalid_key_rejected_before_sql(db, eid, cid):
    with pytest.raises(BizError) as error:
        status(db, eid, cid)
    assert error.value.status_code == 400


def test_receipt_status_route_remains_machine_authenticated_and_read_only(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from wemaa import routes, creation_turns
    from wemaa.services import maa
    token='a'*64;calls=[]
    monkeypatch.setattr(maa.settings,'WECHAT_MAA_TOKEN',token)
    def observed(db,event,cid):
        calls.append((event,cid));return {'protocol':'creation_receipts_v1','status':'sent'}
    monkeypatch.setattr(creation_turns,'status',observed)
    app=FastAPI();app.include_router(routes.machine,prefix='/api/v1/wechat-maa')
    app.dependency_overrides[routes.get_db]=lambda:object()
    path='/api/v1/wechat-maa/creation/events/'+EID+'/status?conversation_id='+CID
    with TestClient(app) as client:
        assert client.get(path).status_code==401
        assert calls==[]
        result=client.get(path,headers={'Authorization':'Bearer '+token})
        assert result.status_code==200 and result.json()['data']['status']=='sent'
        assert calls==[(EID,CID)]
        assert client.post(path,headers={'Authorization':'Bearer '+token},json={}).status_code==404
