"""Maa desktop evidence and one-shot commands, isolated from the reply gateway.

Redis stores a bounded, last-known observation, not a source of verified WeChat
identities. GUI commands are never redelivered after being issued. The Windows
agent must also persist the command id before executing a side effect.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import math
import re
import time
import uuid
from typing import Any

import redis
import unicodedata
from botocore.exceptions import ClientError
from PIL import Image, UnidentifiedImageError

from wemaa.config import settings
from wemaa.errors import BizError, conflict, not_found, unauthorized
from wemaa.storage import get_client

GATEWAY_ID = "win-wechat-maa"
ASSET_BUCKET = "wemaa-maa"
PREFIX = "wechat-maa:"
MAX_BODY = 8 * 1024 * 1024
MAX_RESULT = 64 * 1024
STALE_SECONDS = 90
COMMAND_TTL = 90
HISTORY_TTL = 7 * 86400
SCOPES = {"personal", "enterprise", "public"}
ACTIONS = {"set_polling", "set_group_whitelist", "start_watch", "stop_worker", "scan_contacts", "capture",
           "open_contacts", "open_recent", "friend_count", "checkpoint", "send_message", "send_contact_message", "send_media", "open_conversation", "open_contact"}
MANUAL_ACTIONS = {'send_message','send_contact_message','send_media','open_conversation','open_contact'}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX32 = re.compile(r"^[0-9a-f]{32}$")


def invalid(message: str) -> BizError:
    return BizError(40001, message, 400)


def client():
    return redis.Redis.from_url(settings.REDIS_URL, decode_responses=True,
                                socket_connect_timeout=2, socket_timeout=3)


def authorised(header: str) -> bool:
    token = getattr(settings, "WECHAT_MAA_TOKEN", "")
    return bool(token and header.startswith("Bearer ") and
                hmac.compare_digest(header[7:].encode("utf-8"), token.encode("utf-8")))


def authenticate(header: str) -> None:
    if not authorised(header):
        raise unauthorized("Maa 机器认证失败")


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _object(value: Any, name: str) -> dict:
    if not isinstance(value, dict):
        raise invalid(f"{name} 必须为对象")
    return value


def _list(value: Any, name: str, limit: int) -> list:
    if not isinstance(value, list) or len(value) > limit:
        raise invalid(f"{name} 列表超过限制")
    return value


def _number(value: Any, name: str, *, low: float = 0, high: float = 1e12) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            (isinstance(value, float) and not math.isfinite(value)) or not low <= value <= high):
        raise invalid(f"{name} 数值无效")
    return value


def _text(value: Any, name: str, limit: int = 4096) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise invalid(f"{name} 文本无效")
    return value


def _asset(value: Any) -> None:
    if value is not None and (not isinstance(value, str) or not HEX64.fullmatch(value)):
        raise invalid("图片引用必须为 PNG 文件 SHA256")


def _tree(value: Any, depth: int = 0) -> None:
    """Reject deeply nested, non-finite or unbounded arbitrary detail metadata."""
    if depth > 16:
        raise invalid("数据嵌套过深")
    if isinstance(value, dict):
        if len(value) > 256:
            raise invalid("对象字段过多")
        for key, child in value.items():
            _text(key, "字段名", 128)
            _tree(child, depth + 1)
    elif isinstance(value, list):
        if len(value) > 10000:
            raise invalid("明细条数过多")
        for child in value:
            _tree(child, depth + 1)
    elif isinstance(value, str):
        if len(value) > 65536:
            raise invalid("明细文本过长")
    elif isinstance(value, float) and not math.isfinite(value):
        raise invalid("数值必须有限")
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise invalid("数据类型无效")


def validate_telemetry(body: dict, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    _object(body, "telemetry")
    _tree(body)
    if len(dumps(body).encode()) > MAX_BODY:
        raise BizError(41301, "数据超过 8MB", 413)
    if type(body.get("schema_version")) is not int or body["schema_version"] != 1 or body.get("gateway_id") != GATEWAY_ID:
        raise invalid("Maa schema 或机器标识不匹配")
    collected = _number(body.get("collected_at"), "collected_at")
    if collected > now + 30 or collected < now - 300:
        raise invalid("采集时间过期或机器时钟偏差过大")
    runtime = _object(body.get("runtime"), "runtime")
    for flag in ("sending_enabled", "auto_reply_enabled", "continuous"):
        if type(runtime.get(flag, False)) is not bool:
            raise invalid("Maa 功能开关必须为布尔值")
    if runtime.get("sending_enabled", False) and not getattr(settings, "WECHAT_MAA_REPLY_ENABLED", False):
        raise invalid("服务端尚未启用 Maa 回复")
    for field in ("worker_state", "trial_state", "old_gateway_state"):
        _text(runtime.get(field), field, 80)
    _asset(runtime.get("capture_asset"))
    _object(runtime.get("progress", {}), "progress")
    _object(runtime.get("versions", {}), "versions")
    layout = _object(runtime.get("layout", {}), "layout")
    for name in ("screen", "client"):
        if name in layout:
            values = _list(layout[name], name, 2)
            if len(values) != 2:
                raise invalid("桌面尺寸无效")
            for value in values:
                _number(value, name, low=1, high=8192)
    for name in ("divider_x", "dpi"):
        if name in layout:
            _number(layout[name], name, low=1, high=8192)
    for name in ("progress_updated_at",):
        if runtime.get(name) is not None:
            _number(runtime[name], name, high=now + 30)
    summary = _object(body.get("summary"), "summary")
    for name in ("conversation_count", "message_row_count", "snapshot_count"):
        _number(summary.get(name), name, high=1e9)
    _object(summary.get("snapshot_status_counts", {}), "snapshot_status_counts")
    for scan in _list(body.get("scans", []), "scans", 100):
        _object(scan, "scan")
        if not isinstance(scan.get("scope"), str) or scan["scope"] not in SCOPES or not HEX32.fullmatch(str(scan.get("id", ""))):
            raise invalid("联系人扫描标识无效")
        _text(scan.get("status"), "scan status", 64)
        for field in ("expected", "observed", "position_count"):
            if scan.get(field) is not None:
                _number(scan[field], field, high=100000)
        for field in ("coverage_valid", "bottom_verified"):
            if field in scan and not isinstance(scan[field], bool):
                raise invalid("扫描覆盖状态无效")
    for contact in _list(body.get("contacts", []), "contacts", 10000):
        _object(contact, "contact")
        if not isinstance(contact.get("scope"), str) or contact["scope"] not in SCOPES or not HEX32.fullmatch(str(contact.get("scan_id", ""))):
            raise invalid("联系人分区或扫描标识无效")
        _text(contact.get("id"), "contact id", 256)
        _number(contact.get("ordinal"), "ordinal", high=100000)
        _text(contact.get("display_name", ""), "display_name", 512)
        _text(contact.get("list_name", ""), "list_name", 512)
        _asset(contact.get("avatar_asset"))
        _asset(contact.get("profile_asset"))
    conversation_ids = set()
    for row in _list(body.get("conversations", []), "conversations", 2000):
        _object(row, "conversation")
        cid = row.get("id")
        if not isinstance(cid, str) or not HEX64.fullmatch(cid) or cid in conversation_ids:
            raise invalid("会话 ID 无效或重复")
        conversation_ids.add(cid)
        for alias in _list(row.get('aliases',[]),'group aliases',500):
            if not isinstance(alias,str) or not HEX64.fullmatch(alias):raise invalid('群聊别名无效')
        if 'manual_target_observed' in row and type(row['manual_target_observed']) is not bool:
            raise invalid('会话路径状态无效')
        _text(row.get("title"), "title", 512)
        _asset(row.get("avatar_asset"))
        if row.get('group_profile') is not None:
            profile = _object(row['group_profile'], 'group_profile')
            if profile.get('kind') != 'group' or profile.get('conversation_id') != cid:
                raise invalid('群聊身份关联无效')
            _text(profile.get('name'), 'group name', 512)
            _text(profile.get('row_name'), 'group row name', 512)
            _text(profile.get('header'), 'group header', 512)
            count = profile.get('member_count')
            if type(count) is not int or not 1 <= count <= 9999:
                raise invalid('群人数无效')
            if not re.fullmatch(re.escape(re.sub(r'\s+', '', profile['row_name'])) + r'[（(]' + str(count) + r'[）)]', re.sub(r'\s+', '', profile['header'])):
                raise invalid('群人数缺少匹配标题证据')
            _number(profile.get('observed_at'), 'group observed_at', high=now + 30)
        snapshot = row.get("latest_snapshot")
        if snapshot is not None:
            _object(snapshot, "latest_snapshot")
            _asset(snapshot.get("frame_asset"))
            for payload in _list(snapshot.get("payloads", []), "payloads", 500):
                _object(payload, "payload")
                _asset(payload.get("media_asset"))
                _asset(payload.get("sender_avatar_asset"))
                if "text" in payload:
                    _text(payload["text"], "message text", 16384)
        for message in _list(row.get("messages", []), "messages", 500):
            _object(message, "message")
            payload = _object(message.get("payload"), "message payload")
            _asset(payload.get("sender_avatar_asset"))
    for name, limit in (("events", 500), ("actions", 500), ("backups", 30)):
        for value in _list(body.get(name, []), name, limit):
            _object(value, name)
    for name in ("lessons", "acceptance"):
        if body.get(name) is not None:
            _object(body[name], name)
    return body


def store_telemetry(body: dict) -> dict:
    from wemaa.services import inbox as inbox
    now = time.time()
    validate_telemetry(body, now)
    record = {"received_at": now, "telemetry": body}
    cache = client()
    # Do not allow an older delivery to overwrite a newer successful observation.
    with cache.pipeline() as pipe:
        for _ in range(5):
            try:
                pipe.watch(PREFIX + "latest", PREFIX + "activity", inbox.KEY, inbox.SEQ_KEY)
                old_raw = pipe.get(PREFIX + "latest")
                if old_raw and json.loads(old_raw)["telemetry"]["collected_at"] > body["collected_at"]:
                    raise conflict("采集数据早于已接收数据")
                feed = merge_activity(json.loads(pipe.get(PREFIX + 'activity') or '{}'), body, now)
                received,sequence=inbox.merge(json.loads(pipe.get(inbox.KEY) or '{}'),body,now,
                                               int(pipe.get(inbox.SEQ_KEY) or 0))
                pipe.multi()
                pipe.set(PREFIX + "latest", dumps(record))
                pipe.set(PREFIX + 'activity', dumps(feed), ex=HISTORY_TTL)
                pipe.set(inbox.KEY,dumps(received),ex=HISTORY_TTL)
                pipe.set(inbox.SEQ_KEY,sequence)
                pipe.execute()
                return {"received_at": now}
            except redis.WatchError:
                continue
    raise conflict("采集写入并发冲突，请稍后重试")


def merge_activity(feed: dict, body: dict, now: float) -> dict:
    """Bounded persistent activity; transport repeats do not duplicate events.

    Only whitelisted scalar metadata is kept, never tokens, drafts or diagnostics.
    Seen IDs are refreshed even when rows age out of the display history.
    """
    seen = {key:value for key,value in feed.get('seen', {}).items() if value >= now-HISTORY_TTL}
    items = [row for row in feed.get('items', []) if row['seen'] >= now-HISTORY_TTL]
    seq = feed.get('seq', 0)
    sources = []
    for event in body.get('events', []):
        if type(event.get('id')) is not int or not isinstance(event.get('kind'), str):
            continue
        stamp = event.get('seen')
        if type(stamp) not in (int,float) or not now-HISTORY_TTL <= stamp <= now+30:
            continue
        detail = event.get('detail', {})
        if not isinstance(detail, dict):
            detail = {}
        detail = {k:v for k,v in detail.items() if k in {'conversation','conversation_id','command_id','status','name','contact_id','scan_id','event_id','member_count','diff_status','new_count','reason','reply_safe','snapshot_id','ordinal','unread','run_id'} and type(v) in (str,int,float,bool,type(None))}
        detail = {k:v[:512] if isinstance(v,str) else v for k,v in detail.items()}
        sources.append((f"event:{event['id']}:{stamp}", event['kind'][:128], stamp, detail))
    for action in body.get('actions', []):
        stamp = action.get('time_unix')
        if type(stamp) not in (int,float) or not now-HISTORY_TTL <= stamp <= now+30:
            continue
        node = action.get('node')
        if not isinstance(node,str) or type(action.get('succeeded')) is not bool:
            continue
        identity = hashlib.sha256(dumps(action).encode()).hexdigest()
        sources.append(('action:'+identity,'maa_action',stamp,{'node':node[:128],'succeeded':action['succeeded'],'run_id':str(action.get('run_id',''))[:128]}))
    for identity,kind,stamp,detail in sorted(sources,key=lambda r:r[2]):
        if identity not in seen:
            seq += 1
            items.append({'seq':seq,'id':identity,'kind':kind,'seen':stamp,'detail':detail})
        seen[identity] = now
    return {'seq':seq,'items':items[-5000:], 'seen':dict(sorted(seen.items(),key=lambda r:r[1])[-10000:])}


def activity(after: int = 0, before: int = 0, limit: int = 100, category: str = 'all') -> dict:
    if after and before:
        raise invalid('不能同时使用前后游标')
    feed = json.loads(client().get(PREFIX+'activity') or '{}')
    rows = feed.get('items', [])
    if category == 'messages': rows = [r for r in rows if r['kind']=='message_received' or r['kind'].startswith('reply_')]
    elif category == 'contacts': rows = [r for r in rows if r['kind'].startswith(('contact_','group_'))]
    elif category == 'polling': rows = [r for r in rows if r['kind']=='chat_observed']
    elif category == 'actions': rows = [r for r in rows if r['kind']=='maa_action']
    else: rows = [r for r in rows if r['kind']!='maa_action']
    rows = [r for r in rows if (not after or r['seq']>after) and (not before or r['seq']<before)]
    # Incremental fetch walks forwards, preventing loss when >limit events arrive.
    selected = rows[:limit] if after else rows[-limit:]
    return {'items':selected[::-1], 'has_more':len(rows)>limit,'latest_seq':feed.get('seq',0),
            'retention_seconds':HISTORY_TTL,'max_records':5000,'stale':is_stale(latest())}


def latest() -> dict | None:
    raw = client().get(PREFIX + "latest")
    return json.loads(raw) if raw else None


def is_stale(record: dict | None, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return (not record or now - record["received_at"] > STALE_SECONDS or
            now - record["telemetry"]["collected_at"] > STALE_SECONDS)


def overview() -> dict:
    record = latest()
    history = command_history()
    if not record:
        return {"gateway_id": GATEWAY_ID, "received_at": None, "collected_at": None,
                "stale": True, "offline": True, "runtime": {}, "summary": {}, "scans": [],
                "events": [], "actions": [], "lessons": {}, "acceptance": {}, "backups": [],
                "commands": history, "sending_enabled": False}
    body = record["telemetry"]
    data = {key: body.get(key, {} if key in ("runtime", "summary", "lessons", "acceptance") else [])
            for key in ("runtime", "summary", "scans", "events", "actions", "lessons", "acceptance", "backups")}
    stale = is_stale(record)
    data.update({"gateway_id": GATEWAY_ID, "received_at": record["received_at"],
                 "collected_at": body["collected_at"], "stale": stale, "offline": stale,
                 "commands": history, "sending_enabled": bool(not stale and getattr(settings, "WECHAT_MAA_REPLY_ENABLED", False) and body["runtime"].get("sending_enabled", False)),
                 "reply_backend_enabled": getattr(settings, "WECHAT_MAA_REPLY_ENABLED", False)})
    return data


def contacts(scope: str | None, keyword: str | None, page: int, page_size: int) -> dict:
    record = latest()
    if scope == 'group':
        rows = record['telemetry'].get('conversations', []) if record else []
        items = []
        for row in rows:
            profile = row.get('group_profile')
            if not profile: continue
            items.append({'id':row['id'],'scope':'group','ordinal':len(items)+1,
                          'display_name':profile['name'],'list_name':profile['row_name'],
                          'member_count':profile['member_count'],'identity_status':'visual_group_header',
                          'last_seen':profile['observed_at'],'avatar_asset':row.get('avatar_asset'),
                          'profile_asset':row.get('latest_snapshot',{}).get('frame_asset'), 'facts':profile})
        if keyword: items = [r for r in items if keyword.casefold() in r['display_name'].casefold()]
        return {'items':items[(page-1)*page_size:page*page_size],'total':len(items),'page':page,'page_size':page_size,'stale':is_stale(record),'coverage':'observed_only'}
    items = record["telemetry"].get("contacts", []) if record else []
    # Collector sends rows only from the latest scan of each scope. A defensive
    # filter prevents old cumulative provisional observations appearing as coverage.
    scans = record["telemetry"].get("scans", []) if record else []
    latest_scans = {}
    for scan in scans:
        key = scan["scope"]
        priority = (scan.get("status") == "complete" and scan.get("coverage_valid") is True
                    and scan.get("bottom_verified") is True,
                    scan.get("finished") or scan.get("started") or 0)
        old = latest_scans.get(key, {})
        old_priority = (old.get("status") == "complete" and old.get("coverage_valid") is True
                        and old.get("bottom_verified") is True,
                        old.get("finished") or old.get("started") or 0)
        if key not in latest_scans or priority > old_priority:
            latest_scans[key] = scan
    if latest_scans:
        items = [row for row in items if row["scan_id"] == latest_scans.get(row["scope"], {}).get("id")]
    if scope:
        items = [row for row in items if row["scope"] == scope]
    if keyword:
        needle = keyword.casefold()
        items = [row for row in items if needle in " ".join(str(row.get(key) or "")
                 for key in ("display_name", "list_name", "wechat_id")).casefold()]
    items = sorted(items, key=lambda row: (row["scope"], row["ordinal"]))
    return {"items": items[(page - 1) * page_size:page * page_size], "total": len(items),
            "page": page, "page_size": page_size, "stale": is_stale(record)}


def conversations() -> dict:
    record = latest()
    rows = record["telemetry"].get("conversations", []) if record else []
    result = []
    for row in rows:
        item = {key: value for key, value in row.items() if key not in {"messages", "latest_snapshot"}}
        if row.get("latest_snapshot"):
            item["latest_snapshot"] = {key: value for key, value in row["latest_snapshot"].items() if key != "payloads"}
        result.append(item)
    return {"items": result, "total": len(result), "stale": is_stale(record)}


def conversation_detail(cid: str) -> dict:
    if not HEX64.fullmatch(cid):
        raise not_found("会话不存在")
    record = latest()
    for row in record["telemetry"].get("conversations", []) if record else []:
        if row["id"] == cid or cid in row.get("aliases",[]):
            return {**row, "stale": is_stale(record)}
    raise not_found("会话不存在")


def upload_assets(body: dict) -> dict:
    _object(body, "assets")
    items = _list(body.get("items"), "items", 50)
    checked = []
    # Validate the complete batch before writing any object.
    for item in items:
        _object(item, "asset")
        sha = item.get("sha256")
        _asset(sha)
        if not sha:
            raise invalid("图片 SHA256 不能为空")
        encoded = _text(item.get("png_base64"), "png_base64", 4 * 1024 * 1024)
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            raise invalid("PNG base64 无效")
        if not data or len(data) > 3 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != sha:
            raise invalid("PNG 大小或 SHA256 校验失败")
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.format != "PNG" or not 1 <= image.width <= 1920 or not 1 <= image.height <= 1080:
                    raise invalid("仅允许不超过 1920×1080 的 PNG")
                image.verify()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            raise invalid("PNG 无法解码")
        checked.append((sha, data))
    if sum(len(data) for _, data in checked) > MAX_BODY:
        raise BizError(41301, "图片批次超过 8MB", 413)
    storage = get_client()
    try:
        storage.head_bucket(Bucket=ASSET_BUCKET)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in {"NoSuchBucket", "404", "NotFound"}:
            raise
        try:
            # A distinct bucket has no anonymous-read policy. Do not reuse the
            # public business-assets bucket, nor publish a presigned URL.
            storage.create_bucket(Bucket=ASSET_BUCKET)
        except ClientError as create_exc:
            if create_exc.response.get("Error", {}).get("Code") not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                raise
    for sha, data in checked:
        storage.put_object(Bucket=ASSET_BUCKET, Key=f"wechat-maa/{sha}.png",
                           Body=data, ContentType="image/png",
                           CacheControl="private, max-age=86400")
    return {"stored": [sha for sha, _ in checked]}


def asset_bytes(sha: str) -> bytes:
    if not HEX64.fullmatch(sha):
        raise not_found("图片不存在")
    try:
        obj = get_client().get_object(Bucket=ASSET_BUCKET, Key=f"wechat-maa/{sha}.png")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404", "NotFound"}:
            raise not_found("图片不存在")
        raise
    stream = obj["Body"]
    try:
        data = stream.read(3 * 1024 * 1024 + 1)
    finally:
        stream.close()
    if len(data) > 3 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != sha:
        raise BizError(50201, "图片存储校验失败", 502)
    return data


def validate_command(body: dict, *, check_enabled: bool = True) -> dict:
    _object(body, "command")
    if set(body) - {"action", "params", "request_id"}:
        raise invalid("未知指令字段")
    action = body.get("action")
    if not isinstance(action, str) or action not in ACTIONS:
        raise invalid("不支持该 Maa 指令")
    try:
        request_id = str(uuid.UUID(body["request_id"]))
    except (KeyError, ValueError, TypeError, AttributeError):
        raise invalid("request_id 必须为 UUID")
    params = _object(body.get("params", {}), "params")
    allowed = {"set_polling": {"enabled"}, "set_group_whitelist": {"conversation_ids", "expected_revision"}, "scan_contacts": {"scope", "scan_id", "reuse_scan"}, "start_watch": {"cycles", "auto_reply", "continuous"},
               'send_message': {'conversation_id','text'}, 'send_contact_message': {'contact_id','wechat_id','text'}, 'send_media': {'conversation_id','asset_id','delivery_id','receive_only_wechat_id'}, 'open_conversation': {'conversation_id'}, 'open_contact': {'contact_id'}}.get(action, set())
    if set(params) - allowed:
        raise invalid("指令参数不在白名单中")
    if action == 'set_polling' and type(params.get('enabled')) is not bool:
        raise invalid('轮询开关必须为布尔值')
    if action == "set_group_whitelist":
        ids=_list(params.get('conversation_ids'), '白名单群聊', 100)
        if any(not isinstance(cid,str) or not HEX64.fullmatch(cid) for cid in ids) or len(set(ids))!=len(ids):
            raise invalid('白名单群聊标识无效或重复')
        if not isinstance(params.get('expected_revision'),str) or not HEX64.fullmatch(params['expected_revision']):
            raise invalid('白名单版本无效，请刷新')
    if action == "scan_contacts":
        if not isinstance(params.get("scope", "personal"), str) or params.get("scope", "personal") not in SCOPES:
            raise invalid("联系人分区无效")
        if "scan_id" in params and "reuse_scan" in params:
            raise invalid("续扫和复用资料不可同时使用")
        for key in ("scan_id", "reuse_scan"):
            if key in params and (not isinstance(params[key], str) or not HEX32.fullmatch(params[key])):
                raise invalid(f"{key} 必须为 32 位小写十六进制")
        params = {"scope": params.get("scope", "personal"),
                  **{key: params[key] for key in ("scan_id", "reuse_scan") if key in params}}
    if action == "start_watch" and "cycles" in params:
        if type(params["cycles"]) is not int or not 1 <= params["cycles"] <= 1000:
            raise invalid("轮询次数必须为 1–1000")
    if action == "start_watch":
        for flag in ("auto_reply", "continuous"):
            if flag in params and type(params[flag]) is not bool:
                raise invalid("自动回复/持续运行参数必须为布尔值")
        if params.get("auto_reply") and not getattr(settings, "WECHAT_MAA_REPLY_ENABLED", False):
            raise invalid("Maa 自动回复尚未启用")
    if action in MANUAL_ACTIONS:
        if action in ('open_contact','send_contact_message'):
            if not _text(params.get('contact_id'), 'contact_id',256): raise invalid('联系人不能为空')
        elif not isinstance(params.get('conversation_id'),str) or not HEX64.fullmatch(params['conversation_id']):
            raise invalid('会话标识无效')
        if action=='send_media':
            if type(params.get('asset_id')) is not int or params['asset_id'] <= 0: raise invalid('素材ID无效')
            if 'receive_only_wechat_id' in params and (not isinstance(params['receive_only_wechat_id'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}',params['receive_only_wechat_id'])):
                raise invalid('单次媒体授权必须提供准确微信号')
            if params.get('delivery_id') and not HEX32.fullmatch(params['delivery_id']): raise invalid('交付ID无效')
            if not getattr(settings,'WECHAT_MAA_CREATION_ENABLED',False): raise invalid('微信媒体发送未启用')
        if action=='send_contact_message':
            if not _text(params.get('wechat_id'), 'wechat_id',256): raise invalid('微信号不能为空')
            text=_text(params.get('text'),'私聊消息',400)
            if not text.strip() or any(unicodedata.category(c) in {'Cc','Zl','Zp'} for c in text): raise invalid('私聊消息不能为空或包含换行、制表符、控制字符')
        if action in ('send_message','send_contact_message'):
            text=_text(params.get('text'),'手动消息',400)
            if not text.strip() or any(ord(c)<32 and c not in '\n\t' for c in text): raise invalid('手动消息不能为空或包含控制字符')
            if check_enabled and not getattr(settings,'WECHAT_MAA_REPLY_ENABLED',False): raise invalid('Maa发送未启用')
    return {"action": action, "params": params, "request_id": request_id}


def _validate_scan_reference(params: dict, telemetry: dict) -> None:
    """Reuse only verified coverage; resume only the current incomplete scan.

    Client ids are selectors into authenticated evidence, never trusted proof
    of a resumable/reusable scan. Windows rechecks its DB before execution.
    """
    rows = [row for row in telemetry.get("scans", []) if row.get("scope") == params["scope"]]
    if "reuse_scan" in params:
        selected = next((row for row in rows if row["id"] == params["reuse_scan"]), None)
        if (not selected or selected.get("status") != "complete" or
                selected.get("coverage_valid") is not True or selected.get("bottom_verified") is not True):
            raise conflict("复用资料必须来自同分区已完整验证的扫描")
    if "scan_id" in params:
        newest = max(rows, key=lambda row: row.get("finished") or row.get("started") or 0, default=None)
        if (not newest or newest["id"] != params["scan_id"] or
                newest.get("status") != "incomplete" or not (newest.get("position_count") or 0) > 0):
            raise conflict("仅可续扫同分区最新且已有定位记录的未完成扫描")


def _command_key(cid: str) -> str:
    return PREFIX + "command:" + cid


def _command_id(cid: str) -> None:
    if not HEX32.fullmatch(cid):
        raise not_found("指令不存在")


def enqueue(body: dict, user_id: int | None, *, api_request: dict | None = None) -> tuple[dict, bool]:
    body = validate_command(body, check_enabled=api_request is None)
    cache = client()
    dedupe_key = f"{PREFIX}request:{'send-api' if api_request is not None else user_id}:{body['request_id']}"
    latest_key = PREFIX + "latest"
    queue_key = PREFIX + "queue"
    with cache.pipeline() as pipe:
        for _ in range(5):
            try:
                pipe.watch(dedupe_key, latest_key, queue_key)
                previous = pipe.get(dedupe_key)
                if previous:
                    existing = json.loads(pipe.get(_command_key(previous)) or "null")
                    if existing:
                        same = (existing.get('source') == 'send-api' and existing.get('api_request') == api_request
                                if api_request is not None else existing["action"] == body["action"] and existing["params"] == body["params"])
                        if not same:
                            raise conflict("相同 request_id 不可用于不同指令")
                        return existing, False
                if api_request is not None and not getattr(settings,'WECHAT_MAA_REPLY_ENABLED',False):
                    raise BizError(50332, 'Maa 发送未启用', 503)
                raw = pipe.get(latest_key)
                record = json.loads(raw) if raw else None
                if is_stale(record):
                    if api_request is not None: raise BizError(50330, 'Windows Maa 离线或遥测已过期', 503)
                    raise conflict("Windows Maa 采集端离线或数据已过期")
                if api_request is not None:
                    from wemaa.services.api import resolve_recipient
                    contact = resolve_recipient(record, api_request['recipient'])
                    body = {**body, 'params': {'contact_id':contact['id'], 'wechat_id':contact['wechat_id'], 'text':api_request['text']}}
                if body["action"] == "scan_contacts":
                    _validate_scan_reference(body["params"], record["telemetry"])
                runtime = record["telemetry"]["runtime"]
                if body['action']=='set_group_whitelist':
                    policy=runtime.get('group_whitelist') or {}
                    if policy.get('revision')!=body['params']['expected_revision']:
                        raise conflict('群聊白名单已变更，请刷新后保存')
                    known={r['id'] for r in record['telemetry'].get('conversations',[])
                           if r.get('group_profile',{}).get('kind')=='group' and r.get('manual_target_observed') is True}
                    if any(cid not in known for cid in body['params']['conversation_ids']):
                        raise conflict('只能添加已识别且已建立目标路径的群聊')
                if body['action'] in MANUAL_ACTIONS:
                    if str(runtime.get('worker_state','')).casefold()!='running' or str(runtime.get('trial_state','')).casefold()=='running' or runtime.get('progress',{}).get('manual_commands_enabled') is not True:
                        if api_request is not None: raise BizError(50331, 'Maa 手动执行器不可用', 503)
                        raise conflict('支持手动操作的Maa工作任务未就绪')
                    if body['action'] in ('open_contact','send_contact_message'):
                        target=next((r for r in record['telemetry'].get('contacts',[]) if r['id']==body['params']['contact_id']),None)
                        if not target or not target.get('wechat_id'): raise conflict('联系人未取得可核验资料，不能仅按昵称跳转')
                        if body['action']=='send_contact_message' and (target.get('scope')!='personal' or target.get('wechat_id')!=body['params']['wechat_id']):
                            raise conflict('仅可发送给微信号匹配的个人联系人')
                    else:
                        target=next((r for r in record['telemetry'].get('conversations',[]) if (r['id']==body['params']['conversation_id'] or body['params']['conversation_id'] in r.get('aliases',[]))),None)
                        if not target or target.get('manual_target_observed') is not True: raise conflict('尚未建立该会话的Maa目标路径')
                busy = any(str(runtime.get(key, "")).casefold() == "running" for key in ("worker_state", "trial_state"))
                if busy and body["action"] not in {"stop_worker", "checkpoint", "set_group_whitelist", "set_polling"} | MANUAL_ACTIONS:
                    raise conflict("桌面采集任务正在运行，请先停止任务")
                # Only one unclaimed command at a time; a checkpoint and stop are
                # not allowed to race a queued start/capture/scan.
                for queued_id in pipe.lrange(queue_key, 0, -1):
                    queued_raw = pipe.get(_command_key(queued_id))
                    queued = json.loads(queued_raw) if queued_raw else None
                    if queued and queued["status"] == "queued" and queued["expires_at"] > time.time():
                        raise conflict("已有指令等待 Windows 执行")
                for previous_id in pipe.lrange(PREFIX + "history", 0, 19):
                    previous_raw = pipe.get(_command_key(previous_id))
                    previous_row = json.loads(previous_raw) if previous_raw else None
                    if (previous_row and previous_row["status"] in {"queued", "issued"}
                            and previous_row["expires_at"] > time.time()):
                        raise conflict("已有指令已下发，等待 Windows 结果；不会自动重发")
                now, cid = time.time(), uuid.uuid4().hex
                command = {"id": cid, "action": body["action"], "params": body["params"],
                           "request_id": body["request_id"], "run_id": "maa-web-" + cid[:16],
                           "created_at": now, "expires_at": now + (300 if body['action'] in MANUAL_ACTIONS else COMMAND_TTL),
                           "requested_by": user_id, "status": "queued"}
                if api_request is not None:
                    command.update(source='send-api', api_request=api_request)
                pipe.multi()
                pipe.set(_command_key(cid), dumps(command), ex=HISTORY_TTL)
                pipe.set(dedupe_key, cid, ex=HISTORY_TTL)
                pipe.rpush(queue_key, cid)
                pipe.expire(queue_key, HISTORY_TTL)
                pipe.lpush(PREFIX + "history", cid)
                pipe.ltrim(PREFIX + "history", 0, 199)
                pipe.expire(PREFIX + "history", HISTORY_TTL)
                pipe.execute()
                return command, True
            except redis.WatchError:
                continue
    raise conflict("指令排队并发冲突，请稍后重试")


def next_command() -> dict | None:
    cache = client()
    queue = PREFIX + "queue"
    # Atomic LPOP means an uncertain HTTP response cannot cause redelivery.
    for _ in range(200):
        cid = cache.lpop(queue)
        if not cid:
            return None
        key = _command_key(cid)
        with cache.pipeline() as pipe:
            for _retry in range(5):
                try:
                    pipe.watch(key)
                    raw = pipe.get(key)
                    row = json.loads(raw) if raw else None
                    if not row or row["status"] != "queued":
                        break
                    now = time.time()
                    expired = row["expires_at"] <= now
                    row.update({"status": "expired" if expired else "issued", "issued_at": None if expired else now})
                    pipe.multi()
                    pipe.set(key, dumps(row), ex=HISTORY_TTL)
                    pipe.execute()
                    if not expired:
                        return row
                    break
                except redis.WatchError:
                    continue
    return None


def record_result(cid: str, body: dict) -> dict:
    _command_id(cid)
    _object(body, "result")
    _tree(body)
    if len(dumps(body).encode()) > MAX_RESULT:
        raise BizError(41301, "指令结果超过 64KB", 413)
    if (set(body) - {"status", "detail"} or not isinstance(body.get("status"), str) or
            body.get("status") not in {"succeeded", "failed"}):
        raise invalid("指令结果状态无效")
    _object(body.get("detail", {}), "detail")
    key = _command_key(cid)
    cache = client()
    with cache.pipeline() as pipe:
        for _ in range(5):
            try:
                pipe.watch(key)
                raw = pipe.get(key)
                if not raw:
                    raise not_found("指令不存在")
                row = json.loads(raw)
                if row["status"] in {"succeeded", "failed"}:
                    if row["status"] == body["status"] and row.get("detail", {}) == body.get("detail", {}):
                        return row
                    raise conflict("已完成指令结果不可改写")
                if row["status"] != "issued":
                    raise conflict("指令尚未下发或已过期")
                row.update({"status": body["status"], "detail": body.get("detail", {}), "finished_at": time.time()})
                pipe.multi()
                pipe.set(key, dumps(row), ex=HISTORY_TTL)
                pipe.execute()
                return row
            except redis.WatchError:
                continue
    raise conflict("指令结果并发冲突，请稍后重试")


def command_history() -> list:
    cache = client()
    keys = cache.lrange(PREFIX + "history", 0, 19)
    if not keys:
        return []
    rows = cache.mget([_command_key(cid) for cid in keys])
    result = []
    for raw in rows:
        if not raw:
            continue
        row = json.loads(raw)
        if row["status"] == "queued" and row["expires_at"] <= time.time():
            row["status"] = "expired"
        result.append(row)
    return result
