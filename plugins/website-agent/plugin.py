"""网站 Agent：正式 SDK 消息网关，与 WeMaa 核心共用插件生命周期。"""
from datetime import datetime, timezone
from typing import Any

import asyncio
import json
import os
import time

import redis.asyncio as aioredis
from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, Field, MaiBotPlugin, MessageGateway, PluginConfigBase

from .prompt import _prompt


class PluginSection(PluginConfigBase):
    config_version: str = Field(default='0.1.0', description='配置版本')
    enabled: bool = Field(default=True, description='启用网站客服消息网关')


class WebsiteConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)


class WebsiteAgent(MaiBotPlugin):
    config_model = WebsiteConfig

    async def on_load(self) -> None:
        self.tasks: set[asyncio.Task] = set()
        self.pending: dict[str, asyncio.Future] = {}
        self.error = None
        self.last_success_at = None
        self.redis = aioredis.from_url(os.environ['REDIS_URL'], decode_responses=True)
        if self.config.plugin.enabled and os.getenv('WEMAA_WEBSITE_ENABLED','false').lower() == 'true':
            await self.redis.ping()
            await self.ctx.gateway.update_state('website_agent', ready=True, platform='website_support',
                account_id='website', metadata={'name':'网站 Agent'})
            self.spawn(self.listen())
            self.spawn(self.health())

    def spawn(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def on_unload(self) -> None:
        await self.ctx.gateway.update_state('website_agent', ready=False)
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.redis.aclose()

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            await self.on_unload()
            await self.on_load()

    async def listen(self) -> None:
        while True:
            try:
                async with self.redis.pubsub() as subscription:
                    await subscription.subscribe(os.getenv('SUPPORT_BRAIN_REQUEST_CHANNEL','support:brain:requests'))
                    async for event in subscription.listen():
                        if event.get('type') == 'message':
                            payload = json.loads(event['data'])
                            self.spawn(self.handle(payload))
            except asyncio.CancelledError:
                raise
            except Exception:
                self.error = 'WEBSITE_REQUEST_BUS_DISCONNECTED'
                await asyncio.sleep(3)

    async def handle(self, payload: dict) -> None:
        conversation, request_id = str(payload.get('conversation_id') or ''), str(payload.get('request_id') or '')
        channel = str(payload.get('reply_channel') or '')
        if not conversation or not request_id or not channel.startswith('support:brain:reply:'):
            return
        # Website bus intentionally isolates each request; authoritative history is
        # supplied by the website. A timed-out request cannot reply to a newer one.
        uid = 'web_' + request_id
        result = {'request_id':request_id,'conversation_id':conversation,'ok':False}
        key = 'wemaa:website:request:' + request_id
        if not await self.redis.set(key, 'started', nx=True, ex=86400):
            prior = await self.redis.get(key)
            if prior and prior.startswith('{'):
                await self.redis.publish(channel, prior)
            return
        try:
            generation = int(payload.get('generation') or 0)
            if int(await self.redis.get('support:brain:generation:'+conversation) or 0) != generation:
                result['error'] = 'request_cancelled'
                return
            self.pending[uid] = asyncio.get_running_loop().create_future()
            accepted = await self.ctx.gateway.route_message('website_agent', {
                'message_id':request_id, 'timestamp':datetime.now(timezone.utc).isoformat(),
                'platform':'website_support',
                'message_info':{'user_info':{'user_id':uid,'user_nickname':'网站访客'},
                    'additional_config':{'website_request_id':request_id,'website_support_direct_request':True,
                        'platform_io_account_id':'website'}},
                'raw_message':[{'type':'at','data':{'target_user_id':'website-ai-assistant'}},
                               {'type':'text','data':_prompt(payload)}],
            }, external_message_id=request_id, dedupe_key='website:'+request_id)
            if not accepted:
                result['error'] = 'core_rejected'
                return
            deadline = time.monotonic() + min(23, max(5,int(os.getenv('SUPPORT_BRAIN_TIMEOUT_SECONDS','25'))-2))
            future = self.pending[uid]
            while not future.done():
                if int(await self.redis.get('support:brain:generation:'+conversation) or 0) != generation:
                    result['error'] = 'request_cancelled'
                    return
                if time.monotonic() >= deadline:
                    raise asyncio.TimeoutError
                await asyncio.sleep(.2)
            result.update(ok=True,text=future.result()[:2000])
            self.last_success_at = datetime.now(timezone.utc).isoformat()
            self.error = None
        except asyncio.TimeoutError:
            result['error'] = 'maibot_timeout'
        except asyncio.CancelledError:
            result['error'] = 'plugin_unloaded'
            raise
        except Exception:
            result['error'] = 'plugin_error'
            self.error = 'WEBSITE_CORE_REQUEST_FAILED'
        finally:
            self.pending.pop(uid,None)
            raw = json.dumps(result,ensure_ascii=False)
            await self.redis.setex(key,86400,raw)
            await self.redis.publish(channel,raw)

    @MessageGateway(route_type='duplex', name='website_agent', platform='website_support',
                    protocol='wemaa-website', account_id='website')
    async def send_to_website(self, message: dict[str,Any], **kwargs) -> dict[str,Any]:
        info = message.get('message_info') or {}
        extra = info.get('additional_config') or {}
        uid = extra.get('platform_io_target_user_id') or extra.get('target_user_id')
        future = self.pending.get(uid)
        text = ''.join(str(p['data']) for p in message.get('raw_message',[])
                       if p.get('type') == 'text' and isinstance(p.get('data'),str)).strip()
        if not future or future.done() or not text:
            return {'success':False,'error':'WEBSITE_REQUEST_EXPIRED_OR_EMPTY'}
        future.set_result(text)
        return {'success':True,'external_message_id':message.get('message_id')}

    async def health(self) -> None:
        while True:
            await self.redis.setex('support:brain:health',45,json.dumps({
                'mode':'maibot','version':'WeMaa 0.1.0','connected':self.error is None,
                'last_success_at':self.last_success_at,'error':self.error,'plugin':'wemaa.website-agent'}))
            await asyncio.sleep(15)


def create_plugin():
    return WebsiteAgent()
