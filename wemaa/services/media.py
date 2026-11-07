"""原生媒体存储和一次性交付；Maa 仍是唯一桌面执行者。"""
from pathlib import Path

import hashlib
import io
import json
import math
import secrets
import subprocess
import tempfile
import time

from PIL import Image
from sqlalchemy import select, update

from wemaa.config import settings
from wemaa.errors import conflict, not_found
from wemaa.models import MediaAsset, WechatDelivery
from wemaa.services import maa

MAX_MEDIA = 100 * 1024 * 1024
TERMINAL = {'sent', 'failed', 'uncertain', 'cancelled', 'expired'}


def store_asset(db, user_id: int, raw: bytes, name: str, mime: str, *, asset_id: int | None = None) -> MediaAsset:
    if not 0 < len(raw) <= MAX_MEDIA:
        raise maa.invalid('素材需在100MB以内')
    if mime.startswith('image/'):
        try:
            with Image.open(io.BytesIO(raw)) as im:
                mime = Image.MIME.get(im.format, '')
                if im.width * im.height > 40_000_000:
                    raise maa.invalid('图片尺寸过大')
                im.verify()
            with Image.open(io.BytesIO(raw)) as im:
                im.load()
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            raise maa.invalid('图片不能完整解码') from exc
        if mime not in ('image/png', 'image/jpeg', 'image/webp'):
            raise maa.invalid('图片支持PNG、JPEG、WebP')
        kind = 'image'
    elif mime == 'video/mp4' and len(raw) >= 12 and raw[4:8] == b'ftyp':
        with tempfile.NamedTemporaryFile(suffix='.mp4') as probe:
            probe.write(raw)
            probe.flush()
            try:
                result=subprocess.run(['ffprobe','-v','error','-protocol_whitelist','file,pipe',
                    '-show_entries','stream=codec_type,width,height:format=duration','-of','json',probe.name],
                    capture_output=True,timeout=15,check=True)
                info=json.loads(result.stdout)
                duration=float(info.get('format',{}).get('duration',0))
                streams=[s for s in info.get('streams',[]) if s.get('codec_type')=='video']
                if not math.isfinite(duration) or duration <= 0 or not streams or any(
                        not 0<s.get('width',0)*s.get('height',0)<=40_000_000 for s in streams):
                    raise ValueError('VIDEO_METADATA_INVALID')
            except (OSError,ValueError,subprocess.SubprocessError) as exc:
                raise maa.invalid('视频不能完整读取，或校验工具不可用') from exc
        kind = 'video'
    else:
        raise maa.invalid('仅支持图片或MP4视频')
    sha = hashlib.sha256(raw).hexdigest()
    folder = settings.WEMAA_DATA_DIR / 'media'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / sha
    if not path.exists():
        temporary = folder / (sha + '.' + secrets.token_hex(6) + '.tmp')
        temporary.write_bytes(raw)
        temporary.replace(path)
    row = MediaAsset(id=asset_id or int(time.time() * 1000000) + secrets.randbelow(1000), user_id=user_id,
        name=Path(name).name[:256], type=kind, mime=mime, size=len(raw), sha256=sha)
    db.add(row)
    db.flush()
    return row


def owner_asset(db, user_id, aid):
    row = db.get(MediaAsset, aid)
    if row is None:
        from wemaa.plugins.website import assets
        from wemaa.storage import get_client
        legacy=assets(db,user_id,aid)
        if legacy:
            value=legacy[0]
            if not 0 < value['size'] <= MAX_MEDIA:
                raise maa.invalid('原素材超过大小限制')
            stream=get_client().get_object(Bucket=settings.WEMAA_WEBSITE_ASSET_BUCKET,Key=value['storage_key'])['Body']
            try:
                raw=stream.read(MAX_MEDIA+1)
            finally:
                stream.close()
            if len(raw)!=value['size']:
                raise conflict('原素材长度与账本不一致')
            row=store_asset(db,user_id,raw,value['name'],value['mime'] or '',asset_id=aid)
    if not row or row.user_id != user_id:
        raise not_found('素材不存在或不属于当前用户')
    return row


def add_delivery(db, key, cid, *, user_id=None, asset_id=None, message='', binding=None,
                 receive_only_wechat_id=None):
    from wemaa.services.schedules import target
    previous = db.scalar(select(WechatDelivery).where(WechatDelivery.request_key == key))
    if previous:
        previous_consent = (previous.detail or {}).get('operator_consent', {})
        if ((previous.conversation_id, previous.asset_id, previous.user_id, previous.text) != (cid, asset_id, user_id, message)
                or previous_consent.get('wechat_id') != receive_only_wechat_id):
            raise conflict('相同请求不可改变目标或素材')
        return previous
    if not settings.WECHAT_MAA_REPLY_ENABLED or not settings.WECHAT_MAA_CREATION_ENABLED:
        raise conflict('WeMaa 发送未启用')
    asset = owner_asset(db, user_id, asset_id) if asset_id else None
    if receive_only_wechat_id is not None:
        from wemaa.identity import IdentityRegistry
        import uuid
        try:
            request_id = str(uuid.UUID(key.removeprefix('admin:')))
            identity = IdentityRegistry().resolve(cid)
        except (ValueError, TypeError, AttributeError):
            raise conflict('单次媒体授权缺少已核验私聊身份') from None
        if (key != 'admin:' + request_id or user_id != settings.WEMAA_ADMIN_USER_ID or not asset
                or identity['wechat_id'] != receive_only_wechat_id):
            raise conflict('单次媒体授权与收件人或管理员请求不一致')
    row = WechatDelivery(request_key=key, conversation_id=cid, user_id=user_id,
        asset_id=asset_id, text=message, target=target(cid), status='ready', detail={})
    db.add(row)
    db.flush()
    if receive_only_wechat_id is not None:
        row.detail = {'operator_consent': {'kind': 'single_media', 'request_id': request_id,
            'delivery_id': row.id, 'conversation_id': cid, 'wechat_id': receive_only_wechat_id,
            'asset_id': asset.id, 'sha256': asset.sha256, 'expires_at': row.created_at + 600}}
        db.flush()
    return row


