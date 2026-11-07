"""Same-frame profile identity and cleanup tests; all GUI input is mocked."""
import copy
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image

from sender_identity import verify_member
from sender_directory import SenderDirectory
from store import Store


class IdentityRuntime:
    def __init__(self, image, *, captures=None, first_id='demo_wechat', action_success=True):
        self.image=image
        self.captures=list(captures or [])
        self.first_id=first_id
        self.action_success=action_success
        self.actions=[]
        self.capture_labels=[]

    def capture(self, label):
        self.capture_labels.append(label)
        image=self.captures.pop(0) if self.captures else self.image.copy()
        return image,'mock-main-frame.png'

    def node(self, name, override=None, **kwargs):
        self.actions.append((name,override,kwargs))
        return {'succeeded':self.action_success}

    def recognize_image(self, name, image, **kwargs):
        return [{'text':'微信号：'+self.first_id,'score':.99,'box':[10,10,150,20]}]


class SenderIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='maa-profile-identity-test-')
        self.image=np.full((1040,1920,3),250,dtype=np.uint8)
        self.image[179:215,500:536]=(15,80,170)
        self.image[200:236,545:608]=(237,237,237)
        avatar=np.ascontiguousarray(self.image[179:215,500:536,::-1])
        self.avatar_sha=hashlib.sha256(avatar.tobytes()).hexdigest()
        self.payload={'direction':'incoming','message_kind':'text','reply_safe':True,
                      'group_sender_name':'Demo User','group_sender_verified':True,
                      'visual':{'box':[545,200,63,36],'verified':True,'sha256':'b'*64,
                                'avatar_box':[500,179,36,36],'avatar_verified':True,
                                'avatar_sha256':self.avatar_sha,'avatar_color_order':'RGB'}}
        self.target={'name':'开发组','conversation_title':'开发组(4)','avatar_sha':'c'*64}
        profile=Image.new('RGB',(400,400),(20,60,150))
        self.profile_path=Path(self.temp.name)/'profile.png'
        profile.save(self.profile_path)
        self.card={'hwnd':901,'class':'Qt51514QWindowToolSaveBits',
                   'shape':[400,400,3],'path':str(self.profile_path)}
        self.second=[{'text':'微信号：demo_wechat','score':.99,'box':[10,10,150,20]}]

    def tearDown(self):
        self.temp.cleanup()

    def verify(self, runtime=None, payload=None, *, windows=None, second=None, target_results=None, expected=None):
        runtime=runtime or IdentityRuntime(self.image)
        with patch('sender_identity._target',side_effect=target_results or [{'verified':True},{'verified':True}]), \
             patch('sender_identity._ocr_scaled',return_value=self.second if second is None else second), \
             patch('media_send.owned_windows',side_effect=windows if windows is not None else [[],[self.card],[]]), \
             patch('sender_identity.time.sleep'):
            result=verify_member(runtime,self.image,'fixture',self.target,payload or self.payload,expected_wx=expected)
        return result,runtime

    def test_exact_avatar_click_rgb_hash_and_group_display_name_are_bound(self):
        proof,runtime=self.verify()
        self.assertEqual(proof['wechat_id'],'demo_wechat')
        self.assertEqual(proof['display_name'],'Demo User')
        self.assertEqual(proof['message_avatar_sha256'],self.avatar_sha)
        self.assertEqual(proof['message_avatar_box'],[500,179,36,36])
        self.assertEqual(proof['profile_sha256'],hashlib.sha256(
            np.asarray(Image.open(self.profile_path).convert('RGB'))[:,:,::-1].tobytes()).hexdigest())
        self.assertEqual([a[0] for a in runtime.actions],['OpenMessageProfile','DismissProfile'])
        self.assertEqual(runtime.actions[0][1],{'OpenMessageProfile':{'target':[518,197,1,1]}})

    def test_missing_unverified_malformed_or_offscreen_avatar_never_clicks(self):
        for changes in ({'avatar_verified':False},{'avatar_box':None},
                        {'avatar_box':(500,179,36,36)},{'avatar_box':[500,179,36]},
                        {'avatar_box':[500.0,179,36,36]},{'avatar_box':[500,179,1,36]},
                        {'avatar_box':[500,880,36,36]}):
            with self.subTest(changes=changes):
                payload=copy.deepcopy(self.payload);payload['visual'].update(changes)
                runtime=IdentityRuntime(self.image)
                with self.assertRaisesRegex(ValueError,'MESSAGE_AVATAR_NOT_'):
                    self.verify(runtime,payload)
                self.assertEqual(runtime.actions,[])
                self.assertEqual(runtime.capture_labels,[])

    def test_avatar_hash_mismatch_rejects_before_any_capture_or_input(self):
        payload=copy.deepcopy(self.payload)
        payload['visual']['avatar_sha256']=hashlib.sha256(
            self.image[179:215,500:536].tobytes()).hexdigest()  # BGR is not the declared RGB evidence.
        runtime=IdentityRuntime(self.image)
        with self.assertRaisesRegex(ValueError,'MESSAGE_AVATAR_HASH_MISMATCH'):
            self.verify(runtime,payload)
        self.assertEqual(runtime.actions,[])
        self.assertEqual(runtime.capture_labels,[])

    def test_bubble_or_avatar_movement_prevents_profile_click(self):
        for y,x in ((204,548),(185,506)):
            moved=self.image.copy();moved[y,x,0]-=1
            runtime=IdentityRuntime(self.image,captures=[moved])
            with self.assertRaisesRegex(ValueError,'MESSAGE_MOVED_BEFORE_PROFILE'):
                self.verify(runtime)
            self.assertEqual(runtime.actions,[])

    def test_existing_owned_popup_is_never_dismissed_or_clicked_through(self):
        runtime=IdentityRuntime(self.image)
        with self.assertRaisesRegex(ValueError,'SENDER_EXISTING_POPUP'):
            self.verify(runtime,windows=[[self.card]])
        self.assertEqual(runtime.actions,[])

    def test_failed_click_with_observed_profile_is_not_clicked_again(self):
        proof,runtime=self.verify(IdentityRuntime(self.image,action_success=False))
        self.assertEqual(proof['wechat_id'],'demo_wechat')
        self.assertEqual(sum(a[0]=='OpenMessageProfile' for a in runtime.actions),1)
        self.assertEqual(sum(a[0]=='DismissProfile' for a in runtime.actions),1)

    def test_failed_click_without_card_never_retries_or_guesses_cleanup_target(self):
        runtime=IdentityRuntime(self.image,action_success=False)
        with self.assertRaisesRegex(ValueError,'SENDER_PROFILE_CLICK_FAILED'):
            self.verify(runtime,windows=[[]]+[[]]*13)
        self.assertEqual([a[0] for a in runtime.actions],['OpenMessageProfile'])

    def test_conflicting_missing_or_ambiguous_profile_id_still_restores_chat(self):
        for second in ([{'text':'微信号：different_id'}],[],
                       [{'text':'微信号：demo_wechat 微信号：another_user'}]):
            runtime=IdentityRuntime(self.image)
            with self.subTest(second=second),self.assertRaisesRegex(ValueError,'SENDER_PROFILE_ID_NOT_STABLE'):
                self.verify(runtime,second=second)
            self.assertEqual([a[0] for a in runtime.actions],['OpenMessageProfile','DismissProfile'])
            self.assertEqual(runtime.capture_labels,['fixture-identity-anchor','fixture-identity-restored'])

    def test_expected_id_mismatch_still_dismisses_profile(self):
        runtime=IdentityRuntime(self.image)
        with self.assertRaisesRegex(ValueError,'SENDER_PROFILE_ID_NOT_STABLE'):
            self.verify(runtime,expected='another_user')
        self.assertEqual(runtime.actions[-1][0],'DismissProfile')

    def test_profile_parse_failure_still_dismisses_and_checks_restoration(self):
        self.profile_path.unlink()
        runtime=IdentityRuntime(self.image)
        with self.assertRaises(FileNotFoundError):
            self.verify(runtime)
        self.assertEqual(runtime.actions[-1][0],'DismissProfile')
        self.assertEqual(runtime.capture_labels[-1],'fixture-identity-restored')

    def test_profile_remaining_after_dismissal_is_fatal_without_returning_proof(self):
        runtime=IdentityRuntime(self.image)
        with self.assertRaisesRegex(ValueError,'SENDER_PROFILE_NOT_DISMISSED'):
            self.verify(runtime,windows=[[],[self.card],[self.card]])
        self.assertEqual(runtime.actions[-1][0],'DismissProfile')
        self.assertEqual(runtime.capture_labels,['fixture-identity-anchor'])

    def test_another_owned_popup_after_profile_dismissal_prevents_identity_success(self):
        runtime=IdentityRuntime(self.image)
        other=dict(self.card,hwnd=902)
        with self.assertRaisesRegex(ValueError,'SENDER_PROFILE_(?:NOT_DISMISSED|WINDOW_NOT_UNIQUE|NOT_RESTORED)'):
            self.verify(runtime,windows=[[],[self.card],[other]])

    def test_changed_chat_or_message_after_profile_never_returns_identity_proof(self):
        runtime=IdentityRuntime(self.image)
        with self.assertRaisesRegex(ValueError,'SENDER_CHAT_NOT_RESTORED'):
            self.verify(runtime,target_results=[{'verified':True},{'verified':False}])
        moved=self.image.copy();moved[205,550,0]-=1
        runtime=IdentityRuntime(self.image,captures=[self.image.copy(),moved])
        with self.assertRaisesRegex(ValueError,'MESSAGE_MOVED_DURING_PROFILE_CHECK'):
            self.verify(runtime)

    def test_outgoing_local_account_uses_its_own_avatar_box_and_display_name(self):
        payload=copy.deepcopy(self.payload)
        payload.update(direction='outgoing',group_sender_name=None,group_sender_verified=False)
        proof,_=self.verify(payload=payload)
        self.assertEqual(proof['display_name'],'本机账号')

    def test_outgoing_profile_name_consensus_preserves_the_local_wechat_id(self):
        payload=copy.deepcopy(self.payload)
        payload.update(direction='outgoing',group_sender_name=None,group_sender_verified=False,
                       sender_name='本机账号')
        identity={'text':'微信号：demo_bot','score':.99,'box':[110,92,180,20]}
        first=[identity,{'text':'创作平台-AI','score':.99,'box':[108,42,80,20]},
               {'text':'地区：中国','score':.99,'box':[108,65,80,18]}]
        second=[identity,{'text':'创作平台-AI','score':.97,'box':[109,43,80,20]}]
        runtime=IdentityRuntime(self.image)
        runtime.recognize_image=Mock(return_value=first)
        proof,runtime=self.verify(runtime,payload,second=second,expected='demo_bot')
        self.assertEqual(proof['display_name'],'创作平台-AI')
        self.assertEqual(proof['wechat_id'],'demo_bot')
        self.assertTrue(proof['stable'])
        self.assertEqual([a[0] for a in runtime.actions],['OpenMessageProfile','DismissProfile'])

    def test_outgoing_profile_name_disagreement_falls_back_without_losing_verified_id(self):
        payload=copy.deepcopy(self.payload)
        payload.update(direction='outgoing',group_sender_name=None,group_sender_verified=False,
                       sender_name='本机账号')
        identity={'text':'微信号：demo_bot','score':.99,'box':[110,92,180,20]}
        runtime=IdentityRuntime(self.image)
        runtime.recognize_image=Mock(return_value=[identity,
            {'text':'创作平台-AI','score':.99,'box':[108,42,80,20]}])
        proof,_=self.verify(runtime,payload,second=[identity,
            {'text':'创作平台机器人','score':.99,'box':[108,42,100,20]}])
        self.assertEqual(proof['display_name'],'本机账号')
        self.assertEqual(proof['wechat_id'],'demo_bot')
        self.assertTrue(proof['stable'])

    def test_outgoing_name_low_confidence_wrong_position_or_two_labels_cannot_confirm(self):
        payload=copy.deepcopy(self.payload)
        payload.update(direction='outgoing',group_sender_name=None,group_sender_verified=False)
        identity={'text':'微信号：demo_bot','score':.99,'box':[110,92,180,20]}
        cases=([{'text':'创作平台-AI','score':.7,'box':[108,42,80,20]}],
               [{'text':'创作平台-AI','score':.99,'box':[70,42,80,20]}],
               [{'text':'创作平台-AI','score':.99,'box':[108,90,80,20]}],
               [{'text':'创作平台-AI','score':.99,'box':[108,42,80,20]},
                {'text':'其他文本','score':.99,'box':[195,42,80,20]}])
        for labels in cases:
            with self.subTest(labels=labels):
                runtime=IdentityRuntime(self.image)
                runtime.recognize_image=Mock(return_value=[identity]+labels)
                proof,_=self.verify(runtime,payload,second=[identity]+copy.deepcopy(labels))
                self.assertEqual(proof['display_name'],'本机账号')
                self.assertEqual(proof['wechat_id'],'demo_bot')

    def resolve(self, runtime, *, windows=None, second=None):
        store=Store(Path(self.temp.name)/'directory.sqlite')
        try:
            directory=SenderDirectory(store)
            with patch('sender_identity._target',return_value={'verified':True}), \
                 patch('sender_identity._ocr_scaled',return_value=self.second if second is None else second), \
                 patch('media_send.owned_windows',side_effect=windows if windows is not None else [[],[self.card],[]]), \
                 patch('sender_identity.time.sleep'):
                return directory.resolve_visible(runtime,self.image,'group',[self.payload],self.target,'fixture')
        finally:
            store.db.close()

    def test_directory_allows_review_only_after_failed_id_read_restores_gui(self):
        runtime=IdentityRuntime(self.image)
        result=self.resolve(runtime,second=[{'text':'微信号：different_id'}])
        self.assertFalse(result[0]['sender_identity_verified'])
        self.assertNotIn('sender_wechat_id',result[0])
        self.assertEqual(runtime.actions[-1][0],'DismissProfile')
        self.assertEqual(runtime.capture_labels[-1],'fixture-'+result[0]['sender_identity_key'][:10]+'-identity-restored')

    def test_directory_propagates_dismiss_action_failure_to_stop_worker(self):
        class DismissFailure(IdentityRuntime):
            def node(self,name,override=None,**kwargs):
                result=super().node(name,override,**kwargs)
                if name=='DismissProfile':raise ValueError('NODE_FAILED:DismissProfile')
                return result
        runtime=DismissFailure(self.image)
        with self.assertRaises(ValueError):
            self.resolve(runtime)
        self.assertEqual(sum(a[0]=='OpenMessageProfile' for a in runtime.actions),1)

    def test_directory_propagates_restore_capture_failure_to_stop_worker(self):
        class RestoreFailure(IdentityRuntime):
            def capture(self,label):
                if label.endswith('-identity-restored'):raise ValueError('CAPTURE_FAILED')
                return super().capture(label)
        runtime=RestoreFailure(self.image)
        with self.assertRaises(ValueError):
            self.resolve(runtime)

    def test_directory_stops_on_two_observed_cards_even_when_click_reports_failure(self):
        runtime=IdentityRuntime(self.image,action_success=False)
        card2=dict(self.card,hwnd=902)
        with self.assertRaisesRegex(ValueError,'SENDER_PROFILE_WINDOW_NOT_UNIQUE'):
            self.resolve(runtime,windows=[[],[self.card,card2]])
        self.assertEqual([a[0] for a in runtime.actions],['OpenMessageProfile'])

    def test_directory_propagates_owned_window_read_failure_after_click(self):
        runtime=IdentityRuntime(self.image)
        with self.assertRaises(Exception):
            self.resolve(runtime,windows=[[],RuntimeError('NATIVE_POPUP_CAPTURE_FAILED')])
        self.assertEqual([a[0] for a in runtime.actions],['OpenMessageProfile'])

    def test_directory_propagates_node_exception_after_possible_profile_click(self):
        class ClickJournalFailure(IdentityRuntime):
            def node(self,name,override=None,**kwargs):
                result=super().node(name,override,**kwargs)
                if name=='OpenMessageProfile':raise RuntimeError('ACTIONS_JOURNAL_WRITE_FAILED')
                return result
        runtime=ClickJournalFailure(self.image)
        with self.assertRaises(Exception):
            self.resolve(runtime)
        self.assertEqual([a[0] for a in runtime.actions],['OpenMessageProfile'])

    def test_dispatch_stops_after_profile_cleanup_failure_before_next_reply(self):
        from watch import dispatch_incoming
        reasons=('SENDER_PROFILE_NOT_RESTORED','SENDER_PROFILE_NOT_DISMISSED',
                 'SENDER_CHAT_NOT_RESTORED','SENDER_PROFILE_WINDOW_NOT_UNIQUE')
        for index,reason in enumerate(reasons):
            store=Store(Path(self.temp.name)/('dispatch-'+str(index)+'.sqlite'))
            client=SimpleNamespace(queue=Mock())
            creation=SimpleNamespace(queue_event=Mock())
            experiment=SimpleNamespace(applies_to=Mock(return_value=False),queue_incoming=Mock())
            first={'event_id':'e'*64,'conversation_id':'c'*64,'text':'/菜单','observed_at':100}
            second=dict(first,event_id='f'*64,text='你好')
            try:
                with self.subTest(reason=reason),patch('sender_identity.verify_member',side_effect=ValueError(reason)), \
                     self.assertRaisesRegex(ValueError,reason):
                    dispatch_incoming([(0,first),(1,second)],store,client,creation,experiment,
                                      None,None,[self.payload,self.payload],1,self.target,'fixture')
                creation.queue_event.assert_not_called()
                client.queue.assert_not_called()
                self.assertEqual(store.db.execute('SELECT status,reason FROM inbound_dispatch').fetchall(),
                                 [('review',reason)])
            finally:
                store.db.close()


if __name__=='__main__':
    unittest.main()
