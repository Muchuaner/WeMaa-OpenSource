"""Native vision/embedding routes, backed by protected approved-channel configuration."""
from pathlib import Path

import json

import httpx
from fastapi import Response

from wemaa.config import settings
from wemaa.errors import BizError


async def multimodal(body: dict, endpoint: str) -> Response:
    roles={'wemaa-vlm':'vlm','wemaa-embedding':'embedding'}
    role=roles.get(body.get('model'))
    if not role or (role=='embedding') != (endpoint=='embeddings'):
        raise BizError(40001,'模型不支持此接口',400)
    path=Path(settings.WEMAA_NATIVE_MODELS_FILE)
    if not path.is_file():
        raise BizError(50341,'原生多模态渠道尚未配置',503)
    route=json.loads(path.read_text()).get(role)
    if not route:
        raise BizError(50341,'原生模型任务未配置',503)
    payload={**body,'model':route['model']}
    if role=='vlm':
        payload['stream']=False
    async with httpx.AsyncClient(timeout=60,follow_redirects=False) as client:
        result=await client.post(route['base_url'].rstrip('/')+'/'+endpoint,json=payload,
                                 headers={'Authorization':'Bearer '+route['api_key']})
    return Response(result.content,status_code=result.status_code,media_type='application/json')
