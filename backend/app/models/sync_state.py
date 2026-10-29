from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin
from app.models.enums import SyncStatus


class SyncState(UUIDPrimaryKeyMixin, Base):
    """Per source: change-feed cursor and health. Progress of operations lives in `runs` (IMPL R1)."""

    __tablename__ = "sync_states"

    registry_id: Mapped[str] = mapped_column(ForeignKey("registries.id"), unique=True, nullable=False)
    status: Mapped[SyncStatus] = mapped_column(Enum(SyncStatus, name="sync_status_enum"), nullable=False)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cursor_data: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    # Circuit breaker.
    degraded_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
