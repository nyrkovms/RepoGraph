from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin
from app.models.enums import SyncStatus, SyncType


class SyncState(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "sync_states"

    registry_id: Mapped[str] = mapped_column(ForeignKey("registries.id"), unique=True, nullable=False)
    sync_type: Mapped[SyncType] = mapped_column(
        Enum(SyncType, name="sync_type_enum"), nullable=False, server_default=text("'incremental'")
    )
    status: Mapped[SyncStatus] = mapped_column(Enum(SyncStatus, name="sync_status_enum"), nullable=False)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cursor_data: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    packages_synced: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"), nullable=False)
    packages_total: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"), nullable=False)

    # Added by 0006: health of the source (circuit breaker).
    degraded_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
