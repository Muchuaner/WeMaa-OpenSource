"""核心与 Maa 串行收件回执的边界；只处理显式启用的真实入站事件。"""
import asyncio


def event_id(message):
    if message is None or message.platform != 'wechat':
        return None
    config = message.message_info.additional_config or {}
    return config.get('wemaa_event_id') if config.get('wemaa_serial_reply') is True else None


async def settle(message, status, reason):
    event = event_id(message)
    if event:
        from wemaa.services.replies import complete_core_turn
        await asyncio.to_thread(complete_core_turn, event, status, reason)


async def can_reply(message, trigger):
    event = event_id(trigger)
    if not event:
        return True
    if event_id(message) != event:
        return False
    from wemaa.services.replies import core_status
    row = await asyncio.to_thread(core_status, event)
    return row['status'] == 'generating'
