import tempfile,time,json
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock,patch
import numpy as np
from binding_contacts import button,click_button,contact_action,process_binding_contact
from test_sender_identity import SenderIdentityTests,IdentityRuntime
from sender_identity import verify_member
from store import Store

class BindingContactTests(TestCase):
    def test_missing_low_confidence_or_duplicate_button_rejected(self):
        for hits in ([],[{'text':'发消息','score':.1,'box':[1,2,3,4]}],
                     [{'text':'发消息','score':1,'box':[1,2,3,4]}]*2):
            with self.assertRaises(ValueError):button(hits,{'发消息'})

    def test_wrong_profile_cannot_navigate(self):
        im=np.zeros((400,400,3),dtype=np.uint8)
        hits=[{'text':'发消息','score':1,'box':[100,200,80,25]}, {'text':'微信号：otherwx','score':1,'box':[100,100,80,25]}]
        runtime=SimpleNamespace(recognize_image=Mock(return_value=hits))
        popup=SimpleNamespace(capture=Mock(return_value=(im,'test')),node=Mock())
        with patch('send._ocr_scaled',return_value=hits),self.assertRaisesRegex(ValueError,'ID_CHANGED'):
            click_button(runtime,popup,{'发消息'},'test',expected_wx='mywx')
        popup.node.assert_not_called()

    def test_existing_friend_request_never_sent_twice(self):
        runtime=SimpleNamespace(recognize_image=Mock(return_value=[{'text':'添加到通讯录','score':1}]))
        profile=np.zeros((400,400,3),dtype=np.uint8)
        with patch('media_send.OwnedPopup'),patch('binding_contacts.click_button') as click:
            result=contact_action(runtime,Mock(),'a'*32,True,{},profile,{'wechat_id':'wx123'},'test')
        self.assertEqual(result['status'],'waiting_friend');click.assert_not_called()

    def test_waiting_friend_survives_restart_and_backoff(self):
        with tempfile.TemporaryDirectory() as folder:
            s=Store(Path(folder)/'test.sqlite');did='a'*32
            s.queue_gui({'id':did,'action':'send_delivery','params':{'binding_contact':{}},'expires_at':time.time()+500})
            # a real nonempty marker, then a waiting state preserved by a reload
            s.db.execute('UPDATE gui_commands SET params_json=? WHERE id=?',(json.dumps({'binding_contact':{'wechat_id':'wx123'}}),did));s.db.commit()
            s.gui_result(did,'waiting_friend',{'status':'waiting_friend'});s.db.close()
            s=Store(Path(folder)/'test.sqlite')
            client=SimpleNamespace(store=s,root=Path(folder),api=Mock())
            self.assertFalse(process_binding_contact(client,Mock(),'test'));client.api.assert_not_called();s.db.close()

class ProfileCallbackTests(SenderIdentityTests):
    def test_navigation_callback_returns_proof_without_wrong_chat_dismiss(self):
        runtime=IdentityRuntime(self.image);callback=Mock(return_value={'status':'private_opened','navigated':True})
        with patch('sender_identity._target',return_value={'verified':True}),patch('sender_identity._ocr_scaled',return_value=self.second),patch('media_send.owned_windows',side_effect=[[],[self.card]]):
            proof=verify_member(runtime,self.image,'test',self.target,self.payload,on_profile=callback)
        self.assertEqual(proof['contact_action']['status'],'private_opened')
        self.assertEqual([v[0] for v in runtime.actions],['OpenMessageProfile']);callback.assert_called_once()

    def test_callback_error_propagates_after_cleanup(self):
        runtime=IdentityRuntime(self.image)
        with patch('sender_identity._target',return_value={'verified':True}),patch('sender_identity._ocr_scaled',return_value=self.second),patch('media_send.owned_windows',side_effect=[[],[self.card],[]]),self.assertRaisesRegex(ValueError,'ACTION_FAILED'):
            verify_member(runtime,self.image,'test',self.target,self.payload,on_profile=Mock(side_effect=ValueError('ACTION_FAILED')))
        self.assertEqual([v[0] for v in runtime.actions],['OpenMessageProfile','DismissProfile'])
