"""Dispatch when the authenticated Maa agent polls, independent of browser/auto-reply."""
import json
import math
import time
import uuid
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import select
from wemaa.errors import BizError
from wemaa.models import WechatSchedule
from wemaa.services import maa as maa

GRACE_SECONDS = 300
GREETINGS = {
    'greeting': ['愿你今天心情明朗，一切顺利。', '新的一天，愿你有好心情，也有小惊喜。', '忙碌之余记得休息，祝你今天顺心。', '愿今天的你，遇见温暖，收获快乐。', '送上一份小小的问候，愿你平安喜乐。', '愿你今天的努力，都有值得期待的回响。'],
    'daily': ['每日一言：把眼前的小事做好，就是向前走了一步。', '每日一言：允许自己慢一点，但别忘了继续前行。', '每日一言：生活的美好，常常藏在平凡的小事里。', '每日一言：保持好奇，也给自己留一点从容。', '每日一言：认真生活的每一天，都有自己的意义。', '每日一言：今天也值得被温柔对待。'],
}

def recurrence_config(body):
    value=body.get('recurrence') or {}
    if not isinstance(value,dict):raise maa.invalid('循环设置无效')
    if not value:return {}
    if set(value)!={'weekdays','time'} or not isinstance(value['weekdays'],list) or not value['weekdays'] or any(type(v) is not int or not 0<=v<=6 for v in value['weekdays']) or len(set(value['weekdays']))!=len(value['weekdays']) or not isinstance(value['time'],str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',value['time']):
        raise maa.invalid('请选择周一至周日及有效的北京时间')
    return {'weekdays':sorted(value['weekdays']),'time':value['time']}

def next_occurrence(config, after):
    local=datetime.fromtimestamp(after,ZoneInfo('Asia/Shanghai'))
    hour,minute=map(int,config['time'].split(':'))
    for offset in range(8):
        value=(local+timedelta(days=offset)).replace(hour=hour,minute=minute,second=0,microsecond=0)
        if value.weekday() in config['weekdays'] and value.timestamp()>after:return value.timestamp()
    raise maa.invalid('循环时间无效')

def same_target(current, saved):
    if current.get('group_name'):
        if saved.get('group_id') and current.get('group_id')==saved['group_id']:return True
        name=lambda v:re.sub(r'[（(]\d{1,4}[）)]$','',re.sub(r'\s+','',v))
        return name(saved.get('group_name') or saved['title'])==name(current['group_name'])
    return current==saved

def finish_occurrence(row):
    if row.recurrence and row.status=='sent':
        history=list(row.detail.get('history',[]))
        history.append({'send_at':row.send_at,'messages':row.messages,'status':'sent','finished_at':time.time()})
        row.detail={'history':history[-30:],'last_status':'sent'}
        row.messages=[{k:v for k,v in m.items() if k in ('kind','text','asset_id','name','category')}|{'status':'pending'} for m in row.messages]
        row.send_at=next_occurrence(row.recurrence,max(time.time(),row.send_at))
        row.command_id=None;row.revision+=1;row.status='pending'

def delete(db,sid,revision):
    row=db.scalar(select(WechatSchedule).where(WechatSchedule.id==sid).with_for_update())
    if not row or row.deleted_at:raise maa.not_found('定时任务不存在')
    if revision!=row.revision or row.status in ('sending','dispatched') or (row.status=='pending' and claimed(row)):
        raise maa.conflict('任务正在执行或已修改，请刷新后再删除')
    row.deleted_at=time.time();row.revision+=1
    if row.status=='pending':row.status='cancelled'
    return row

def request_id(row):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'maa-schedule:' + row.id + ':' + str(row.revision)))

def claimed(row):
    cache=maa.client()
    legacy=cache.get(f'{maa.PREFIX}request:{row.requested_by}:{request_id(row)}')
    if legacy:return legacy
    for index in range(len(row.messages or [])):
        intent=str(uuid.uuid5(uuid.NAMESPACE_URL,'maa-schedule:'+row.id+':'+str(row.revision)+':'+str(index)))
        value=cache.get(f'{maa.PREFIX}request:{row.requested_by}:{intent}')
        if value:return value

def target(cid):
    record = maa.latest()
    if maa.is_stale(record):
        raise maa.conflict('Windows Maa 离线，请恢复在线后选择发送对象')
    row = next((r for r in record['telemetry'].get('conversations', []) if r['id'] == cid or cid in r.get('aliases',[])), None)
    if not row or row.get('manual_target_observed') is not True or not row.get('avatar_asset'):
        raise maa.invalid('请选择已核验目标路径和头像的聊天对象')
    result={'title': row['title'], 'avatar_asset': row['avatar_asset']}
    if row.get('group_profile',{}).get('kind')=='group':result.update(group_name=row['group_profile']['name'],group_id=row['id'])
    return result

