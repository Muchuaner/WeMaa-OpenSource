"""只读关联创作入站与即时交付；模型结果或 HTTP 200 不是发送回执。"""
import json
import re

from sqlalchemy import inspect, text

from wemaa.errors import BizError, not_found

EVENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{15,127}\Z')
CONVERSATION = re.compile(r'[0-9a-f]{64}\Z')
IMMEDIATE = ('reply', 'error', 'catalog', 'menu', 'asset', 'binding-link')


def _object(value):
    return json.loads(value) if isinstance(value, str) else value or {}


def status(db, event_id, conversation_id):
    """读取同一原事件的即时回信，不等待后续异步生图完成通知。"""
    if not isinstance(event_id, str) or not EVENT.fullmatch(event_id):
        raise BizError(40001, '创作事件 ID 无效')
    if not isinstance(conversation_id, str) or not CONVERSATION.fullmatch(conversation_id):
        raise BizError(40001, '视觉会话 ID 无效')
    schema = inspect(db.connection())
    if not all(schema.has_table(name) for name in ('wechat_creation_requests', 'wechat_deliveries')):
        raise BizError(50342, '创作回执关联暂不可用，原入站继续暂存', 503)
    row = db.execute(text('SELECT conversation_id,status,body,result,created_at '
                          'FROM wechat_creation_requests WHERE event_id=:event'),
                     {'event': event_id}).mappings().first()
    if not row or row['conversation_id'] != conversation_id:
        raise not_found('本会话没有这个创作事件')
    result = _object(row['result'])
    body = _object(row['body'])
    keys = {f'key{i}': event_id + ':' + suffix for i, suffix in enumerate(IMMEDIATE)}
    clauses = ','.join(':' + key for key in keys)
    deliveries = list(db.execute(text('SELECT id,status,detail FROM wechat_deliveries '
                                      f'WHERE dedupe_key IN ({clauses}) ORDER BY created_at,id'),
                                 keys).mappings())
    # 菜单/目录的显式交付引用也必须属于原事件；不从相似文本或时间猜关联。
    explicit = result.get('delivery_id')
    reference_missing = bool(explicit and not any(d['id'] == explicit for d in deliveries))
    evidence = []
    for delivery in deliveries:
        proof = _object(delivery['detail']).get('evidence') or {}
        verified = (delivery['status'] == 'sent' and proof.get('title_verified') is True
                    and proof.get('outgoing_verified') is True)
        evidence.append({'id': delivery['id'], 'status': delivery['status'], 'verified_sent': verified})
    state, reason = 'waiting', 'CREATION_IMMEDIATE_RECEIPT_PENDING'
    if reference_missing:
        state, reason = 'uncertain', 'CREATION_IMMEDIATE_REFERENCE_MISMATCH'
    elif any(d['status'] in ('failed', 'uncertain', 'cancelled') for d in evidence):
        state, reason = 'uncertain', 'CREATION_IMMEDIATE_DELIVERY_BLOCKED'
    elif evidence and all(d['verified_sent'] for d in evidence):
        state, reason = 'sent', 'CREATION_IMMEDIATE_DELIVERIES_VERIFIED'
    elif not evidence and result.get('handled') is True and row['status'] not in ('pending', 'processing'):
        state, reason = 'uncertain', 'CREATION_IMMEDIATE_DELIVERY_MISSING'
    return {'protocol': 'creation_receipts_v1', 'event_id': event_id,
            'conversation_id': conversation_id, 'status': state, 'reason': reason,
            'observed_at': body.get('observed_at'), 'created_at': row['created_at'],
            'deliveries': evidence}
