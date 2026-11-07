"""Sparse fixed greetings, original support replies, independent of XiaoZhou."""
import json,secrets,time
from datetime import datetime,timedelta
import redis
from wemaa.services import maa as maa,replies as replies,retired_experiment as experiment
KEY='wechat-maa:greetings:v1'
TEXTS=('你好，来打个招呼。','晚上好，祝你今天顺利。','嗨，祝你今天心情不错。','你好，祝你一天愉快。',
       '来问声好，祝你一切顺利。','嗨，愿今天有些开心的小事。','你好，祝你工作生活都顺心。','打个招呼，祝你心情轻松。',
       '嗨，愿你今天过得自在。','你好，祝你今天有所收获。','来问候一下，愿你一切安好。','嗨，祝你有个愉快的一天。',
       '你好，愿今天的事情都顺利。','打声招呼，祝你生活愉快。','嗨，祝你今天有个好心情。','问声好，愿你今天轻松顺心。')

def initial():return {'enabled':False,'validated':None,'next_at':None,'items':[],'used':[],'opt_out':[]}
def state():return json.loads(replies.client().get(KEY) or 'null') or initial()
def mutate(fn):
    with replies.client().pipeline() as p:
        for _ in range(8):
            try:
                p.watch(KEY);s=json.loads(p.get(KEY) or 'null') or initial();result=fn(s)
                p.multi();p.set(KEY,maa.dumps(s));p.execute();return result
            except redis.WatchError:continue
    raise maa.conflict('问候状态繁忙')

def next_time(now):
    t=datetime.fromtimestamp(now,experiment.TZ)
    candidate=now+secrets.randbelow(4501)+2700
    c=datetime.fromtimestamp(candidate,experiment.TZ)
    if c.hour>=22 or c.hour<8:
        day=t+timedelta(days=1) if t.hour>=8 else t
        candidate=day.replace(hour=8,minute=0,second=0,microsecond=0).timestamp()+secrets.randbelow(3600)
    return candidate

def overview():
    return state()|{'persona':'原客服人格','window':'08:00–22:00','interval':'随机45–120分钟','daily_limit':4,'contact_cooldown_days':7}

def control(body):
    if not isinstance(body,dict) or set(body)!={'enabled'} or type(body['enabled']) is not bool:raise maa.invalid('只接受enabled布尔值')
    def op(s):
        if body['enabled'] and not s['validated']:raise maa.conflict('先完成恢复后的实机客服收发验证')
        s['enabled']=body['enabled']
        if s['enabled']:s['next_at']=next_time(time.time())
        else:
            for i in s['items']:
                if i['status'] in ('pending','generating','ready'):i['status']='cancelled'
    mutate(op);return overview()

def accept(event):
    row=json.loads(replies.client().get(replies.event_key(event)) or 'null')
    if not row or row['status']!='sent' or row.get('experiment') or row.get('greeting'):raise maa.invalid('需要原客服已核验发送回执')
    if time.time()-row['updated_at']>1800:raise maa.invalid('收发验证已过期')
    mutate(lambda s:s.update(validated={'event_id':event,'checked_at':time.time()}));return overview()

