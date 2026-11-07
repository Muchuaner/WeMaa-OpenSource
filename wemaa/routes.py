"""WeMaa 原生路由，与原版 Dashboard 共用同一 FastAPI 和登录态。"""
from typing import Literal

import json
import logging
import secrets
import time

import httpx
from fastapi import APIRouter, Depends, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy import select

from wemaa.config import settings
from wemaa.database import get_db
from wemaa.errors import BizError, forbidden, unauthorized
from wemaa.identity import IdentityRegistry
from wemaa.models import MediaAsset, WechatDelivery, WechatSchedule
from wemaa.plugins import website
from wemaa.runtime import runtime
from wemaa.services import maa, media, schedules, greetings, replies


def ok(data):
    return {'code': 0, 'message': 'success', 'data': data}


async def bounded_json(request: Request, limit: int = maa.MAX_BODY) -> dict:
    length = request.headers.get('content-length')
    if length:
        if not length.isdigit():
            raise maa.invalid('Content-Length 无效')
        if int(length) > limit:
            raise BizError(41301, '请求体超过大小限制', 413)
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > limit:
            raise BizError(41301, '请求体超过大小限制', 413)
        raw.extend(chunk)
    try:
        return maa._object(json.loads(raw), '请求体')
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise maa.invalid('JSON 无效') from None


def machine_auth(request: Request):
    maa.authenticate(request.headers.get('authorization', ''))


async def admin_auth(request: Request):
    from src.webui.core.auth import get_current_token
    get_current_token(request.cookies.get('maibot_session'))


admin = APIRouter(dependencies=[Depends(admin_auth)])
machine = APIRouter(dependencies=[Depends(machine_auth)])
session_router = APIRouter()
model_router = APIRouter()


@model_router.post('/chat/completions')
async def model_completion(request: Request):
    actual = request.headers.get('authorization','').removeprefix('Bearer ')
    if not settings.SUPPORT_BRAIN_ADAPTER_TOKEN or not secrets.compare_digest(actual,settings.SUPPORT_BRAIN_ADAPTER_TOKEN):
        raise unauthorized('模型网关认证失败')
    body = await bounded_json(request,2*1024*1024)
    if body.get('model') == 'wemaa-vlm':
        from wemaa.model_gateway import multimodal
        return await multimodal(body,'chat/completions')
    async with httpx.AsyncClient(timeout=52,follow_redirects=False) as client:
        response = await client.post(settings.WEMAA_WEBSITE_URL.rstrip('/')+'/internal/support-brain/v1/chat/completions',
            headers={'Authorization':'Bearer '+settings.SUPPORT_MODEL_PROXY_TOKEN},json=body)
    return Response(response.content,status_code=response.status_code,media_type='application/json')


@model_router.post('/embeddings')
async def embeddings(request: Request):
    actual=request.headers.get('authorization','').removeprefix('Bearer ')
    if not settings.SUPPORT_BRAIN_ADAPTER_TOKEN or not secrets.compare_digest(actual,settings.SUPPORT_BRAIN_ADAPTER_TOKEN):
        raise unauthorized('模型网关认证失败')
    from wemaa.model_gateway import multimodal
    return await multimodal(await bounded_json(request,2*1024*1024),'embeddings')


@model_router.get('/models')
async def model_list(request: Request):
    actual = request.headers.get('authorization','').removeprefix('Bearer ')
    if not settings.SUPPORT_BRAIN_ADAPTER_TOKEN or not secrets.compare_digest(actual,settings.SUPPORT_BRAIN_ADAPTER_TOKEN):
        raise unauthorized('模型网关认证失败')
    return {'object':'list','data':[{'id':'website-support-'+role,'object':'model','owned_by':'wemaa'}
                                   for role in ('planner','replyer','utils')]+[
                                       {'id':alias,'object':'model','owned_by':'wemaa'} for alias in ('wemaa-vlm','wemaa-embedding')]}


@session_router.post('/session')
async def session(request: Request, response: Response):
    """同站管理员 SSO；后端重新验证权限，浏览器不接收核心令牌正文。"""
    if not website.configured():
        raise unauthorized('请使用 WeMaa 登录')
    authorization = request.headers.get('authorization', '')
    if not authorization.startswith('Bearer '):
        raise unauthorized()
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        upstream = await client.get(settings.WEMAA_WEBSITE_URL.rstrip('/') + '/api/v1/auth/me',
                                    headers={'Authorization': authorization})
    if upstream.status_code != 200:
        raise unauthorized()
    user = upstream.json().get('data') or {}
    permissions = user.get('permissions') or []
    if '*' not in permissions or user.get('id') != settings.WEMAA_ADMIN_USER_ID:
        raise forbidden('WeMaa 需要站点所有者管理员权限')
    from src.webui.core.security import get_token_manager
    response.set_cookie('maibot_session', get_token_manager().get_token(), httponly=True,
                        secure=request.headers.get('x-forwarded-proto') == 'https' or request.url.scheme == 'https',
                        samesite='lax', path='/', max_age=86400)
    return ok({'authenticated': True})