def validate(body):
    if set(body) - {'conversation_id', 'text', 'send_at', 'revision', 'request_id', 'messages', 'recurrence'}:
        raise maa.invalid('未知定时任务字段')
    checked = maa.validate_command({'action': 'send_message', 'params': {
        'conversation_id': body.get('conversation_id'), 'text': body.get('text') if 'messages' not in body else '定时消息'}, 'request_id': str(uuid.uuid4())})
    config=recurrence_config(body)
    if config and body.get('send_at') is not None:raise maa.invalid('单次和循环发送只能选择一种')
    at = next_occurrence(config,time.time()+15) if config else body.get('send_at')
    if type(at) not in (float, int) or not math.isfinite(at) or not time.time() + 15 <= at <= time.time() + 366*86400:
        raise maa.invalid('请选择至少15秒后、一年内的发送时间')
    return checked['params'], float(at)

def save(db, body, user_id, sid=None):
    params, at = validate(body)
    config=recurrence_config(body)
    messages = validate_messages(db, body.get('messages'), user_id) if 'messages' in body else []
    if messages: params['text'] = next((m['text'] for m in messages if m['kind'] == 'text'), '定时媒体消息')
    proof = target(params['conversation_id'])
    if not sid:
        try: sid_create = uuid.UUID(body['request_id']).hex
        except (KeyError, ValueError, TypeError, AttributeError): raise maa.invalid('request_id 必须为 UUID')
        existing = db.get(WechatSchedule, sid_create)
        if existing:
            if existing.deleted_at or existing.requested_by != user_id or existing.conversation_id != params['conversation_id'] or existing.text != params['text'] or (not config and existing.send_at != at) or existing.recurrence != config or existing.messages != messages:
                raise maa.conflict('相同请求不可更改消息、对象或时间')
            return existing
    if sid:
        row = db.scalar(select(WechatSchedule).where(WechatSchedule.id == sid).with_for_update())
        if not row or row.deleted_at: raise maa.not_found('定时任务不存在')
        if row.status != 'pending' or row.send_at <= time.time() or claimed(row) or body.get('revision') != row.revision:
            raise maa.conflict('任务已到期、已执行或被修改，请刷新')
        row.revision += 1
    else:
        row = WechatSchedule(id=sid_create, requested_by=user_id)
        db.add(row)
    row.conversation_id = params['conversation_id']
    row.messages = messages
    row.recurrence = config
    row.text = params['text']
    row.send_at = at
    row.target = proof
    row.detail = {}
    db.flush()
    return row

def cancel(db, sid, revision):
    row = db.scalar(select(WechatSchedule).where(WechatSchedule.id == sid).with_for_update())
    if not row or row.deleted_at: raise maa.not_found('定时任务不存在')
    if row.status != 'pending' or row.send_at <= time.time() or claimed(row) or revision != row.revision:
        raise maa.conflict('任务已到期、已执行或被修改，不能取消')
    row.status = 'cancelled'
    row.messages = [{**m, 'status': 'cancelled'} for m in row.messages]
    row.revision += 1
    return row

def serialize(row):
    result = {k: getattr(row, k) for k in ('id','conversation_id','target','text','send_at','status','revision','command_id','detail','created_at','messages','recurrence')}
    if row.status == 'cancelled': result['messages'] = [{**m, 'status': 'cancelled'} for m in row.messages]
    if row.status == 'pending' and row.send_at + GRACE_SECONDS < time.time(): result['status'] = 'expired'
    if row.command_id and not row.messages and row.status not in ('sent','failed','uncertain','expired'):
        raw = maa.client().get(maa._command_key(row.command_id))
        command = json.loads(raw) if raw else None
        if command:
            detail = command.get('detail') or {}
            result['detail'] = detail
            if command['status'] == 'succeeded' and detail.get('status') == 'sent': result['status'] = 'sent'
            elif command['status'] in ('succeeded','failed'): result['status'] = 'uncertain' if detail.get('status') != 'failed' and detail.get('status') != 'rejected' else 'failed'
            elif command['status'] == 'expired' or (command['status'] == 'queued' and command['expires_at'] <= time.time()): result['status'] = 'expired'
            elif command['expires_at'] <= time.time(): result['status'] = 'uncertain'
            else: result['status'] = command['status']
        else: result['status'] = 'uncertain'
    return result

