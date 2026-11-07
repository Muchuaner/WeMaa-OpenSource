"""Maa-only reply preparation and irreversible send receipts.

This bridge reuses the ordinary support conversation API (including the chosen
brain, memory, usage accounting and hand-off policy). It never loads the legacy
Windows gateway, never touches the desktop, and never sends a WeChat message.
Redis journals are written before external calls. Ambiguous generation/send
outcomes are terminal: losing a response must not trigger another model call or
another physical send.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
import logging
import sqlite3
from typing import Any

import httpx
import redis
from fastapi.concurrency import run_in_threadpool

from wemaa.config import settings
from wemaa.errors import BizError, conflict, not_found


GATEWAY_ID = "win-wechat-maa"
PREFIX = "wechat-maa:reply:"
JOURNAL_TTL = 30 * 86400
SESSION_TTL = 180 * 86400
GENERATION_SECONDS = 120
MAX_TEXT = 2000
MAX_VERIFIED_REPLY = 400
MAX_BODY = 128 * 1024
MAX_CONTEXT = 30
EVENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{15,127}$")
CONVERSATION_ID = re.compile(r"^[0-9a-f]{64}$")
SAFE_REASON = re.compile(r"^[A-Za-z0-9_.:=-]{0,160}$")
TERMINAL = {"sent", "failed", "uncertain", "no_reply"}
logger = logging.getLogger('wemaa.replies')


def invalid(message: str) -> BizError:
    return BizError(40001, message, 400)


def client():
    return redis.Redis.from_url(settings.REDIS_URL, decode_responses=True,
                                socket_connect_timeout=2, socket_timeout=3)


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _text(value: Any, name: str, maximum: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise invalid(f"{name} 文本无效")
    if "\x00" in value:
        raise invalid(f"{name} 包含非法字符")
    return value.strip()


def _event_id(value: Any) -> str:
    if not isinstance(value, str) or not EVENT_ID.fullmatch(value):
        raise invalid("event_id 无效")
    return value


def _creation_buffer_parent(event_id: str, conversation_id: str):
    """已核验的创作回信也可结束同一会话回合；不把 HTTP 接收视为送达。"""
    from wemaa.plugins import website
    if not website.configured():return None
    from wemaa.database import session_factory
    from wemaa.creation_turns import status
    try:
        with session_factory()() as db:
            row=status(db,event_id,conversation_id)
    except BizError as exc:
        if exc.status_code==404:return None
        raise
    observed=row.get('observed_at')
    if type(observed) not in (float,int) or not math.isfinite(observed):return None
    return {'serial_reply':True,'conversation_id':row['conversation_id'],'status':row['status'],
            'created_at':row['created_at'],'inbound':{'observed_at':observed}}


def validate_prepare(body: Any, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    if not isinstance(body, dict):
        raise invalid("请求体必须为对象")
    allowed = {"gateway_id", "event_id", "conversation_id", "conversation_title",
               "conversation_type", "sender_name", "sender_verified", "direction",
               "stable_ocr", "observed_at", "text", "context", "media_notice", "identity",
               "serial_reply", "buffered_after"}
    if set(body) - allowed:
        raise invalid("自动回复请求含未知字段")
    if body.get("gateway_id") != GATEWAY_ID:
        raise invalid("Maa 机器标识无效")
    event_id = _event_id(body.get("event_id"))
    cid = body.get("conversation_id")
    if not isinstance(cid, str) or not CONVERSATION_ID.fullmatch(cid):
        raise invalid("视觉会话 ID 无效")
    kind = body.get("conversation_type")
    if not isinstance(kind, str) or kind not in {"private", "group"}:
        raise invalid("会话类型无效")
    if body.get("sender_verified") is not True or body.get("stable_ocr") is not True:
        raise invalid("发送者或 OCR 尚未验证，不能自动回复")
    if body.get("direction") != "incoming":
        raise invalid("只接受已验证的新入站文字，不回复自己的消息")
    if 'media_notice' in body and (body['media_notice'] is not True or body.get('text')!='[收到图片/视频，内容尚未解析]'):
        raise invalid('媒体收件标记或固定内容无效')
    observed = body.get("observed_at")
    serial = body.get('serial_reply', False)
    if type(serial) is not bool:
        raise invalid('串行回复标记无效')
    buffered = body.get('buffered_after')
    if buffered is not None and (not serial or not isinstance(buffered, str) or not EVENT_ID.fullmatch(buffered)):
        raise invalid('暂存消息来源无效')
    delayed = False
    if serial and isinstance(observed, (int, float)) and not isinstance(observed, bool) and math.isfinite(observed):
        # 重试只读取同一事件；暂存消息保留真实观测时间，不能伪装成刚收到。
        existing = json.loads(client().get(event_key(event_id)) or 'null')
        delayed = bool(existing and existing.get('serial_reply') is True
                       and existing['conversation_id'] == cid
                       and existing['inbound']['observed_at'] == observed)
        if buffered and not delayed:
            parent = json.loads(client().get(event_key(buffered)) or 'null')
            if parent is None:
                parent=_creation_buffer_parent(buffered,cid)
            delayed = bool(parent and parent.get('serial_reply') is True
                           and parent['conversation_id'] == cid
                           and parent['status'] in ('sent', 'no_reply')
                           and (parent.get('inbound') or {}).get('observed_at',parent['created_at']) <= observed + 30
                           and now - 86400 <= observed <= now + 30)
            if not delayed:
                raise BizError(50339,'上一回合尚未核验送达，原入站继续暂存',503)
    if (isinstance(observed, bool) or not isinstance(observed, (int, float)) or
            (isinstance(observed, float) and not math.isfinite(observed)) or
            not (delayed or now - 300 <= observed <= now + 30)):
        raise invalid("只接受最近五分钟的已验证新消息")
    context = body.get("context", [])
    if not isinstance(context, list) or len(context) > MAX_CONTEXT:
        raise invalid("参考上下文超过限制")
    normalized_context = []
    for item in context:
        if not isinstance(item, dict) or set(item) != {"role", "content"}:
            raise invalid("参考上下文字段无效")
        if not isinstance(item.get("role"), str) or item["role"] not in {"user", "assistant"}:
            raise invalid("参考上下文不能含系统或工具指令")
        normalized_context.append({"role": item["role"], "content": _text(item.get("content"), "上下文", MAX_TEXT)})
    identity = body.get('identity')
    if identity is not None:
        keys = {'wechat_id','display_name','profile_sha256','message_frame_sha256','message_avatar_sha256','stable'}
        if (not isinstance(identity,dict) or set(identity) != keys or identity.get('stable') is not True
                or not re.fullmatch(r'[A-Za-z0-9_-]{3,128}',str(identity.get('wechat_id','')))
                or any(not re.fullmatch(r'[a-f0-9]{64}',str(identity.get(k,''))) for k in
                       ('profile_sha256','message_frame_sha256','message_avatar_sha256'))):
            raise invalid('消息发送者资料证据无效')
        _text(identity.get('display_name'), '资料页姓名', 256)
    return {"gateway_id": GATEWAY_ID, "event_id": event_id, "conversation_id": cid,
            "conversation_title": _text(body.get("conversation_title"), "会话标题", 256),
            "conversation_type": kind, "sender_name": _text(body.get("sender_name"), "发送者", 256),
            "sender_verified": True, "direction": "incoming", "stable_ocr": True,
            "observed_at": observed, "text": _text(body.get("text"), "入站消息", MAX_TEXT),
            "context": normalized_context, **({'identity':identity} if identity else {}),
            **({'serial_reply': True} if serial else {}),
            **({'buffered_after': buffered} if buffered else {}),
            **({'media_notice':True} if body.get('media_notice') is True else {})}


def event_key(event_id: str) -> str:
    return PREFIX + "event:" + hashlib.sha256(f"{GATEWAY_ID}\x1f{_event_id(event_id)}".encode()).hexdigest()


def conversation_key(cid: str) -> str:
    return PREFIX + "conversation:" + cid


def _fingerprint(body: dict) -> str:
    # Clock/context can differ on a harmless retry. Identity and incoming text
    # cannot: an OCR correction needs a new, explicitly reconciled event.
    core = {name: body[name] for name in ("gateway_id", "event_id", "conversation_id",
            "conversation_title", "conversation_type", "sender_name", "text")}
    if '_experiment' in body: core['_experiment'] = body['_experiment']
    if 'media_notice' in body:core['media_notice']=body['media_notice']
    if 'identity' in body:core['identity']=body['identity']
    for field in ('serial_reply', 'buffered_after'):
        if field in body: core[field] = body[field]
    return hashlib.sha256(dumps(core).encode()).hexdigest()


def public_record(row: dict, *, include_reply: bool = False) -> dict:
    result = {name: row.get(name) for name in ("event_id", "conversation_id", "status", "reply_id",
              "support_conversation_id", "created_at", "updated_at", "expires_at", "reason")}
    if include_reply and row.get("status") == "ready":
        result["reply_text"] = row.get("reply_text", "")
    if row.get('serial_reply') is True:
        result['serial_reply'] = True
    return result


def _write(pipe, key: str, row: dict) -> None:
    pipe.set(key, dumps(row), ex=JOURNAL_TTL)


def _begin(body: dict) -> tuple[dict, bool]:
    if not getattr(settings, "WECHAT_MAA_REPLY_ENABLED", False):
        raise BizError(50304, "Maa 自动回复尚未启用", 503)
    cache, now = client(), time.time()
    key, lock_key = event_key(body["event_id"]), conversation_key(body["conversation_id"])
    fingerprint = _fingerprint(body)
    with cache.pipeline() as pipe:
        for _ in range(6):
            try:
                pipe.watch(key, lock_key)
                raw = pipe.get(key)
                if raw:
                    row = json.loads(raw)
                    if row["fingerprint"] != fingerprint:
                        raise conflict("同一入站事件的消息或身份不可修改")
                    if row["status"] == "generating" and now > row["generation_deadline"]:
                        row.update(status="uncertain", reason="GENERATION_OUTCOME_UNKNOWN", updated_at=now)
                        pipe.multi()
                        _write(pipe, key, row)
                        pipe.execute()
                    elif row["status"] == "sending" and now > row["claimed_at"] + GENERATION_SECONDS:
                        row.update(status="uncertain", reason="SEND_OUTCOME_UNKNOWN", updated_at=now)
                        pipe.multi()
                        _write(pipe, key, row)
                        pipe.execute()
                    return row, False
                lock_raw = pipe.get(lock_key)
                if lock_raw:
                    held = json.loads(lock_raw)
                    prior = json.loads(pipe.get(event_key(held['event_id'])) or 'null')
                    if (prior and prior.get('serial_reply') is True
                            and prior['status'] not in ('sent', 'no_reply')) or now <= held.get("deadline", 0):
                        raise conflict("同一会话正在生成回复，请稍后重试")
                digest = hashlib.sha256(f"{GATEWAY_ID}\x1f{body['event_id']}".encode()).hexdigest()
                row = {"event_id": body["event_id"], "conversation_id": body["conversation_id"],
                       "status": "generating", "reply_id": "maa-reply-" + digest,
                       "fingerprint": fingerprint, "created_at": now, "updated_at": now,
                       "generation_deadline": now + (1800 if body.get('serial_reply') else GENERATION_SECONDS),
                       "expires_at": min(now + 300, body["observed_at"] + 300),
                       "inbound": body, "support_conversation_id": None, "reason": None,
                       **({'serial_reply': True} if body.get('serial_reply') else {})}
                pipe.multi()
                _write(pipe, key, row)
                pipe.set(lock_key, dumps({"event_id": body["event_id"], "deadline": now + GENERATION_SECONDS}),
                         ex=JOURNAL_TTL if body.get('serial_reply') else GENERATION_SECONDS + 30)
                pipe.lpush(PREFIX + "history", key)
                pipe.ltrim(PREFIX + "history", 0, 999)
                pipe.expire(PREFIX + "history", JOURNAL_TTL)
                pipe.execute()
                return row, True
            except redis.WatchError:
                continue
    raise conflict("Maa 回复状态繁忙，请稍后重试")


def _finish(row: dict, status: str, *, reply: str = "", reason: str | None = None,
            public_id: str | None = None) -> dict:
    cache, key = client(), event_key(row["event_id"])
    lock_key = conversation_key(row["conversation_id"])
    with cache.pipeline() as pipe:
        for _ in range(6):
            try:
                pipe.watch(key, lock_key)
                current = json.loads(pipe.get(key) or "null")
                if not current:
                    raise not_found("Maa 回复事件不存在")
                if current["status"] != "generating":
                    return current
                current.update(status=status, reply_text=reply, reason=reason,
                               support_conversation_id=public_id, updated_at=time.time())
                lock = json.loads(pipe.get(lock_key) or "null")
                pipe.multi()
                _write(pipe, key, current)
                if (lock and lock.get("event_id") == row["event_id"]
                        and (not current.get('serial_reply') or status in ('sent', 'no_reply'))):
                    pipe.delete(lock_key)
                pipe.execute()
                return current
            except redis.WatchError:
                continue
    raise conflict("Maa 回复完成状态繁忙")


def _session_key(cid: str) -> str:
    return PREFIX + "session:" + cid


def _load_session(cid: str) -> tuple[str, str] | None:
    cache = client()
    raw = cache.get(_session_key(cid))
    if not raw:
        return None
    body = json.loads(raw)
    cache.expire(_session_key(cid), SESSION_TTL)
    return str(body["public_id"]), str(body["visitor_token"])


def _save_session(cid: str, public_id: str, token: str) -> None:
    client().set(_session_key(cid), dumps({"public_id": public_id, "visitor_token": token}), ex=SESSION_TTL)


def _support_base() -> str:
    # This is a trusted deployment URL, never an address supplied by a chat.
    return settings.WECHAT_GATEWAY_INTERNAL_API_BASE.rstrip("/") + settings.API_PREFIX + "/support"


def require_desktop_ready() -> None:
    """先确认出站通路可用，再交给核心；短暂重启只等待，不启动必失败的回复。"""
    from wemaa.services import maa

    record = maa.latest()
    runtime = (record or {}).get('telemetry', {}).get('runtime', {})
    progress = runtime.get('progress') or {}
    if (maa.is_stale(record)
            or str(runtime.get('worker_state', '')).casefold() != 'running'
            or str(runtime.get('trial_state', '')).casefold() == 'running'
            or runtime.get('sending_enabled') is not True
            or progress.get('manual_commands_enabled') is not True):
        raise BizError(50335, 'Maa 出站通路尚未就绪，入站消息继续暂存', 503)


def core_ingest_absent(event_id: str) -> bool:
    """操作员恢复前的只读否定证据；正常状态查询从不调用或恢复模型。"""
    from src.common.database.database import _DB_FILE
    from wemaa.runtime import runtime
    with sqlite3.connect('file:'+str(_DB_FILE)+'?mode=ro',uri=True,timeout=2) as db:
        stored=db.execute('SELECT 1 FROM mai_messages WHERE platform=? AND message_id=? LIMIT 1',
                          ('wechat','maa_'+_event_id(event_id))).fetchone()
    if stored:return False
    path=runtime.registry.path.parent/'delivery-guards.sqlite3'
    with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True,timeout=2) as db:
        attempted=db.execute('SELECT 1 FROM attempts WHERE source_event=? LIMIT 1',(event_id,)).fetchone()
    return not attempted


def _claim_operator_ingress_retry(body: dict, absent: bool) -> tuple[dict,bool]:
    """仅消费服务器私有日志中的一次性操作员许可；请求体不能创建许可。"""
    cache=client();key=event_key(body['event_id']);lk=conversation_key(body['conversation_id'])
    with cache.pipeline() as pipe:
        pipe.watch(key,lk)
        row=json.loads(pipe.get(key) or 'null')
        if not row:raise not_found('原入站事件不存在')
        permit=row.get('operator_retry_ingress')
        if not permit:return row,False
        now=time.time()
        if (not isinstance(permit,dict) or not absent or row['status']!='uncertain' or row.get('serial_reply') is not True
                or row.get('reason')!='WEMAA_CORE_OUTCOME_UNKNOWN' or row.get('receipt')
                or row.get('support_conversation_id') or row.get('operator_ingress_retry_count')
                or permit.get('kind')!='verified_empty_core_ingress_v1'
                or permit.get('expected_fingerprint')!=row['fingerprint']
                or row['fingerprint']!=_fingerprint(body)
                or type(permit.get('expires_at')) not in (float,int)
                or not now<=permit['expires_at']<=now+120):
            raise conflict('操作员恢复许可或未执行证据不满足，保留原阻塞')
        lock=json.loads(pipe.get(lk) or 'null')
        if not lock or lock.get('event_id')!=row['event_id']:
            raise conflict('原会话锁已变化，不能恢复')
        previous=dict(row)
        row.pop('operator_retry_ingress')
        row.update(status='generating',reason='OPERATOR_VERIFIED_EMPTY_INGRESS_RETRY',
                   operator_ingress_retry_count=1,updated_at=now,
                   generation_deadline=now+1800,expires_at=now+300)
        audit={'kind':'operator_verified_empty_ingress_retry','at':now,
               'previous':previous,'core_message_absent':True,'delivery_attempt_absent':True}
        pipe.multi()
        pipe.lpush(PREFIX+'operator-recovery-audit',dumps(audit))
        pipe.expire(PREFIX+'operator-recovery-audit',JOURNAL_TTL)
        _write(pipe,key,row)
        pipe.execute()
        return row,True


async def prepare(body: dict) -> dict:
    """可靠入站进入核心；出站由原生 Maa 驱动独立持久排队并核验。"""
    from wemaa.runtime import runtime
    normalized = await run_in_threadpool(validate_prepare,body)
    # 已有事件只能读取原始状态；离线期间也不能重新生成或丢掉回执。
    # 新事件在持久入站/模型调用前拒绝，桌面使用同一 event_id 等待重试。
    existing = await run_in_threadpool(client().get, event_key(normalized['event_id']))
    saved = json.loads(existing) if existing else None
    operator_retry = bool(saved and saved.get('operator_retry_ingress'))
    admitted = None
    if not existing or operator_retry:
        await run_in_threadpool(require_desktop_ready)
        try:
            admitted = await runtime.call(runtime.admit_identity(normalized))
        except sqlite3.OperationalError as exc:
            logger.warning('身份准备未进入核心 event=%s error=%s',normalized['event_id'],type(exc).__name__)
            raise BizError(50336,'身份数据库暂时繁忙，原入站继续暂存',503) from exc
        except RuntimeError as exc:
            if str(exc)!='WEMAA_CORE_NOT_READY':raise
            raise BizError(50338,'Maa 核心尚未就绪，原入站继续暂存',503) from exc
        except ValueError as exc:
            raise BizError(40912,'Maa 入站身份或路由尚未核验',409) from exc
    row, created = await run_in_threadpool(_begin, normalized)
    if not created and operator_retry:
        absent = await run_in_threadpool(core_ingest_absent,normalized['event_id'])
        row, created = await run_in_threadpool(_claim_operator_ingress_retry,normalized,absent)
    if not created:
        return public_record(row, include_reply=True)
    from wemaa.services import greetings
    await run_in_threadpool(greetings.inbound, normalized['conversation_id'], normalized['text'])
    try:
        result = await runtime.call(runtime.ingest(normalized,admitted=admitted))
        decision = result.get('group_reply')
        if decision and decision.get('reason') == 'classification_failed':
            raise ValueError('WEMAA_GROUP_CLASSIFICATION_FAILED')
        reason = ('WEMAA_GROUP_SILENT_' + decision['reason'].upper()
                  if decision and decision['reply'] is False else 'WEMAA_CORE_ACCEPTED')
        status = ('generating' if normalized.get('serial_reply')
                  and not (decision and decision['reply'] is False) else 'no_reply')
        completed = await run_in_threadpool(_finish, row, status, reason=reason,
                                            public_id=result['session_id'])
    except ValueError as exc:
        completed = await run_in_threadpool(_finish, row, 'failed', reason=str(exc)[:120])
    except Exception:
        # 入站可能已写入核心，禁止重新执行同一事件。
        logger.exception('核心入站结果未知 event=%s',normalized['event_id'])
        completed = await run_in_threadpool(_finish, row, 'uncertain', reason='WEMAA_CORE_OUTCOME_UNKNOWN')
    return public_record(completed, include_reply=True)


def core_status(event_id: str) -> dict:
    """Maa 只查询本轮终态；查询不会重新调用模型或执行发送。"""
    row = json.loads(client().get(event_key(event_id)) or 'null')
    if not row or row.get('serial_reply') is not True:
        raise not_found('串行回复事件不存在')
    if row['status'] == 'generating' and time.time() > row['generation_deadline']:
        row = _finish(row, 'uncertain', reason='WEMAA_CORE_TURN_TIMEOUT')
    return public_record(row)


def complete_core_turn(event_id: str, status: str, reason: str) -> dict:
    if status not in ('sent', 'no_reply', 'failed', 'uncertain'):
        raise ValueError('CORE_TURN_STATUS_INVALID')
    row = json.loads(client().get(event_key(event_id)) or 'null')
    if not row or row.get('serial_reply') is not True:
        raise ValueError('CORE_SERIAL_EVENT_NOT_FOUND')
    return public_record(_finish(row, status, reason=reason))


def claim(event_id: str, body: Any) -> dict:
    if not getattr(settings, "WECHAT_MAA_REPLY_ENABLED", False):
        raise BizError(50304, "Maa 自动回复尚未启用", 503)
    if not isinstance(body, dict) or set(body) != {"reply_id"}:
        raise invalid("发送许可只接受 reply_id")
    cache, key, now = client(), event_key(event_id), time.time()
    with cache.pipeline() as pipe:
        for _ in range(6):
            try:
                from wemaa.services import retired_experiment as experiment,greetings as greetings
                pipe.watch(key, experiment.KEY,greetings.KEY)
                row = json.loads(pipe.get(key) or "null")
                if not row:
                    raise not_found("Maa 回复事件不存在")
                if body["reply_id"] != row["reply_id"]:
                    raise conflict("回复 ID 与入站事件不匹配")
                permit = row["status"] == "ready"
                if permit:
                    experiment_state = json.loads(pipe.get(experiment.KEY) or 'null') or experiment.initial()
                    blocked = experiment.claim_reason(row, experiment_state)
                    blocked=blocked or greetings.reason(row,json.loads(pipe.get(greetings.KEY) or 'null') or greetings.initial())
                    if blocked:
                        row.update(status='failed', reason=blocked, updated_at=now)
                        permit = False
                    elif now > row["expires_at"]:
                        row.update(status="failed", reason="REPLY_EXPIRED", updated_at=now)
                        permit = False
                    else:
                        row.update(status="sending", claimed_at=now, updated_at=now)
                    pipe.multi()
                    _write(pipe, key, row)
                    pipe.execute()
                return {**public_record(row), "permit": permit}
            except redis.WatchError:
                continue
    raise conflict("Maa 发送许可繁忙")


def result(event_id: str, body: Any) -> dict:
    if not isinstance(body, dict) or set(body) - {"status", "reason", "evidence"}:
        raise invalid("发送结果字段无效")
    status, reason = body.get("status"), body.get("reason", "")
    if not isinstance(status, str) or status not in {"sent", "failed", "uncertain"}:
        raise invalid("发送结果状态无效")
    if not isinstance(reason, str) or not SAFE_REASON.fullmatch(reason):
        raise invalid("结果原因只能为简短 ASCII 状态码")
    evidence = body.get("evidence", {})
    if not isinstance(evidence, dict) or set(evidence) - {"frame_sha256", "snapshot_id", "title_verified", "outgoing_verified"}:
        raise invalid("发送证据字段无效")
    if "frame_sha256" in evidence and (not isinstance(evidence["frame_sha256"], str) or
                                       not CONVERSATION_ID.fullmatch(evidence["frame_sha256"])):
        raise invalid("截图 SHA256 无效")
    if "snapshot_id" in evidence and (type(evidence["snapshot_id"]) is not int or not 0 < evidence["snapshot_id"] < 1e12):
        raise invalid("截图 ID 无效")
    for flag in ("title_verified", "outgoing_verified"):
        if flag in evidence and type(evidence[flag]) is not bool:
            raise invalid("发送验证状态无效")
    if status == "sent" and (evidence.get("title_verified") is not True or evidence.get("outgoing_verified") is not True):
        raise invalid("成功发送必须核验会话标题和出站消息")
    receipt = {"status": status, "reason": reason, "evidence": evidence}
    cache, key = client(), event_key(event_id)
    with cache.pipeline() as pipe:
        for _ in range(6):
            try:
                pipe.watch(key)
                row = json.loads(pipe.get(key) or "null")
                if not row:
                    raise not_found("Maa 回复事件不存在")
                if row["status"] in TERMINAL:
                    if row.get("receipt") == receipt:
                        return public_record(row)
                    if row["status"] == "uncertain" and status == "sent" and not row.get("reconciled"):
                        # 延迟对账只允许一次 uncertain→sent 的观察收敛；
                        # 它核验的是早已结束的那次点击，绝不授权重发。
                        if reason != "RECONCILED_OUTGOING_OBSERVED":
                            raise invalid("对账收敛原因必须为 RECONCILED_OUTGOING_OBSERVED")
                        if not isinstance(evidence.get("frame_sha256"), str) or not CONVERSATION_ID.fullmatch(evidence["frame_sha256"]):
                            raise invalid("对账收敛必须包含有效的截图 SHA256")
                        if type(evidence.get("snapshot_id")) is not int or not (0 < evidence["snapshot_id"] < 1e12):
                            raise invalid("对账收敛必须包含有效的快照 ID")
                        if evidence.get("title_verified") is not True or evidence.get("outgoing_verified") is not True:
                            raise invalid("对账收敛必须核验会话标题和出站消息")
                        row.update(status="sent", reason=reason or None, receipt=receipt,
                                   reconciled=True, updated_at=time.time())
                        pipe.multi()
                        _write(pipe, key, row)
                        pipe.execute()
                        return public_record(row)
                    raise conflict("已终结的发送结果不可覆盖或重发")
                if row["status"] != "sending":
                    raise conflict("发送结果只能确认已经领取的一次性发送许可")
                row.update(status=status, reason=reason or None, receipt=receipt, updated_at=time.time())
                pipe.multi()
                _write(pipe, key, row)
                pipe.execute()
                return public_record(row)
            except redis.WatchError:
                continue
    raise conflict("Maa 发送结果繁忙")


def history(limit: int = 100) -> dict:
    cache = client()
    keys = cache.lrange(PREFIX + "history", 0, limit - 1)
    records = [json.loads(raw) for raw in cache.mget(keys) if raw] if keys else []
    return {"enabled": bool(getattr(settings, "WECHAT_MAA_REPLY_ENABLED", False)),
            "items": [public_record(row) for row in records],
            "retention_seconds": JOURNAL_TTL, "sending_transport": "MaaFramework"}