def allowed_delivery(db, row):
    from wemaa.services.schedules import same_target, target
    if not row:
        raise not_found('媒体交付不存在')
    if row.status in TERMINAL:
        raise conflict('媒体交付已终结')
    if not settings.WECHAT_MAA_REPLY_ENABLED or not settings.WECHAT_MAA_CREATION_ENABLED:
        raise conflict('WeMaa 发送未启用')
    if time.time() - row.created_at > 600:
        raise conflict('媒体交付已过期')
    if not same_target(target(row.conversation_id), row.target):
        raise conflict('目标身份或现场路由已变化')
    return row


def delivery_payload(db, row):
    asset = owner_asset(db, row.user_id, row.asset_id) if row.asset_id else None
    data = {key: getattr(row, key) for key in ('id','conversation_id','status','text','asset_id','created_at','detail')}
    if asset:
        data.update({key: getattr(asset, key) for key in ('type','size','mime','name')})
    if row.status in {'ready','sending'} and row.detail.get('operator_consent'):
        data['operator_consent'] = row.detail['operator_consent']
    return data


def next_deliveries(db):
    result = []
    for row in db.scalars(select(WechatDelivery).where(WechatDelivery.status.in_(['ready','sending']))
                          .order_by(WechatDelivery.created_at).limit(100)):
        if time.time() - row.created_at > 600:
            row.status = 'uncertain' if row.status == 'sending' else 'expired'
            row.detail = {'reason': 'DELIVERY_EXPIRED_NO_REPLAY'}
        elif row.status == 'ready':
            try:
                allowed_delivery(db, row)
                result.append(delivery_payload(db, row))
            except Exception as exc:
                if not isinstance(exc, maa.BizError):
                    raise
                # 暂时离线不终结任务，但绝不绕过发送前的再次核验。
        if len(result) >= 20:
            break
    db.commit()
    return result


def media_bytes(db, row):
    allowed_delivery(db, row)
    asset = owner_asset(db, row.user_id, row.asset_id)
    raw = (settings.WEMAA_DATA_DIR / 'media' / asset.sha256).read_bytes()
    if len(raw) != asset.size or hashlib.sha256(raw).hexdigest() != asset.sha256:
        raise conflict('媒体完整性核验失败')
    row.sha256, row.mime, row.size = asset.sha256, asset.mime, asset.size
    db.commit()
    return raw, row.mime, row.sha256


def claim_delivery(db, did, claim_token=None):
    row = db.get(WechatDelivery, did)
    if not row:
        raise not_found('媒体交付不存在')
    if claim_token is not None and (not isinstance(claim_token,str) or len(claim_token)!=32 or any(c not in '0123456789abcdef' for c in claim_token)):
        raise maa.invalid('发送许可标识无效')
    token_hash = hashlib.sha256(claim_token.encode()).hexdigest() if claim_token else None
    if row.status != 'ready':
        if row.status=='sending' and token_hash and (row.detail or {}).get('claim_token_sha256')==token_hash:
            allowed_delivery(db, row)
            return {'permit': True, 'id': did, 'resumed': True}
        return {'permit': False, 'status': row.status}
    allowed_delivery(db, row)
    if row.asset_id and not row.sha256:
        raise conflict('尚未核验媒体下载')
    changed = db.execute(update(WechatDelivery).where(WechatDelivery.id == did, WechatDelivery.status == 'ready')
                         .values(status='sending', updated_at=time.time(), detail={**(row.detail or {}), **({'claim_token_sha256':token_hash} if token_hash else {})})).rowcount
    db.commit()
    return {'permit': changed == 1, 'id': did}


def delivery_result(db, did, body):
    row = db.scalar(select(WechatDelivery).where(WechatDelivery.id == did).with_for_update())
    if not row:
        raise not_found('媒体交付不存在')
    body = {key: value for key, value in body.items() if key != 'operator_consent'}
    if (row.detail or {}).get('operator_consent'):
        body['operator_consent'] = row.detail['operator_consent']
    status, evidence = body.get('status'), body.get('evidence') or {}
    if not isinstance(evidence,dict):
        raise maa.invalid('媒体证据格式无效')
    if status not in {'sent','failed','uncertain'}:
        raise maa.invalid('回执状态无效')
    if status == 'sent' and (evidence.get('title_verified') is not True or evidence.get('outgoing_verified') is not True
                             or (row.asset_id and evidence.get('media_sha256') != row.sha256)):
        raise maa.invalid('成功回执缺少目标、原素材与出站核验')
    if row.status in TERMINAL:
        if row.detail != body:
            raise conflict('终态回执不可覆盖')
        return {'id': did, 'status': row.status}
    if row.status != 'sending' and not (row.status=='ready' and status in {'failed','uncertain'}):
        raise conflict('交付未领取')
    row.status, row.detail, row.updated_at = status, body, time.time()
    db.commit()
    return {'id': did, 'status': status}
