import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from wemaa.errors import BizError
from wemaa.identity import IdentityRegistry
from wemaa.models import WechatDelivery
from wemaa.services import maa, media, schedules


@pytest.fixture
def context(monkeypatch):
    engine = create_engine('sqlite://')
    WechatDelivery.__table__.create(engine)
    for name in ('WECHAT_MAA_REPLY_ENABLED', 'WECHAT_MAA_CREATION_ENABLED'):
        monkeypatch.setattr(maa.settings, name, True)
    monkeypatch.setattr(maa.settings, 'WEMAA_ADMIN_USER_ID', 1)
    monkeypatch.setattr(schedules, 'target', lambda cid: {'title': 'same name', 'avatar_asset': 'a' * 64})
    monkeypatch.setattr(IdentityRegistry, 'resolve', lambda self, cid: {'wechat_id': 'exact_id'})
    monkeypatch.setattr(media, 'owner_asset', lambda db, user, aid: SimpleNamespace(
        id=aid, sha256='f' * 64, type='image', size=10, mime='image/png', name='fixture.png'))
    with Session(engine) as db:
        yield db


def create(db, **kwargs):
    args = dict(key='admin:' + str(uuid.uuid4()), cid='b' * 64, user_id=1, asset_id=11,
                receive_only_wechat_id='exact_id')
    args.update(kwargs)
    return media.add_delivery(db, **args)


@pytest.mark.parametrize('override', [
    {'receive_only_wechat_id': 'same_name_wrong_id'}, {'user_id': 2},
    {'key': 'core:' + str(uuid.uuid4())}, {'asset_id': None},
])
def test_consent_cannot_change_identity_authority_or_message_kind(context, override):
    with pytest.raises(BizError):
        create(context, **override)
    assert context.query(WechatDelivery).count() == 0


def test_consent_is_exact_immutable_and_preserved_in_final_receipt(context):
    row = create(context)
    consent = row.detail['operator_consent']
    assert (consent['delivery_id'], consent['conversation_id'], consent['asset_id'], consent['sha256']) == (
        row.id, row.conversation_id, 11, 'f' * 64)
    assert consent['expires_at'] == row.created_at + 600
    assert media.delivery_payload(context, row)['operator_consent'] == consent
    assert create(context, key=row.request_key).id == row.id
    with pytest.raises(BizError):
        create(context, key=row.request_key, receive_only_wechat_id=None)
    row.status = 'sending'; row.sha256 = 'f' * 64; context.flush()
    receipt = {'status': 'sent', 'evidence': {'title_verified': True,
               'outgoing_verified': True, 'media_sha256': 'f' * 64},
               'operator_consent': {'wechat_id': 'forged'}}
    media.delivery_result(context, row.id, receipt)
    assert row.detail['operator_consent'] == consent
    assert 'operator_consent' not in media.delivery_payload(context, row)
    assert media.delivery_result(context, row.id, receipt)['status'] == 'sent'


def test_default_media_does_not_acquire_a_receive_only_override(context):
    row = create(context, receive_only_wechat_id=None)
    assert row.detail == {}
    assert 'operator_consent' not in media.delivery_payload(context, row)


@pytest.mark.parametrize('value', ['', True, 'bad id'])
def test_explicit_override_requires_a_wechat_id(context, value):
    with pytest.raises(BizError):
        maa.validate_command({'request_id': str(uuid.uuid4()), 'action': 'send_media',
            'params': {'conversation_id': 'b' * 64, 'asset_id': 11, 'receive_only_wechat_id': value}})


def test_claim_nonce_recovers_only_same_owner_before_terminal(context,monkeypatch):
    db=context
    row=create(db);row.sha256='f'*64;db.commit()
    first='a'*32
    assert media.claim_delivery(db,row.id,first)['permit']
    assert media.claim_delivery(db,row.id,first)['resumed']
    assert not media.claim_delivery(db,row.id,'b'*32)['permit']
    assert not media.claim_delivery(db,row.id)['permit']
    row.status='uncertain';db.commit()
    assert not media.claim_delivery(db,row.id,first)['permit']


@pytest.mark.parametrize('token',[1,'','x'*32,'a'*31])
def test_claim_rejects_invalid_nonce_without_state_change(context,token):
    row=create(context);row.sha256='f'*64;context.commit()
    with pytest.raises(BizError):media.claim_delivery(context,row.id,token)
    assert row.status=='ready'