@admin.get('')
def overview():
    return ok({**maa.overview(), 'wemaa': runtime.status()})


@admin.get('/identity')
def identities():
    registry = IdentityRegistry()
    return ok({'items': registry.items(), **registry.summary()})


@admin.post('/identity/sync')
async def sync_identity():
    return ok(await runtime.call(runtime.sync_contacts()))


@admin.get('/contacts')
def contacts(scope: Literal['personal','group','enterprise','public'] | None = None,
             keyword: str | None = None, page: int = 1, page_size: int = 30):
    if page < 1 or not 1 <= page_size <= 100:
        raise maa.invalid('分页参数无效')
    result = maa.contacts(scope, keyword, page, page_size)
    mapping = {r['contact_id']: r for r in IdentityRegistry().items()}
    result['items'] = [{**r, 'core_identity': mapping.get(r['id'])} for r in result['items']]
    return ok(result)


@admin.get('/conversations')
def conversations():
    return ok(maa.conversations())


@admin.get('/conversations/{cid}')
def conversation(cid: str):
    return ok(maa.conversation_detail(cid))


@admin.get('/activity')
def activity(after: int = 0, before: int = 0, limit: int = 100,
             category: Literal['all','messages','contacts','polling','actions'] = 'all'):
    if after < 0 or before < 0 or not 1 <= limit <= 200:
        raise maa.invalid('日志游标无效')
    return ok(maa.activity(after, before, limit, category))


@admin.get('/assets/{sha}')
def evidence(sha: str):
    return Response(maa.asset_bytes(sha), media_type='image/png',
                    headers={'Cache-Control': 'private, max-age=86400', 'X-Content-Type-Options': 'nosniff'})


@admin.post('/commands')
async def command(request: Request, db=Depends(get_db)):
    body = maa.validate_command(await bounded_json(request, maa.MAX_RESULT))
    if body['action'] == 'send_media':
        params = body['params']
        if 'delivery_id' in params:
            raise maa.invalid('交付标识由服务端分配')
        row = media.add_delivery(db, 'admin:' + body['request_id'], params['conversation_id'],
            user_id=settings.WEMAA_ADMIN_USER_ID, asset_id=params['asset_id'],
            receive_only_wechat_id=params.get('receive_only_wechat_id'))
        db.commit()
        return ok(media.delivery_payload(db, row))
    row, _ = await run_in_threadpool(maa.enqueue, body, settings.WEMAA_ADMIN_USER_ID)
    return ok(row)


@admin.get('/commands/{cid}')
def command_status(cid: str, db=Depends(get_db)):
    maa._command_id(cid)
    delivery = db.get(WechatDelivery, cid)
    if delivery:
        return ok(media.delivery_payload(db, delivery))
    raw = maa.client().get(maa._command_key(cid))
    if not raw:
        raise maa.not_found('指令不存在')
    return ok(json.loads(raw))


@admin.get('/schedules')
def list_schedules(deleted: bool = False, db=Depends(get_db)):
    schedules.sync_results(db)
    rows = db.scalars(select(WechatSchedule).where(
        WechatSchedule.deleted_at.is_not(None) if deleted else WechatSchedule.deleted_at.is_(None))
        .order_by(WechatSchedule.created_at.desc()).limit(200)).all()
    db.commit()
    return ok({'items': [schedules.serialize(row) for row in rows]})


@admin.post('/schedules')
@admin.put('/schedules/{sid}')
async def save_schedule(request: Request, sid: str | None = None, db=Depends(get_db)):
    row = schedules.save(db, await bounded_json(request, maa.MAX_RESULT), settings.WEMAA_ADMIN_USER_ID, sid)
    db.commit()
    return ok(schedules.serialize(row))