def plan(now=None):
    now=time.time() if now is None else now;dt=datetime.fromtimestamp(now,experiment.TZ);date=dt.date().isoformat()
    def op(s):
        for i in s['items']:
            raw=replies.client().get(replies.event_key(i['id']));row=json.loads(raw or 'null')
            if row:i['status']=row['status']
            if i['status'] in ('pending','generating','ready') and now>=i['expires_at']:i['status']='expired'
        live=[i for i in s['items'] if i['status'] in ('pending','generating','ready','sending')]
        if s['enabled'] and not live and 8<=dt.hour<22 and now>=(s['next_at'] or now):
            today=[i for i in s['items'] if i['date']==date]
            choices=[t for t in TEXTS if t not in s['used'] and (not t.startswith('晚上') or dt.hour>=18)]
            if not choices:s['enabled']=False;s['reason']='greetings_exhausted'
            elif len(today)<4:
                rows=experiment.candidates(maa.latest());eligible=[cid for cid in rows if cid not in s['opt_out'] and not experiment.state()['members'].get(cid,{}).get('exited') and not any(i['contact_id']==cid and now-i['created_at']<7*86400 for i in s['items'])]
                if eligible:
                    cid=secrets.choice(eligible);text=secrets.choice(choices);end=dt.replace(hour=22,minute=0,second=0,microsecond=0).timestamp()
                    i={'id':secrets.token_hex(32),'date':date,'contact_id':cid,'name':rows[cid]['display_name'],'text':text,'created_at':now,'scheduled_at':now,'expires_at':min(end,now+600),'status':'pending'}
                    s['items'].append(i);s['used'].append(text);live=[i]
            s['next_at']=next_time(now)
        return {'enabled':s['enabled'],'id':'customer-greetings-v1','items':live if s['enabled'] else [],'testing_contact':None,'testing_name':None}
    return mutate(op)

def reason(row,s):
    meta=row.get('greeting')
    if not meta:return None
    i=next((x for x in s['items'] if x['id']==row['event_id']),None)
    if not s['enabled'] or not i:return 'GREETINGS_STOPPED'
    if i['status'] in ('cancelled','blocked','expired') or time.time()>=i['expires_at']:return 'GREETING_CANCELLED_OR_EXPIRED'
    if i.get('inbound_at',0)>=i['created_at']:return 'GREETING_NEW_INBOUND'
    return None

def inbound(cid,text):
    if not state()['items']:return
    def op(s):
        for i in s['items']:
            if i.get('conversation_id')!=cid:continue
            i['inbound_at']=time.time()
            if experiment.classify(text)=='exit' and i['contact_id'] not in s['opt_out']:s['opt_out'].append(i['contact_id'])
            if i['status'] in ('pending','generating','ready'):i['status']='cancelled'
    mutate(op)

async def prepare(body):
    from fastapi.concurrency import run_in_threadpool
    cid=experiment.validate_contact(body)
    normalized=replies.validate_prepare({k:v for k,v in body.items() if k not in ('contact_id','identity')})
    s=state();item=next((i for i in s['items'] if i['id']==normalized['event_id'] and i['contact_id']==cid),None)
    if not item or reason({'event_id':normalized['event_id'],'greeting':True},s):raise maa.conflict('问候任务已失效')
    if normalized['conversation_type']!='private':raise maa.invalid('问候仅发送个人好友')
    row,created=await run_in_threadpool(replies._begin,normalized)
    if not created:return replies.public_record(row,include_reply=True)
    def bind():
        def op(s):
            i=next(i for i in s['items'] if i['id']==row['event_id']);i['conversation_id']=normalized['conversation_id'];i['status']='generating'
        mutate(op)
        key=replies.event_key(row['event_id'])
        with replies.client().pipeline() as p:
            p.watch(key);r=json.loads(p.get(key));r['greeting']={'contact_id':cid};p.multi();replies._write(p,key,r);p.execute()
    await run_in_threadpool(bind)
    session=await run_in_threadpool(replies._load_session,normalized['conversation_id'])
    done=await run_in_threadpool(replies._finish,row,'ready',reply=item['text'],public_id=session[0] if session else None)
    return replies.public_record(done,include_reply=True)

def navigation(body):
    if not isinstance(body,dict) or set(body)!={'event_id','attempts','reason'} or type(body['attempts']) is not int or not 3<=body['attempts']<=20 or not replies.SAFE_REASON.fullmatch(str(body['reason'])):raise maa.invalid('导航失败报告无效')
    def op(s):
        i=next((i for i in s['items'] if i['id']==body['event_id']),None)
        if i and i['status']=='pending' and not replies.client().get(replies.event_key(i['id'])):i.update(status='blocked',reason=body['reason'])
    mutate(op);return {'accepted':True,'event_id':body['event_id']}
