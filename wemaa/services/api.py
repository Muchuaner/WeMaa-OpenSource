"""Narrow fixed-token API over the sole Maa worker's one-shot command queue."""
from __future__ import annotations

import hmac
import json
import re
import uuid
import unicodedata

from wemaa.config import settings
from wemaa.errors import BizError, conflict, not_found, unauthorized
from wemaa.services import maa as maa


def authenticate(header: str) -> None:
    token = settings.WECHAT_MAA_API_TOKEN
    if len(token) < 32 or token == settings.WECHAT_MAA_TOKEN:
        raise BizError(50333, '微信私聊 API 未配置独立 token', 503)
    if not header.startswith('Bearer ') or not hmac.compare_digest(header[7:].encode('utf-8'), token.encode('utf-8')):
        raise unauthorized('微信私聊 API token 无效')


def validate_recipient(recipient: dict) -> dict:
    recipient = maa._object(recipient, 'recipient')
    if set(recipient) not in ({'wechat_id'}, {'contact_id'}):
        raise maa.invalid('recipient 必须使用 wechat_id 或 contact_id，二选一')
    for key, value in recipient.items():
        maa._text(value, key, 256)
        if not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise maa.invalid('收件人标识无效')
    return dict(recipient)


def validate_message(body: dict) -> dict:
    if set(body) != {'request_id', 'recipient', 'text'}:
        raise maa.invalid('请求必须包含 request_id、recipient 和 text')
    try:
        request_id = str(uuid.UUID(body['request_id']))
    except (ValueError, TypeError, AttributeError):
        raise maa.invalid('request_id 必须为 UUID')
    recipient = validate_recipient(body['recipient'])
    text = maa._text(body['text'], 'text', 400)
    if not text.strip() or any(unicodedata.category(c) in {'Cc','Zl','Zp'} for c in text):
        raise maa.invalid('消息必须为 1–400 字单行文字，不允许控制字符')
    return {'request_id': request_id, 'recipient': dict(recipient), 'text': text}


def personal_contacts(record: dict | None) -> list[dict]:
    if not record:
        return []
    body = record['telemetry']
    scans = [s for s in body.get('scans', []) if s.get('scope') == 'personal'
             and s.get('status') == 'complete' and s.get('coverage_valid') is True
             and s.get('bottom_verified') is True]
    scan = max(scans, key=lambda s: s.get('finished') or s.get('started') or 0, default=None)
    return [r for r in body.get('contacts', []) if scan and r.get('scope') == 'personal' and r.get('scan_id') == scan['id']]


def contact_ready(row: dict) -> bool:
    facts = row.get('facts') or {}
    return bool(isinstance(facts, dict) and row.get('wechat_id') and re.fullmatch(r'[A-Za-z0-9_-]{1,256}', row['wechat_id'])
                and row.get('identity_status') == 'ocr_wechat_id'
                and facts.get('scope') == 'personal' and facts.get('list_avatar_sha'))


def contacts(keyword: str | None, page: int, page_size: int) -> dict:
    record = maa.latest()
    rows = personal_contacts(record)
    if keyword:
        rows = [r for r in rows if keyword.casefold() in ' '.join(str(r.get(k) or '')
                for k in ('id', 'wechat_id', 'display_name', 'list_name')).casefold()]
    rows.sort(key=lambda r: r.get('ordinal', 0))
    return {'items': [{'contact_id': r['id'], 'wechat_id': r.get('wechat_id'),
                       'display_name': r.get('display_name') or r.get('list_name'),
                       'identity_status': r.get('identity_status'), 'can_resolve': contact_ready(r)}
                      for r in rows[(page - 1) * page_size:page * page_size]],
            'total': len(rows), 'page': page, 'page_size': page_size, 'stale': maa.is_stale(record)}


def resolve_recipient(record: dict, recipient: dict) -> dict:
    field = 'id' if 'contact_id' in recipient else 'wechat_id'
    value = recipient.get('contact_id', recipient.get('wechat_id'))
    rows = [r for r in personal_contacts(record) if r.get(field) == value]
    if not rows:
        raise not_found('个人联系人不存在或未取得完整采集资料')
    if len(rows) != 1:
        raise conflict('联系人匹配不唯一')
    if not contact_ready(rows[0]):
        raise conflict('联系人身份资料不足，不能发送')
    return rows[0]