def sync_results(db):
    rows = db.scalars(select(WechatSchedule).where(WechatSchedule.status == 'dispatched').limit(200).with_for_update(skip_locked=True)).all()
    for row in rows:
        data = serialize(row)
        if data['status'] in ('sent', 'failed', 'uncertain', 'expired'):
            row.status = data['status']; row.detail = data['detail']
            finish_occurrence(row)
    db.commit()

def dispatch_due(db):
    sync_results(db)
    active = db.scalars(select(WechatSchedule).where(WechatSchedule.status == 'sending').order_by(WechatSchedule.send_at).limit(20).with_for_update(skip_locked=True)).all()
    for row in active:
        dispatch_batch(db, row)
        finish_occurrence(row)
    db.commit()
    # PostgreSQL locks serialize edits/cancel/dispatch across API processes.
    rows = db.scalars(select(WechatSchedule).where(WechatSchedule.status == 'pending', WechatSchedule.deleted_at.is_(None),
        WechatSchedule.send_at <= time.time()).order_by(WechatSchedule.send_at).limit(10)
        .with_for_update(skip_locked=True)).all()
    for row in rows:
        if row.messages:
            dispatch_batch(db, row)
            continue
        intent_id = request_id(row)
        # Recover the Redis enqueue/SQL commit gap using the same immutable intent.
        previous = claimed(row)
        if previous:
            row.command_id = previous
            row.status = 'dispatched'
            continue
        if time.time() > row.send_at + GRACE_SECONDS:
            row.status = 'expired'; row.detail = {'reason': '超过5分钟执行窗口'}
            continue
        try:
            if not same_target(target(row.conversation_id), row.target):
                row.status = 'failed'; row.detail = {'reason': '发送对象身份已变化，请重新选择'}
                continue
            command, _ = maa.enqueue({'action': 'send_message', 'params': {
                'conversation_id': row.conversation_id, 'text': row.text}, 'request_id': intent_id}, row.requested_by)
            row.command_id = command['id']; row.status = 'dispatched'; row.detail = {}
        except BizError as exc:
            row.detail = {'reason': str(exc.detail['message'])}
    db.commit()


def validate_messages(db, messages, user_id):
    from wemaa.services.media import owner_asset
    if not isinstance(messages, list) or not 1 <= len(messages) <= 20:
        raise maa.invalid('每个任务需包含1至20条消息')
    checked = []
    for item in messages:
        if not isinstance(item, dict) or item.get('kind') not in ('text', 'image', 'video', 'greeting'):
            raise maa.invalid('消息类型只能为文字、图片或视频')
        kind = item['kind']
        if kind == 'greeting':
            if set(item)!={'kind','category'} or item['category'] not in GREETINGS:raise maa.invalid('请选择随机问候或每日一言')
            checked.append({'kind':kind,'category':item['category'],'status':'pending'})
        elif kind == 'text':
            if set(item) != {'kind', 'text'}: raise maa.invalid('文字消息字段无效')
            maa.validate_command({'action':'send_message','params':{'conversation_id':'a'*64,'text':item['text']},'request_id':str(uuid.uuid4())})
            checked.append({'kind':kind,'text':item['text'],'status':'pending'})
        else:
            if set(item) != {'kind', 'asset_id'} or type(item['asset_id']) is not int: raise maa.invalid('媒体素材无效')
            if not maa.settings.WECHAT_MAA_CREATION_ENABLED: raise maa.invalid('微信媒体发送未启用')
            asset = owner_asset(db, user_id, item['asset_id'])
            if asset.type != kind: raise maa.invalid('素材类型与消息类型不一致')
            if kind == 'video' and asset.mime != 'video/mp4': raise maa.invalid('视频仅支持MP4')
            if kind == 'image' and asset.mime not in ('image/png','image/jpeg','image/webp'): raise maa.invalid('图片仅支持PNG、JPEG、WebP')
            checked.append({'kind':kind,'asset_id':asset.id,'name':asset.name,'status':'pending'})
    return checked


