"""Run real Maa recognition against captured fixtures; never touches Windows UI."""
import argparse
import json
from pathlib import Path
from maa.controller import CustomController
import numpy as np
from PIL import Image
from maa.resource import Resource
from maa.tasker import Tasker
from maa.toolkit import Toolkit
from list_rows import ListRows,VerifyRecentRow
from runner import plain,Runtime
from types import SimpleNamespace

root=Path(__file__).resolve().parent
parser=argparse.ArgumentParser()
parser.add_argument('image')
parser.add_argument('--kind',default='contacts')
parser.add_argument('--against')
parser.add_argument('--expected',type=int,default=396)
args=parser.parse_args()
Toolkit.init_option(str(root/'data'/'offline'))
class FixtureController(CustomController):
    def __init__(self,path):
        self.image=np.array(Image.open(path).convert('RGB'))[:,:,::-1].copy()
        super().__init__()
    def connect(self): return True
    def request_uuid(self): return 'offline-fixture-no-input'
    def screencap(self): return self.image.copy()
    def reject(self,*args): return False
    start_app=stop_app=click=swipe=touch_down=touch_move=touch_up=click_key=input_text=key_down=key_up=reject

controller=FixtureController(args.image)
controller.set_screenshot_use_raw_size(True)
assert controller.post_connection().wait().succeeded
resource=Resource()
resource.register_custom_recognition('ListRows',ListRows())
resource.register_custom_recognition('VerifyRecentRow',VerifyRecentRow())
from categories import ContactCategory,arrow_state
resource.register_custom_recognition('ContactCategory',ContactCategory())
assert resource.post_bundle(str(root/'resource')).wait().succeeded
tasker=Tasker()
tasker.bind(resource,controller)
results={}
for name in ('ListRows','UnreadBadges','ListAtTop'):
    job=tasker.post_task(name,{'ListRows':{'custom_recognition_param':{'kind':args.kind}}}).wait()
    results[name]={'succeeded':job.succeeded,'detail':plain(job.get())}
target=root/'data'/('offline-'+args.kind+'.json')
target.write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
summary={}
for name,result in results.items():
    summary[name]=[n.get('recognition',{}).get('filtered_results') for n in result['detail']['nodes'] if n.get('recognition')]
print(json.dumps(summary,ensure_ascii=False,indent=2))
print('TOP_ROI_COLORS='+json.dumps(np.unique(controller.image[82:90,470:479,:3].reshape(-1,3),axis=0).tolist()))
if args.against:
    from flows import contact_rows
    from contact_alignment import overlap,photo_match,stationary
    holder=SimpleNamespace(tasker=tasker,pipeline=json.loads((root/'resource'/'pipeline'/'navigation.json').read_text(encoding='utf-8')))
    holder.recognize_image=lambda *a,**kw:Runtime.recognize_image(holder,*a,**kw)
    previous=contact_rows(holder,controller.image,args.expected)
    other=np.array(Image.open(args.against).convert('RGB'))[:,:,::-1].copy()
    current=contact_rows(holder,other,args.expected)
    try: aligned=len(current) if stationary(previous,current) else overlap(previous,current)
    except Exception as exc: aligned=str(exc)
    print(json.dumps({'previous':[(r['y'],r['name']) for r in previous],'current':[(r['y'],r['name']) for r in current],'overlap':aligned,'matches':[[int(photo_match(a['photo'],b['photo'])) for b in current] for a in previous]},ensure_ascii=True))
if args.kind=='categories':
    from maa.pipeline import JCustomRecognition
    for category,want in [('公众号、服务号','collapsed'),('企业微信联系人','collapsed'),('联系人','expanded')]:
        job=tasker.post_recognition('Custom',JCustomRecognition(custom_recognition='ContactCategory',custom_recognition_param={'category':category,'want':want}),controller.image).wait()
        result=plain(job.get())['nodes'][0]['recognition']
        print(json.dumps({'category':category,'result':result},ensure_ascii=True))
        assert result['hit'], 'Category header/state must match'
if args.kind=='recent':
    from flows import named_avatar_rows
    from maa.pipeline import JCustomRecognition
    import hashlib
    holder=SimpleNamespace(tasker=tasker,pipeline=json.loads((root/'resource'/'pipeline'/'navigation.json').read_text(encoding='utf-8')))
    items=Runtime.recognize_image(holder,'ReadRecentList',controller.image)
    hits=Runtime.recognize_image(holder,'ListRows',controller.image,'recent')
    centers=hits[0]['detail']['centers']
    rows=named_avatar_rows(centers,items,'recent')
    first=rows[0]
    y=first['y']
    sha=hashlib.sha256(controller.image[y-13:y+13,78:102,:3].tobytes()).hexdigest()
    job=tasker.post_recognition('Custom',JCustomRecognition(custom_recognition='VerifyRecentRow',custom_recognition_param={'y':y,'name':first['name'],'avatar_sha':sha}),controller.image).wait()
    verified=plain(job.get())['nodes'][0]['recognition']['hit']
    print(json.dumps({'snapshot_ocr_items':len(items),'named_rows':len(rows),'row_guard_hit':verified},ensure_ascii=False))
    assert verified, 'Correct row must pass'
    for wrong in ({'name':'definitely-not-this-contact'}, {'avatar_sha':'0'*64}):
        params={'y':y,'name':first['name'],'avatar_sha':sha,**wrong}
        result=tasker.post_recognition('Custom',JCustomRecognition(custom_recognition='VerifyRecentRow',custom_recognition_param=params),controller.image).wait()
        assert not plain(result.get())['nodes'][0]['recognition']['hit'], 'Wrong identity must fail closed'
    print('ROW_GUARD_NEGATIVE_CASES=2 PASSED')