@admin.post('/schedules/{sid}/{action}')
async def schedule_action(sid: str, action: Literal['cancel','delete','restore'], request: Request, db=Depends(get_db)):
    body = await bounded_json(request, 4096)
    if action == 'restore':
        row = db.scalar(select(WechatSchedule).where(WechatSchedule.id == sid).with_for_update())
        if not row or not row.deleted_at:
            raise maa.not_found('回收站任务不存在')
        if row.revision != body.get('revision'):
            raise maa.conflict('任务已修改，请刷新')
        row.deleted_at = None
        row.revision += 1
    else:
        row = (schedules.cancel if action == 'cancel' else schedules.delete)(db, sid, body.get('revision'))
    db.commit()
    return ok(schedules.serialize(row))


@admin.get('/media-options')
def media_options(db=Depends(get_db)):
    rows = db.scalars(select(MediaAsset).where(MediaAsset.user_id == settings.WEMAA_ADMIN_USER_ID)
                      .order_by(MediaAsset.created_at.desc()).limit(500))
    native=[{key: getattr(row, key) for key in ('id','type','name','size','mime')} for row in rows]
    seen={row['id'] for row in native}
    native.extend({key:row[key] for key in ('id','type','name','size','mime')}
                  for row in website.assets(db,settings.WEMAA_ADMIN_USER_ID) if row['id'] not in seen)
    return ok(native)


@admin.post('/media')
async def upload_media(file: UploadFile, db=Depends(get_db)):
    raw = await file.read(media.MAX_MEDIA + 1)
    row = media.store_asset(db, settings.WEMAA_ADMIN_USER_ID, raw, file.filename or 'media', file.content_type or '')
    db.commit()
    return ok({key: getattr(row, key) for key in ('id','type','name','size','mime')})


@admin.get('/replies')
def reply_history():
    return ok(replies.history())


@admin.get('/greetings')
def greeting_status():
    return ok(greetings.overview())


@admin.post('/greetings/control')
async def greeting_control(request: Request):
    return ok(await run_in_threadpool(greetings.control, await bounded_json(request, 4096)))


@admin.get('/plugins/website')
def website_status():
    return ok(website.status())


@admin.get('/native-api')
def native_api_status():
    token = settings.WECHAT_MAA_API_TOKEN
    return ok({'enabled': len(token) >= 32 and token != settings.WECHAT_MAA_TOKEN,
        'base_path': '/api/v1/wechat-maa-api', 'authentication': 'Bearer',
        'endpoints': [
            {'method':'GET','path':'/contacts','description':'核验联系人及分页搜索'},
            {'method':'POST','path':'/messages','description':'幂等私聊发送'},
            {'method':'GET','path':'/messages/{message_id}','description':'真实发送回执'},
            {'method':'GET','path':'/received-messages','description':'已核验原文与增量游标'},
            {'method':'GET','path':'/messages/{message_id}/replies','description':'同一好友后续来信'}]})


@machine.put('/telemetry')
async def telemetry(request: Request):
    body = await bounded_json(request)
    result = await run_in_threadpool(maa.store_telemetry, body)
    # 构造器也执行 SQLite schema/WAL 检查，不能先在 ASGI 事件循环内构造。
    await run_in_threadpool(lambda: IdentityRegistry().sync(body))
    return ok(result)


@machine.post('/assets')
async def assets(request: Request):
    return ok(await run_in_threadpool(maa.upload_assets, await bounded_json(request)))


@machine.get('/commands/next')
def next_command(db=Depends(get_db)):
    schedules.dispatch_due(db)
    return ok(maa.next_command())


@machine.put('/commands/{cid}/result')
async def result(cid: str, request: Request):
    return ok(await run_in_threadpool(maa.record_result, cid, await bounded_json(request, maa.MAX_RESULT)))


@machine.post('/reply/prepare')
async def prepare_reply(request: Request):
    return ok(await replies.prepare(await bounded_json(request, replies.MAX_BODY)))


@machine.post('/reply/{eid}/claim')
async def claim_reply(eid: str, request: Request):
    return ok(await run_in_threadpool(replies.claim, eid, await bounded_json(request, 4096)))


@machine.get('/reply/{eid}/status')
def serial_reply_status(eid: str):
    return ok(replies.core_status(eid))


@machine.put('/reply/{eid}/result')
async def reply_result(eid: str, request: Request):
    return ok(await run_in_threadpool(replies.result, eid, await bounded_json(request, 4096)))


@machine.get('/greetings/plan')
def greeting_plan():
    return ok(greetings.plan())


@machine.post('/greetings/prepare')
async def greeting_prepare(request: Request):
    return ok(await greetings.prepare(await bounded_json(request, replies.MAX_BODY)))


