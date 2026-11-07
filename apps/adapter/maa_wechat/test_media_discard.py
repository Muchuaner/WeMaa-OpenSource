import hashlib,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
from PIL import Image
from media_send import discard_failed_preview

class DiscardTests(unittest.TestCase):
 def test_only_unchanged_owned_unsent_preview_can_be_removed(self):
  for change in ('none','border','pixels','clicked','extra_draft'):
   with self.subTest(change=change),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);did='a'*32;cid='b'*64;folder=root/'data/media-cache'/did;folder.mkdir(parents=True)
    source=folder/'1.mp4';source.write_bytes(b'test');sha=hashlib.sha256(source.read_bytes()).hexdigest()
    saved=np.full((1040,1920,3),249,np.uint8);saved[895:897,1000:1700]=235;saved[908:968,504:730]=238;saved[920:930,520:530]=0
    path=root/'prepared.png';Image.fromarray(saved[:,:,::-1]).save(path)
    proof={'reason':'MEDIA_EXACT_PREVIEW_NOT_VERIFIED','diagnostic':{'send_click_attempted':change=='clicked','import':{'file_sha256':sha},'frames':[{'stage':'prepared','path':str(path),'sha256':hashlib.sha256(saved.tobytes()).hexdigest()}]}}
    item={'conversation_id':cid,'type':'video','path':str(source),'sha256':sha}
    row=('send_delivery','failed',json.dumps(item),json.dumps(proof))
    store=SimpleNamespace(db=SimpleNamespace(execute=lambda *a:SimpleNamespace(fetchone=lambda:row)),canonical=lambda c:c)
    current=saved.copy()
    if change=='border':current[908,504]+=1
    if change=='pixels':current[920,520]=255
    if change=='extra_draft':current[940:950,850:860]=0
    nodes=[];runtime=SimpleNamespace(capture=lambda label:(current,'after.png'),node=lambda name:nodes.append(name))
    with patch('send._target',return_value={'verified':True}),patch('send._draft',return_value={'verified':True}):
     if change in ('none','border'):self.assertEqual(discard_failed_preview(runtime,root,store,cid,did,{},'test')['status'],'reviewed')
     else:
      with self.assertRaises(ValueError):discard_failed_preview(runtime,root,store,cid,did,{},'test')
      self.assertEqual(nodes,[])
    self.assertNotIn('SendDraft',nodes)