def dispatch_batch(db, row):
    from wemaa.models import WechatDelivery
    from wemaa.services.media import add_delivery, owner_asset
    history=row.detail.get('history',[])
    messages = [dict(m) for m in row.messages]
    now = time.time()
    for index, item in enumerate(messages):
        if item['status'] == 'sent': continue
        if item.get('command_id'):
            raw = maa.client().get(maa._command_key(item['command_id']))
            cmd = json.loads(raw) if raw else None
            if cmd:
                detail = cmd.get('detail') or {}
                if cmd['status'] == 'succeeded' and detail.get('status') == 'sent': item['status'] = 'sent'
                elif cmd['status'] in ('succeeded','failed'): item['status'] = 'failed' if detail.get('status') in ('failed','rejected') else 'uncertain'
                elif cmd['status'] == 'expired' or (cmd['status'] == 'queued' and cmd['expires_at'] <= now): item['status'] = 'expired'
                elif cmd['expires_at'] <= now: item['status'] = 'uncertain'
                else: item['status'] = cmd['status']
                item['detail'] = detail
            else: item['status'] = 'uncertain'
        elif item.get('delivery_id'):
            delivery = db.scalar(select(WechatDelivery).where(WechatDelivery.id == item['delivery_id']).with_for_update())
            if not delivery:
                from wemaa.plugins import website
                prior=website.legacy_delivery(db,item['delivery_id'])
                if not prior:
                    item['status']='uncertain'
                else:
                    item['status']={'ready':'queued','sending':'issued','cancelled':'failed'}.get(prior['status'],prior['status'])
                    item['detail']=prior['detail']
                    if now > item['dispatched_at'] + 300:
                        if prior['status']=='ready':
                            item['status']='expired' if website.expire_legacy_delivery(db,item['delivery_id']) else 'uncertain'
                        elif prior['status']=='sending':
                            item['status']='uncertain'
            else:
                item['status'] = {'ready':'queued','sending':'issued','cancelled':'failed'}.get(delivery.status, delivery.status)
                item['detail'] = delivery.detail
                if now > item['dispatched_at'] + 300 and delivery.status == 'ready':
                    delivery.status = 'cancelled'; item['status'] = 'expired'
                elif now > item['dispatched_at'] + 300 and delivery.status == 'sending': item['status'] = 'uncertain'
        if item['status'] == 'sent':
            item.setdefault('finished_at', now)
            continue
        if item['status'] in ('failed','expired','uncertain'):
            row.status = item['status']; row.detail = {'reason': '第'+str(index+1)+'条未确认成功，后续消息已停止', 'sent_count': index, 'total': len(messages)}
            for rest in messages[index+1:]: rest['status'] = 'skipped'
            break
        if item.get('command_id') or item.get('delivery_id'):
            row.status = 'sending'
            break
        start = messages[index-1].get('finished_at', row.send_at) if index else row.send_at
        if now > start + GRACE_SECONDS:
            item['status'] = 'expired'; row.status = 'expired'; row.detail = {'reason':'等待派发超过5分钟，后续消息停止'}
            for rest in messages[index+1:]: rest['status'] = 'skipped'
            break
        intent = str(uuid.uuid5(uuid.NAMESPACE_URL, 'maa-schedule:'+row.id+':'+str(row.revision)+':'+str(index)))
        previous = maa.client().get(f'{maa.PREFIX}request:{row.requested_by}:{intent}')
        if previous:
            item.update(command_id=previous, status='queued', dispatched_at=now); row.status='sending'
            break
        try:
            if not same_target(target(row.conversation_id), row.target):
                item['status']='failed'; row.status='failed'; row.detail={'reason':'发送对象身份已变化'}
                for rest in messages[index+1:]: rest['status']='skipped'
                break
            if item['kind'] in ('text','greeting'):
                if item['kind']=='greeting':
                    # Stable for this occurrence, including a Redis/SQL commit gap.
                    import hashlib
                    pool=GREETINGS[item['category']]
                    item['resolved_text']=pool[int(hashlib.sha256(intent.encode()).hexdigest(),16)%len(pool)]
                cmd, _ = maa.enqueue({'action':'send_message','params':{'conversation_id':row.conversation_id,'text':item.get('resolved_text') or item['text']},'request_id':intent}, row.requested_by)
                item['command_id'] = cmd['id']
            else:
                record = maa.latest(); runtime = record['telemetry']['runtime']
                if runtime.get('worker_state') != 'Running' or runtime.get('progress',{}).get('manual_commands_enabled') is not True:
                    raise maa.conflict('Maa任务未就绪')
                asset=owner_asset(db,row.requested_by,item['asset_id'])
                if asset.type != item['kind']: raise maa.invalid('媒体类型已变化')
                d=add_delivery(db, 'schedule:'+row.id+':'+str(row.revision)+':'+str(index),row.conversation_id,user_id=row.requested_by,asset_id=item['asset_id'])
                item['delivery_id']=d.id
            item.update(status='queued',dispatched_at=now); row.status='sending'; row.detail={}
        except BizError as exc:
            row.detail={'reason':str(exc.detail['message'])}
        break
    else:
        row.status='sent'; row.detail={'sent_count':len(messages),'total':len(messages)}
    row.messages=messages
    if history:row.detail={**row.detail,'history':history}
