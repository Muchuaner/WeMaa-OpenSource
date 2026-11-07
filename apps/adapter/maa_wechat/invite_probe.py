"""Bounded invitation observation and duplicate acceptance verification."""
import argparse,hashlib,json,time,traceback
from runner import Runtime,ROOT
from store import Store
from watch import read_rows,select,read_title
from group_invites import initialize,policy,joined_evidence,card_candidates,process_card
from flows import scroll_top,box
p=argparse.ArgumentParser();p.add_argument('--step',choices=['inspect','verify','duplicate'],required=True);p.add_argument('--id',required=True);a=p.parse_args()
try:
 r=Runtime();r.run_id=a.id;s=Store(ROOT/'data'/'wechat.sqlite3');initialize(s);result={'policy':policy(ROOT)}
 if a.step=='duplicate':
  r.node('OpenRecent');scroll_top(r,a.id);rows,_,_=read_rows(r,ROOT,a.id+'-rows');choices=[v for v in rows if v['name']=='Tom']
  if len(choices)!=1 or not select(r,choices[0]):raise ValueError('TOM_NOT_VERIFIED')
  row=choices[0];image,path=r.capture(a.id+'-card');title=read_title(r,image,row);hits=r.recognize_image('ReadChatMessages',image);cards=card_candidates(hits)
  if len(cards)!=1:raise ValueError('INVITE_CARD_NOT_UNIQUE')
  y=box(cards[0])[1];key=hashlib.sha256((row['key']+'|').encode()+image[y-10:y+90,550:797,:3].tobytes()).hexdigest()
  prior=s.db.execute('SELECT status FROM group_invites WHERE id=?',(key,)).fetchone()
  if not prior or prior[0]!='accepted':raise ValueError('ACCEPTED_CARD_NOT_FOUND:'+key)
  def joins():
   count=0
   for line in (ROOT/'data'/'actions.jsonl').open(encoding='utf-8'):
    try:count+=json.loads(line).get('node')=='AcceptGroupInvite'
    except ValueError:pass
   return count
  before=joins();decisions=[process_card(r,ROOT,s,row,title,image,hits,a.id+'-duplicate-'+str(i)) for i in range(2)]
  after=joins()
  if decisions!=[False,False] or before!=after:raise ValueError('DUPLICATE_INVITE_REPLAYED')
  result['duplicate']={'invite_id':key,'prior_status':prior[0],'decisions':decisions,'join_clicks_before':before,'join_clicks_after':after}
 elif a.step=='verify':
  image,path=r.capture(a.id+'-verified-main');expected='创作平台AI-内测001'
  if not joined_evidence(r,image,expected):raise ValueError('JOINED_GROUP_NOT_VERIFIED')
  prior=s.db.execute('SELECT detail_json FROM group_invites WHERE id=?',('tom-invite-20260928-1556',)).fetchone()
  if not prior:raise ValueError('JOIN_ATTEMPT_NOT_RECORDED')
  proof=json.loads(prior[0]);proof.update(status='accepted',main_title=' '.join(h['text'] for h in r.recognize_image('ReadChatTitle',image)),main_frame=path,selected_row_verified=True,stable_header=True,verification_id=a.id)
  s.db.execute('UPDATE group_invites SET status=?,detail_json=?,updated=? WHERE id=?',('accepted',json.dumps(proof,ensure_ascii=False),time.time(),'tom-invite-20260928-1556'));s.db.commit();s.event('group_invite_verified',proof);result['verification']=proof
 image,path=r.capture(a.id+'-main');result['frame']=path
 if a.step=='inspect':result['ocr']=r.recognize_image('Read',image)
 s.db.close()
except Exception as e:result={'error':str(e),'traceback':traceback.format_exc()}
(ROOT/'data'/(a.id+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
