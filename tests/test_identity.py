from copy import deepcopy

import pytest

from wemaa.identity import IdentityRegistry
from wemaa.services.replies import validate_prepare


def test_wal_reader_snapshot_does_not_block_identity_sync_commit(tmp_path):
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor
    registry=IdentityRegistry(tmp_path/'ids.sqlite3')
    registry.sync({'contacts':[contact()],'conversations':[conversation()]})
    reader=sqlite3.connect(registry.path)
    assert reader.execute('pragma journal_mode').fetchone()[0]=='wal'
    reader.execute('BEGIN')
    assert reader.execute('select name from identities').fetchone()[0]=='同名好友'
    changed=contact(name='新资料')
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(registry.sync,{'contacts':[changed],'conversations':[conversation()]})
        try:
            assert future.result(timeout=2)['identified']==1
            assert reader.execute('select name from identities').fetchone()[0]=='同名好友'
        finally:
            reader.rollback();reader.close()
    assert registry.resolve('c'*64)['name']=='新资料'


def test_registry_instances_share_writer_but_reads_remain_available(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    a=IdentityRegistry(tmp_path/'ids.sqlite3');b=IdentityRegistry(a.path)
    a.sync({'contacts':[contact()],'conversations':[conversation()]})
    entered=threading.Event()
    def bind():
        entered.set();b.bind_core(b.user_id('test_friend'),'person','session')
    with ThreadPoolExecutor(max_workers=1) as pool:
        with a.connection(write=True) as db:
            db.execute('update identities set seen=seen+1')
            f=pool.submit(bind);assert entered.wait(1)
            assert not f.done()
            assert b.summary()['identified']==1
        f.result(timeout=2)
    assert a.resolve('c'*64)['person_id']=='person'


def test_identity_admission_does_not_block_core_event_loop(monkeypatch):
    import asyncio,threading
    from types import SimpleNamespace
    from wemaa.runtime import Runtime
    from wemaa.services import maa
    release=threading.Event()
    def register(*_args,**_kw):
        release.wait(.5);return {'id':'user','name':'Demo User'}
    runtime=Runtime();runtime.registry=SimpleNamespace(register_sender=register,resolve=lambda _cid:{'id':'user','name':'Demo User'})
    monkeypatch.setattr(maa,'latest',lambda:{})
    monkeypatch.setattr(maa,'is_stale',lambda _body:False)
    async def run():
        tick=asyncio.Event();asyncio.get_running_loop().call_later(.02,tick.set)
        task=asyncio.create_task(runtime.admit_identity({'identity':{'wechat_id':'verified'},'conversation_id':'c'*64,'conversation_type':'private'}))
        try:
            await asyncio.wait_for(tick.wait(),.2)
            assert not task.done()
        finally:release.set()
        assert (await task)['user_id']=='user'
    asyncio.run(run())


def contact(wx='test_friend', name='同名好友'):
    return {'id':'wx:'+wx,'scope':'personal','wechat_id':wx,'display_name':name,
            'identity_status':'ocr_wechat_id','profile_asset':'a'*64,'avatar_asset':'b'*64}


def conversation(wx='test_friend', cid='c'*64):
    return {'id':cid,'aliases':['d'*64],'private_contact':{'contact_id':'wx:'+wx,
                'wechat_id':wx,'profile_verified':True}}


def test_identity_survives_rename_avatar_and_restart(tmp_path):
    registry=IdentityRegistry(tmp_path/'ids.sqlite3')
    row=contact()
    registry.sync({'contacts':[row],'conversations':[conversation()]})
    identity=registry.resolve('c'*64)
    registry.bind_core(identity['id'],'core-person','core-session')
    row.update(display_name='新备注',avatar_asset='e'*64)
    registry=IdentityRegistry(tmp_path/'ids.sqlite3')
    registry.sync({'contacts':[row],'conversations':[conversation()]})
    current=registry.resolve('d'*64)
    assert current['id']==identity['id']
    assert current['name']=='新备注'
    assert current['person_id']=='core-person'
    assert registry.route_for(current['id'])=='c'*64


def test_same_name_and_avatar_are_not_identity(tmp_path):
    registry=IdentityRegistry(tmp_path/'ids.sqlite3')
    registry.sync({'contacts':[contact('friend_1'),contact('friend_2')],
                   'conversations':[conversation('friend_1')]})
    assert registry.summary()=={'identified':2,'registered':0,'routable':1,'needs_review':0}
    assert len({r['id'] for r in registry.items()})==2
    with pytest.raises(ValueError,match='PRIVATE_ROUTE_NOT_VERIFIED'):
        registry.route_for(registry.user_id('friend_2'))


def test_stale_alias_cannot_change_friend(tmp_path):
    registry=IdentityRegistry(tmp_path/'ids.sqlite3')
    registry.sync({'contacts':[contact('friend_1')],'conversations':[conversation('friend_1')]})
    registry.sync({'contacts':[contact('friend_2')],'conversations':[conversation('friend_2')]})
    with pytest.raises(ValueError):registry.resolve('c'*64)
    assert registry.summary()['needs_review']==1


def test_bad_profile_and_malformed_route_do_not_gain_identity(tmp_path):
    registry=IdentityRegistry(tmp_path/'ids.sqlite3')
    bad=contact('friend_2');bad.pop('profile_asset')
    route=conversation();route['private_contact']['wechat_id']='broken id'
    registry.sync({'contacts':[contact(),bad],'conversations':[route]})
    assert registry.summary()['identified']==1
    assert registry.summary()['routable']==0
    assert registry.summary()['needs_review']==1


def test_account_isolation(tmp_path):
    a=IdentityRegistry(tmp_path/'ids.sqlite3',account='one')
    b=IdentityRegistry(tmp_path/'ids.sqlite3',account='two')
    assert a.user_id('same_wxid')!=b.user_id('same_wxid')
    a.sync({'contacts':[contact('same_wxid')],'conversations':[]})
    with pytest.raises(ValueError,match='PRIVATE_IDENTITY_NOT_VERIFIED'):
        b.identity_for(a.user_id('same_wxid'))


def test_contact_identity_does_not_require_recent_chat(tmp_path):
    registry=IdentityRegistry(tmp_path/'ids.sqlite3')
    registry.sync({'contacts':[contact('first'),contact('second')],'conversations':[]})
    row=registry.identity_for(registry.user_id('second'))
    assert row['wechat_id']=='second' and row['contact_id']=='wx:second'
    assert registry.summary()['routable']==0


def test_group_member_and_friend_share_proven_identity(tmp_path):
    registry=IdentityRegistry(tmp_path/'ids.sqlite3')
    registry.sync({'contacts':[contact()],'conversations':[]})
    proof={'wechat_id':'test_friend','display_name':'群内备注','profile_sha256':'a'*64,
           'message_frame_sha256':'b'*64,'message_avatar_sha256':'c'*64,'stable':True}
    sender=registry.register_sender(proof,'d'*64,private=False)
    assert sender['id']==registry.items()[0]['id']
    assert registry.summary()['routable']==0
    assert sender['name']=='同名好友'


def test_old_nickname_only_group_is_not_promoted(tmp_path):
    registry=IdentityRegistry(tmp_path/'ids.sqlite3')
    value=conversation();value['group_profile']={'kind':'group'}
    registry.sync({'contacts':[contact()],'conversations':[value]})
    assert registry.summary()['routable']==0
