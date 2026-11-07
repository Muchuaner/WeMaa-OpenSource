"""WeMaa 原生运行时：在核心事件循环内注册 Maa Platform IO 驱动。"""
from datetime import datetime

import asyncio
import logging
import time

from wemaa.config import settings
from wemaa.identity import IdentityRegistry

logger = logging.getLogger('wemaa')


class Runtime:
    def __init__(self):
        self.loop = None
        self.driver = None
        self.registry = None
        self.tasks = []
        self.error = None
        self.registered_at = None
        self.last_telemetry_at = None

    async def start(self):
        from wemaa.database import initialize
        from wemaa.driver import MaaDriver
        from src.platform_io import get_platform_io_manager, RouteBinding
        initialize()
        self.loop = asyncio.get_running_loop()
        self.registry = IdentityRegistry()
        manager = get_platform_io_manager()
        self.driver = MaaDriver(self)
        await manager.add_driver(self.driver)
        manager.bind_send_route(RouteBinding(route_key=self.driver.descriptor.route_key,
            driver_id=self.driver.driver_id, driver_kind=self.driver.descriptor.kind))
        self.tasks.append(asyncio.create_task(self._sync_loop(), name='wemaa-identities'))
        self.registered_at = time.time()

    async def call(self, coroutine):
        if self.loop is None or not self.loop.is_running():
            coroutine.close()
            raise RuntimeError('WEMAA_CORE_NOT_READY')
        if asyncio.get_running_loop() is self.loop:
            return await coroutine
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coroutine, self.loop))

    async def _sync_loop(self):
        while True:
            try:
                await self.sync_contacts()
                self.error = None
            except asyncio.CancelledError:
                raise
            except Exception:
                self.error = 'IDENTITY_SYNC_FAILED'
                logger.exception('WeMaa 身份同步失败')
            await asyncio.sleep(15)

    async def sync_contacts(self):
        from wemaa.services import maa
        from src.chat.message_receive.chat_manager import chat_manager
        from src.person_info.person_info import Person
        record = await asyncio.to_thread(maa.latest)
        if not record or maa.is_stale(record):
            return {'stale': True}
        # /telemetry 在独立线程内完成唯一一次身份同步；这里仅绑定核心身份，
        # 不再重放同一全量快照、清空再重建 routes。
        self.last_telemetry_at = record['received_at']
        for row in await asyncio.to_thread(self.registry.items):
            person = Person.register_person(platform='wechat', user_id=row['id'], nickname=row['name'])
            if person is None:
                raise RuntimeError('CORE_PERSON_REGISTRATION_FAILED')
            if person.nickname != row['name']:
                person.nickname = row['name']
                person.sync_to_database()
            session = chat_manager.get_existing_session_by_session_id(row['session_id']) if row['session_id'] else None
            if session is None:
                session = await chat_manager.get_or_create_session('wechat', row['id'], account_id=settings.WEMAA_ACCOUNT_ID)
            if session.user_nickname != row['name']:
                session.user_nickname = row['name']
                chat_manager._save_session(session)
            if row['person_id'] != person.person_id or row['session_id'] != session.session_id:
                await asyncio.to_thread(self.registry.bind_core,row['id'],person.person_id,session.session_id)
        return await asyncio.to_thread(self.registry.summary)

    async def admit_identity(self, body):
        """幂等身份准备；不保存核心消息、不调用模型、不产生出站命令。"""
        from wemaa.services import maa
        record = await asyncio.to_thread(maa.latest)
        if maa.is_stale(record):
            raise ValueError('WEMAA_TELEMETRY_STALE')
        proof = body.get('identity')
        if proof:
            row = await asyncio.to_thread(self.registry.register_sender,proof,body['conversation_id'],
                                          private=body['conversation_type'] == 'private')
        if body['conversation_type'] == 'private':
            row = await asyncio.to_thread(self.registry.resolve,body['conversation_id'])
            return {'user_id':row['id'],'name':row['name'],'group_id':None}
        else:
            conversation = next((r for r in record['telemetry'].get('conversations', [])
                                 if body['conversation_id'] in [r['id'], *r.get('aliases', [])]), None)
            if not conversation or not conversation.get('group_profile'):
                raise ValueError('GROUP_ROUTE_NOT_VERIFIED')
            if not proof:
                raise ValueError('GROUP_SENDER_PROFILE_NOT_VERIFIED')
            return {'user_id':row['id'],'name':row['name'],'group_id':'maa_'+conversation['id']}

    async def ingest(self, body, *, admitted=None):
        from src.chat.message_receive.bot import chat_bot
        from src.chat.message_receive.message import SessionMessage
        from src.common.data_models.mai_message_data_model import MessageInfo, UserInfo, GroupInfo
        from src.common.data_models.message_component_data_model import MessageSequence, TextComponent
        identity=admitted if admitted is not None else await self.admit_identity(body)
        uid,name=identity['user_id'],identity['name']
        group=(GroupInfo(group_id=identity['group_id'],group_name=body['conversation_title'])
               if identity.get('group_id') else None)
        message = SessionMessage(message_id='maa_' + body['event_id'],
            timestamp=datetime.fromtimestamp(body['observed_at']), platform='wechat')
        message.message_info = MessageInfo(user_info=UserInfo(uid, name), group_info=group,
            additional_config={'platform_io_account_id': settings.WEMAA_ACCOUNT_ID,
                               'wemaa_event_id': body['event_id'],
                               'wemaa_serial_reply': body.get('serial_reply') is True,
                               'wemaa_conversation_id': body['conversation_id']})
        message.raw_message = MessageSequence([TextComponent(text=body['text'])])
        if group is not None:
            from src.chat.message_receive.chat_manager import chat_manager
            from wemaa.group_reply import decide
            session = await chat_manager.get_or_create_session('wechat', uid, group.group_id,
                account_id=settings.WEMAA_ACCOUNT_ID)
            decision = await decide(body, session_id=session.session_id, sender_id=uid)
            if decision.reason == 'classification_failed':
                # 技术故障不是已确认的静默，也不能进入接收流程释放 FIFO。
                raise ValueError('WEMAA_GROUP_CLASSIFICATION_FAILED')
            message.message_info.additional_config['wemaa_group_reply'] = decision.metadata()
            logger.info('群聊回复意图: event=%s reply=%s reason=%s',
                        body['event_id'], decision.reply, decision.reason)
        # 使用正式消息流程，保留命令、插件 hook、人物、表达、记忆和规划器。
        await chat_bot.receive_message(message)
        return {'session_id': message.session_id, 'user_id': uid,
                **({'group_reply': decision.metadata()} if group is not None else {})}

    def status(self):
        return {'ready': self.loop is not None and self.loop.is_running(), 'error': self.error,
                'registered_at': self.registered_at, 'identity': self.registry.summary() if self.registry else {},
                'platform': 'wechat', 'driver': 'wemaa.maa', 'version': '0.1.0'}


runtime = Runtime()
