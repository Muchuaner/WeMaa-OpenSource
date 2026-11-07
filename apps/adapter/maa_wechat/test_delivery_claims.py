import tempfile,time,json,unittest,urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,Mock
from store import Store
from delivery_claims import DeliveryClaims
from creation_client import CreationClient

class ClaimRecovery(unittest.TestCase):
 def test_restart_retains_nonce_and_gui_start_is_permanent(self):
  with tempfile.TemporaryDirectory() as d:
   s=Store(Path(d)/'db');c=DeliveryClaims(s);token=c.token('a')
   self.assertEqual(DeliveryClaims(s).token('a'),token)
   c.begin_gui('a')
   with self.assertRaisesRegex(ValueError,'GUI_ALREADY_STARTED'):DeliveryClaims(s).token('a')
   with self.assertRaises(ValueError):c.begin_gui('a')
   s.db.close()

 def test_committed_claim_response_lost_has_no_gui_effect_and_same_nonce_retry(self):
  with tempfile.TemporaryDirectory() as d:
   s=Store(Path(d)/'db');cid='b'*64;did='a'*32
   s.queue_gui({'id':did,'action':'send_delivery','params':{'id':did,'conversation_id':cid,'text':'free test'},'expires_at':time.time()+300})
   s.route(cid,{'name':'Demo User','avatar_sha':'f'*64,'conversation_title':'Demo User','row_y':243})
   c=CreationClient.__new__(CreationClient);c.store=s;c.root=Path(d);c.next_event={};c.turns=None
   calls=[]
   def api(path,body,method):
    calls.append(body['claim_token'])
    if len(calls)==1:raise TimeoutError('response lost after cloud commit')
    return {'permit':True,'resumed':True}
   c.api=api;c.receipt=Mock()
   with patch('private_baseline.receive_only',return_value=False),patch('binding_contacts.process_binding_contact',return_value=False),patch('group_policy.allowed',return_value=True),patch('manual_gui.find_recent',return_value=({'name':'Demo User'},0)),patch('manual_gui.observe',return_value=(cid,{'name':'Demo User'})),patch('send.send_verified',return_value={'status':'sent','evidence':{'title_verified':True,'outgoing_verified':True}}) as gui:
    self.assertTrue(c.process_one(None,'test'));gui.assert_not_called();c.receipt.assert_not_called()
    self.assertEqual(s.db.execute('SELECT status FROM gui_commands').fetchone()[0],'pending')
    self.assertFalse(c.process_one(None,'test'));self.assertEqual(len(calls),1)
    c.next_event.clear();self.assertTrue(c.process_one(None,'test'))
    self.assertEqual(calls[0],calls[1]);gui.assert_called_once();self.assertEqual(c.receipt.call_count,1)
    self.assertEqual(s.db.execute('SELECT phase FROM delivery_claims').fetchone()[0],'gui_started')
   s.db.close()
