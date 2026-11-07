"""可选网站能力；原生微信控制和核心回复不依赖此插件。"""
import json

import httpx
from sqlalchemy import inspect, text
from redis import RedisError

from wemaa.config import settings
from wemaa.errors import BizError


def configured() -> bool:
    return bool(settings.WEMAA_WEBSITE_ENABLED and settings.WEMAA_WEBSITE_URL and settings.WEMAA_WEBSITE_TOKEN)


async def request(method: str, path: str, *, body=None):
    if not configured():
        raise BizError(50340, "网站 Agent 插件未配置", 503)
    allowed = ("/api/v1/wechat-maa/creation/", "/internal/wemaa/")
    if not path.startswith(allowed):
        raise ValueError("WEBSITE_PLUGIN_PATH_NOT_ALLOWED")
    async with httpx.AsyncClient(timeout=100, follow_redirects=False) as client:
        return await client.request(method, settings.WEMAA_WEBSITE_URL.rstrip("/") + path,
            json=body, headers={"Authorization": "Bearer " + settings.WEMAA_WEBSITE_TOKEN})


def status() -> dict:
    from wemaa.services import maa
    health = None
    try:
        value = json.loads(maa.client().get('support:brain:health') or 'null')
        if value and value.get('plugin') == 'wemaa.website-agent':
            health = value
    except (ValueError, OSError, RedisError):
        pass
    return {"id": "website-agent", "name": "网站 Agent", "enabled": settings.WEMAA_WEBSITE_ENABLED,
            "configured": configured(), "connected": bool(health and health.get('connected')),
            "health": health, "capabilities": ["网站会话", "账户绑定", "图片创作", "视频创作", "素材交付", "积分与报价"]}


def assets(db, owner: int, asset_id: int | None = None) -> list[dict]:
    """Compatibility with the website's existing asset ledger; native uploads are independent."""
    if not configured() or not inspect(db.get_bind()).has_table('assets'):
        return []
    query='''SELECT id,user_id,type,name,size,mime,storage_key FROM assets
             WHERE user_id=:owner AND status=1 AND is_deleted=false AND type IN ('image','video')'''
    params={'owner':owner}
    if asset_id is not None:
        query+=' AND id=:id'
        params['id']=asset_id
    query+=' ORDER BY created_at DESC LIMIT 500'
    return [dict(row) for row in db.execute(text(query),params).mappings()]


def legacy_delivery(db, delivery_id: str):
    """Only read pre-cutover receipts; their original owner keeps claim/idempotency state."""
    if not configured() or not inspect(db.get_bind()).has_table('wechat_deliveries'):
        return None
    return db.execute(text('SELECT status,detail FROM wechat_deliveries WHERE id=:id'),
                      {'id':delivery_id}).mappings().first()


def expire_legacy_delivery(db, delivery_id: str) -> bool:
    return db.execute(text("UPDATE wechat_deliveries SET status='cancelled' WHERE id=:id AND status='ready'"),
                      {'id':delivery_id}).rowcount == 1