def create_message(body: dict) -> tuple[dict, bool]:
    value = validate_message(body)
    # A bounded selector is only a placeholder until atomic enqueue resolves it
    # against the watched telemetry. Repeat requests need no online lookup.
    recipient = value['recipient']
    command, created = maa.enqueue({'request_id': value['request_id'], 'action': 'send_contact_message',
        'params': {'contact_id': recipient.get('contact_id', recipient.get('wechat_id')),
                   'wechat_id': recipient.get('wechat_id', 'resolve-after-dedupe'), 'text': value['text']}},
        None, api_request=value)
    return serialize(command), created


def serialize(row: dict) -> dict:
    detail = row.get('detail') or {}
    evidence = detail.get('evidence') or {}
    diagnostic = detail.get('diagnostic') or {}
    expired = row['expires_at'] <= maa.time.time()
    raw = row['status']
    if raw == 'queued':
        status = 'expired' if expired else 'queued'
    elif raw == 'issued':
        status = 'uncertain' if expired else 'processing'
    elif raw == 'expired':
        status = 'expired'
    elif detail.get('status') == 'sent':
        verified = (raw == 'succeeded' and evidence.get('title_verified') is True
                    and evidence.get('outgoing_verified') is True and evidence.get('profile_verified') is True
                    and diagnostic.get('send_click_attempted') is True and diagnostic.get('draft_retained') is False)
        status = 'sent' if verified else 'uncertain'
    elif detail.get('status') == 'blocked' and diagnostic.get('send_click_attempted') is False:
        status = 'blocked'
    elif raw == 'failed' and diagnostic.get('send_click_attempted') is False:
        status = 'expired' if detail.get('reason') == 'MANUAL_COMMAND_EXPIRED' else 'failed'
    elif raw == 'failed' and detail.get('error') in {'MANUAL_WORKER_NOT_AVAILABLE', 'OLD_GATEWAY_NOT_DISABLED', 'COMMAND_EXPIRED'}:
        status = 'expired' if detail['error'] == 'COMMAND_EXPIRED' else 'failed'
    else:
        status = 'uncertain'
    reason = detail.get('reason') or detail.get('error')
    if reason and not (isinstance(reason, str) and re.fullmatch(r'[A-Za-z0-9_]{1,160}', reason)):
        reason = 'EXECUTION_FAILED'
    if raw == 'issued' and expired:
        reason = 'RESULT_NOT_RECEIVED_NO_RETRY'
    return {'message_id': row['id'], 'request_id': row['request_id'], 'status': status,
            'recipient': {'contact_id': row['params']['contact_id'], 'wechat_id': row['params']['wechat_id']},
            'created_at': row['created_at'], 'expires_at': row['expires_at'], 'finished_at': row.get('finished_at'),
            'reason': reason, 'evidence': {'target_verified': evidence.get('title_verified') is True,
                'profile_verified': evidence.get('profile_verified') is True,
                'outgoing_verified': evidence.get('outgoing_verified') is True,
                'input_empty_verified': status == 'sent'}}


def message_row(message_id: str) -> dict:
    maa._command_id(message_id)
    raw = maa.client().get(maa._command_key(message_id))
    row = json.loads(raw) if raw else None
    if not row or row.get('source') != 'send-api' or row.get('action') != 'send_contact_message':
        raise not_found('API 消息任务不存在')
    return row


def message(message_id: str) -> dict:
    return serialize(message_row(message_id))


def received_messages(wechat_id: str | None, contact_id: str | None, after: int, limit: int) -> dict:
    from wemaa.services import inbox as inbox
    recipient=validate_recipient({k:v for k,v in {'wechat_id':wechat_id,'contact_id':contact_id}.items() if v is not None})
    record=maa.latest()
    contact=resolve_recipient(record,recipient) if record else None
    if not contact:raise not_found('个人联系人资料尚未同步')
    return inbox.read({'contact_id':contact['id'],'wechat_id':contact['wechat_id']},after,limit)


def replies(message_id: str, after: int, limit: int) -> dict:
    from wemaa.services import inbox as inbox
    row=message_row(message_id);status=serialize(row)['status']
    since=row.get('issued_at',row['created_at'])
    result=inbox.read({'contact_id':row['params']['contact_id'],'wechat_id':row['params']['wechat_id']},
                      after,limit,since=since,ready=status=='sent')
    return dict(result,message_id=row['id'],request_id=row['request_id'],send_status=status,since=since)
