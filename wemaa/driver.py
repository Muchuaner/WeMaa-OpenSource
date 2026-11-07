"""原生 Maa 出站驱动：实际 UI 回执成功后才向核心报告 SENT。"""
import asyncio
import base64
import json
import re
import time
import uuid

from src.platform_io import DeliveryReceipt, DeliveryStatus, DriverDescriptor, DriverKind
from src.platform_io.drivers.base import PlatformIODriver

from wemaa.config import settings
from wemaa.database import session_factory
from wemaa.delivery_guard import DeliveryGuard
from wemaa.services import maa, media


class ConfirmedNoSend(RuntimeError):
    """Worker 已持久确认未准备草稿、未点击发送。"""


class MaaDriver(PlatformIODriver):
    def __init__(self, runtime):
        super().__init__(DriverDescriptor(driver_id='wemaa.maa', kind=DriverKind.LOCAL,
            platform='wechat', account_id=settings.WEMAA_ACCOUNT_ID,
            metadata={'name': 'Maa 微信', 'receipt': 'verified_outgoing_bubble',
                      'native_api': '/api/v1/wechat-maa-api'}))
        self.runtime = runtime
        self.lock = asyncio.Lock()
        self.delivery_guard = DeliveryGuard(runtime.registry.path.parent / 'delivery-guards.sqlite3',
                                            settings.WEMAA_ACCOUNT_ID)

    @staticmethod
    def inbound_source(session_id):
        from src.chat.message_receive.chat_manager import chat_manager
        incoming = chat_manager.last_messages.get(session_id)
        if incoming is None:
            return None
        config = incoming.message_info.additional_config or {}
        event = config.get('wemaa_event_id')
        if not isinstance(event, str) or not re.fullmatch('[a-f0-9]{64}', event):
            return None
        return {'event_id': event, 'observed_at': incoming.timestamp.timestamp()}

    def session(self, message):
        from src.chat.message_receive.chat_manager import chat_manager
        session = chat_manager.get_existing_session_by_session_id(message.session_id)
        if session is None:
            raise ValueError('CORE_SESSION_NOT_REGISTERED')
        if session.platform != 'wechat' or session.account_id != settings.WEMAA_ACCOUNT_ID:
            raise ValueError('CORE_SESSION_ACCOUNT_MISMATCH')
        return session

    def target(self, session):
        if session.group_id:
            if not session.group_id.startswith('maa_'):
                raise ValueError('GROUP_ROUTE_NOT_REGISTERED')
            return session.group_id[4:]
        return self.runtime.registry.route_for(session.user_id)

    def text_target(self, session):
        if not session.group_id:
            # 已有独立资料核验的会话直接导航；仅没有可靠路由的好友走通讯录。
            # Worker 仍核对会话、选中头像、空草稿及新出站气泡。
            try:
                conversation_id = self.runtime.registry.route_for(session.user_id)
            except ValueError as exc:
                if str(exc) != 'PRIVATE_ROUTE_NOT_VERIFIED':
                    raise
            else:
                return 'send_message', {'conversation_id': conversation_id}
            identity = self.runtime.registry.identity_for(session.user_id)
            if identity['status'] == 'profile_observed':
                # 通讯录中的好友无须先出现在最近聊天；Worker 会重新读取
                # 资料页微信号，并核验实际打开的聊天和新的发送气泡。
                return 'send_contact_message', {'contact_id': identity['contact_id'],
                                                'wechat_id': identity['wechat_id']}
        return 'send_message', {'conversation_id': self.target(session)}

    async def media_target(self, session, request_id):
        try:
            return self.target(session)
        except ValueError as exc:
            if str(exc) != 'PRIVATE_ROUTE_NOT_VERIFIED':
                raise
        identity = self.runtime.registry.identity_for(session.user_id)
        if identity['status'] != 'profile_observed':
            raise ValueError('PRIVATE_ROUTE_NOT_VERIFIED')
        row, _ = await asyncio.to_thread(maa.enqueue, {'action': 'open_contact',
            'params': {'contact_id': identity['contact_id']},
            'request_id': str(uuid.uuid5(uuid.NAMESPACE_URL, 'wemaa:open:' + request_id))},
            settings.WEMAA_ADMIN_USER_ID)
        receipt = await self.wait_command(row, expected='opened')
        opened = receipt.get('conversation_id')
        if not isinstance(opened, str) or not re.fullmatch('[a-f0-9]{64}', opened):
            raise ValueError('PRIVATE_OPEN_RECEIPT_INVALID')
        # 只有最新遥测中的独立资料证明与同一身份相符，才能使用新路径。
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            await self.runtime.sync_contacts()
            try:
                resolved = self.runtime.registry.resolve(opened)
            except ValueError:
                await asyncio.sleep(1)
                continue
            if resolved['id'] != session.user_id:
                raise ValueError('PRIVATE_OPEN_IDENTITY_MISMATCH')
            return self.runtime.registry.route_for(session.user_id)
        raise ValueError('PRIVATE_OPEN_PROOF_NOT_OBSERVED')

    async def send_message(self, message, route_key, metadata=None):
        receipts = []
        attempt = None
        command_ids = []
        enqueue_attempted = False
        try:
            if not settings.WECHAT_MAA_REPLY_ENABLED:
                raise ValueError('WEMAA_CORE_WECHAT_SENDING_DISABLED')
            async with self.lock:
                session = self.session(message)
                parts = self.parts(message)
                if not parts:
                    raise ValueError('EMPTY_OUTPUT')
                attempt = await asyncio.to_thread(self.delivery_guard.reserve, message.session_id,
                    message.message_id, self.inbound_source(message.session_id))
                for index, part in enumerate(parts):
                    request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f'wemaa:{message.message_id}:{index}'))
                    if part['kind'] == 'text':
                        action, params = self.text_target(session)
                        enqueue_attempted = True
                        row, _ = await asyncio.to_thread(maa.enqueue, {'action': action,
                            'params': {**params, 'text': part['text']},
                            'request_id': request_id}, settings.WEMAA_ADMIN_USER_ID)
                        command_ids.append(row['id'])
                        receipt = await self.wait_command(row)
                    else:
                        enqueue_attempted = True
                        cid = await self.media_target(session, request_id)
                        with session_factory()() as db:
                            from sqlalchemy import select
                            from wemaa.models import WechatDelivery
                            row = db.scalar(select(WechatDelivery).where(WechatDelivery.request_key=='core:'+request_id))
                            if row is None:
                                asset = media.store_asset(db, settings.WEMAA_ADMIN_USER_ID, part['data'],
                                                          part['name'], part['mime'])
                                row = media.add_delivery(db, 'core:' + request_id, cid,
                                    user_id=settings.WEMAA_ADMIN_USER_ID, asset_id=asset.id)
                            elif row.conversation_id != cid:
                                raise ValueError('CORE_MEDIA_RETRY_TARGET_CHANGED')
                            db.commit()
                            did = row.id
                        command_ids.append(did)
                        receipt = await self.wait_media(did)
                    receipts.append(receipt)
                await asyncio.to_thread(self.delivery_guard.finish, attempt, 'verified', '', command_ids)
                attempt = None
        except Exception as exc:
            if attempt is not None:
                await asyncio.to_thread(self.delivery_guard.finish, attempt,
                    'uncertain' if enqueue_attempted and not (isinstance(exc, ConfirmedNoSend) and not receipts) else 'blocked',
                    str(exc)[:256], command_ids)
            return DeliveryReceipt(internal_message_id=message.message_id, route_key=route_key,
                status=DeliveryStatus.FAILED, driver_id=self.driver_id, driver_kind=self.descriptor.kind,
                error=str(exc)[:256], metadata={'verified_receipts': receipts, 'retry': False})
        return DeliveryReceipt(internal_message_id=message.message_id, route_key=route_key,
            status=DeliveryStatus.SENT, driver_id=self.driver_id, driver_kind=self.descriptor.kind,
            metadata={'verified_receipts': receipts})

    @staticmethod
    def parts(message):
        from src.common.data_models.message_component_data_model import TextComponent, ImageComponent, EmojiComponent, ReplyComponent, AtComponent
        parts = []
        for component in message.raw_message.components:
            if isinstance(component, TextComponent):
                text = re.sub(r'[\r\n\t]+', ' ', component.text).strip()
                if any(ord(c) < 32 or ord(c) == 127 for c in text):
                    raise ValueError('OUTPUT_CONTROL_CHARACTER')
                parts.extend({'kind': 'text', 'text': text[i:i + 400]} for i in range(0, len(text), 400))
            elif isinstance(component, (ImageComponent, EmojiComponent)):
                from src.plugin_runtime.host.message_utils import PluginMessageUtils
                raw = component.binary_data or base64.b64decode(PluginMessageUtils._binary_component_to_base64(
                    component, 'emoji' if isinstance(component,EmojiComponent) else 'image'))
                if not raw:
                    raise ValueError('OUTPUT_IMAGE_BYTES_MISSING')
                parts.append({'kind': 'image', 'data': raw, 'mime': 'image/png', 'name': 'wemaa-image.png'})
            elif isinstance(component, (ReplyComponent, AtComponent)):
                # 引用/@是消息关系，不作为用户可见正文或内部 ID 文本发送。
                continue
            else:
                raise ValueError('MAA_UNSUPPORTED_OUTPUT_' + type(component).__name__)
        return parts

    @staticmethod
    async def wait_command(row, expected='sent'):
        while time.time() < row['expires_at'] + 10:
            raw = await asyncio.to_thread(maa.client().get, maa._command_key(row['id']))
            current = json.loads(raw or 'null')
            if not current:
                raise RuntimeError('COMMAND_RECEIPT_MISSING_NO_RETRY')
            detail = current.get('detail') or {}
            if current['status'] == 'succeeded' and detail.get('status') == expected:
                return {'id': row['id'], 'status': expected,
                        'conversation_id': detail.get('conversation_id')}
            if current['status'] in {'failed', 'expired', 'succeeded'}:
                diagnostic = detail.get('diagnostic') or {}
                reason = str(detail.get('reason') or 'COMMAND_NOT_VERIFIED_NO_RETRY')[:160]
                if (current['status'] == 'failed' and detail.get('status') == 'failed'
                        and diagnostic.get('phase') == 'navigation_or_validation'
                        and diagnostic.get('send_click_attempted') is False
                        and diagnostic.get('draft_prepared') is False):
                    raise ConfirmedNoSend('COMMAND_FAILED_BEFORE_INPUT:' + reason)
                raise RuntimeError('COMMAND_NOT_VERIFIED_NO_RETRY:' + reason)
            await asyncio.sleep(1)
        raise RuntimeError('COMMAND_OUTCOME_UNKNOWN_NO_RETRY')

    @staticmethod
    async def wait_media(did):
        from wemaa.models import WechatDelivery
        deadline = time.time() + 610
        while time.time() < deadline:
            with session_factory()() as db:
                row = db.get(WechatDelivery, did)
                if row and row.status == 'sent':
                    return {'id': did, 'status': 'sent'}
                if not row or row.status in media.TERMINAL:
                    raise RuntimeError('MEDIA_NOT_VERIFIED_NO_RETRY')
            await asyncio.sleep(1)
        raise RuntimeError('MEDIA_OUTCOME_UNKNOWN_NO_RETRY')