@machine.post('/greetings/navigation')
async def greeting_navigation(request: Request):
    return ok(greetings.navigation(await bounded_json(request, 4096)))


@machine.get('/greetings/preflight/{eid}')
def greeting_preflight(eid: str):
    row = json.loads(replies.client().get(replies.event_key(eid)) or 'null')
    return ok({'permit': bool(row and row.get('greeting') and row['status'] == 'sending'
                             and not greetings.reason(row, greetings.state()))})


@machine.get('/experiment/plan')
def retired_plan():
    return ok({'enabled': False, 'id': 'xiaozhou-v1', 'items': [], 'retired': True})


async def website_response(request: Request, suffix: str, body=None):
    response = await website.request(request.method, '/api/v1/wechat-maa/creation/' + suffix, body=body)
    headers = {k: v for k, v in response.headers.items() if k.lower() in ('content-type','content-length','x-content-sha256')}
    return Response(response.content, status_code=response.status_code, headers=headers)


@machine.get('/creation/deliveries')
async def deliveries(db=Depends(get_db)):
    items = await run_in_threadpool(media.next_deliveries, db)
    if website.configured():
        try:
            response = await website.request('GET', '/api/v1/wechat-maa/creation/deliveries')
            response.raise_for_status()
            items.extend(response.json()['data']['items'])
        except (httpx.HTTPError,KeyError,ValueError):
            logging.getLogger('wemaa.website').warning('网站素材交付暂不可用，原生交付继续运行')
    return ok({'items': items})


@machine.get('/creation/deliveries/{did}/media')
async def delivery_media(did: str, request: Request, db=Depends(get_db)):
    row = db.get(WechatDelivery, did)
    if not row:
        return await website_response(request, f'deliveries/{did}/media')
    raw, mime, sha = media.media_bytes(db, row)
    return Response(raw, media_type=mime, headers={'X-Content-SHA256': sha, 'Cache-Control': 'no-store'})


@machine.post('/creation/deliveries/{did}/claim')
async def delivery_claim(did: str, request: Request, db=Depends(get_db)):
    body = await bounded_json(request, 256)
    if set(body)-{'claim_token'} or ('claim_token' in body and not isinstance(body['claim_token'],str)):
        raise maa.invalid('发送许可格式无效')
    if not db.get(WechatDelivery, did):
        return await website_response(request, f'deliveries/{did}/claim', body)
    return ok(await run_in_threadpool(media.claim_delivery, db, did, body.get('claim_token')))


@machine.get('/creation/deliveries/{did}/preflight')
async def delivery_preflight(did: str, request: Request, db=Depends(get_db)):
    row = db.get(WechatDelivery, did)
    if not row:
        return await website_response(request, f'deliveries/{did}/preflight')
    media.allowed_delivery(db, row)
    return ok({'permit': row.status == 'sending'})


@machine.put('/creation/deliveries/{did}/result')
async def delivery_result(did: str, request: Request, db=Depends(get_db)):
    body = await bounded_json(request, 8192)
    if not db.get(WechatDelivery, did):
        return await website_response(request, f'deliveries/{did}/result', body)
    return ok(media.delivery_result(db, did, body))


@machine.post('/creation/{path:path}')
async def website_event(path: str, request: Request):
    # 仅保留已有绑定/创作操作；不暴露任意网站 URL 转发。
    import re
    if path != 'events' and not re.fullmatch(r'deliveries/[a-f0-9]{32}/binding-link', path):
        raise maa.not_found()
    return await website_response(request, path, await bounded_json(request, replies.MAX_BODY))


@machine.get('/creation/events/{eid}/status')
def creation_event_status(eid: str, conversation_id: str, db=Depends(get_db)):
    from wemaa.creation_turns import status
    return ok(status(db, eid, conversation_id))


def register_routes(app):
    from wemaa.api_routes import router as api

    @app.exception_handler(BizError)
    async def business_error(request, exc):
        return JSONResponse(status_code=exc.status_code, content={**exc.detail, 'data': None})

    app.include_router(session_router, prefix='/api/wemaa')
    app.include_router(model_router, prefix='/api/wemaa-model/v1', tags=['Model gateway'])
    app.include_router(admin, prefix='/api/wemaa', tags=['WeMaa'])
    app.include_router(admin, prefix='/api/v1/admin/support/wechat-maa', include_in_schema=False)
    app.include_router(machine, prefix='/api/v1/wechat-maa', tags=['Maa adapter'])
    app.include_router(api, prefix='/api/v1/wechat-maa-api', tags=['Native API'])
