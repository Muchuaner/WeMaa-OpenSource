"""Durable, one-shot admin-authored messages for the sole Maa Worker."""
import time
import uuid
from sqlalchemy import BigInteger, Float, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from wemaa.database import Base

class WechatSchedule(Base):
    __tablename__ = 'wechat_schedules'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    requested_by: Mapped[int] = mapped_column(BigInteger)
    conversation_id: Mapped[str] = mapped_column(String(64))
    target: Mapped[dict] = mapped_column(JSON)
    messages: Mapped[list] = mapped_column(JSON, default=list)
    recurrence: Mapped[dict] = mapped_column(JSON, default=dict)
    deleted_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    text: Mapped[str] = mapped_column(Text)
    send_at: Mapped[float] = mapped_column(Float, index=True)
    status: Mapped[str] = mapped_column(String(24), default='pending', index=True)
    revision: Mapped[int] = mapped_column(default=1)
    command_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class MediaAsset(Base):
    __tablename__ = 'wemaa_media_assets'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(String(256))
    type: Mapped[str] = mapped_column(String(16))
    mime: Mapped[str] = mapped_column(String(100))
    size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class WechatDelivery(Base):
    __tablename__ = 'wemaa_media_deliveries'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    request_key: Mapped[str] = mapped_column(String(256), unique=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    conversation_id: Mapped[str] = mapped_column(String(64))
    asset_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    text: Mapped[str] = mapped_column(Text, default='')
    target: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default='ready')
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mime: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)
