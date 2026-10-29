import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import Ecosystem


class SyncFailure(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "sync_failures"
    __table_args__ = (
        Index("uq_sync_failures_key", "operation", "ecosystem", "registry_id", "package_ref", unique=True,
              postgresql_nulls_not_distinct=True),
        Index("idx_sync_failures_retry_due", "retryable", "next_retry_at"),
        Index("idx_sync_failures_last_seen", "last_seen_at"),
    )

    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    error_class: Mapped[str] = mapped_column(String(255), nullable=False)
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    traceback: Mapped[str | None] = mapped_column(Text, nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    ecosystem: Mapped[Ecosystem] = mapped_column(Enum(Ecosystem, name="ecosystem_enum"), nullable=False)
    registry_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("registries.id"), nullable=True)   # NULL: no single source
    package_ref: Mapped[str] = mapped_column(Text, nullable=False)   # purl without version
