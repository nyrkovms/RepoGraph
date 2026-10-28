import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import Ecosystem


class SyncFailure(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "sync_failures"
    __table_args__ = (
        UniqueConstraint("operation", "package_name", name="uq_sync_failures_operation_package"),
        Index("idx_sync_failures_retry_due", "retryable", "next_retry_at"),
        Index("idx_sync_failures_last_seen", "last_seen_at"),
    )

    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    package_name: Mapped[str] = mapped_column(String(255), nullable=False, server_default=text("''"))
    error_class: Mapped[str] = mapped_column(String(255), nullable=False)
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    traceback: Mapped[str | None] = mapped_column(Text, nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    # Added by 0006: failures are keyed by ecosystem, source and purl (new unique key in 0007).
    ecosystem: Mapped[Ecosystem | None] = mapped_column(Enum(Ecosystem, name="ecosystem_enum"), nullable=True)
    registry_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("registries.id"), nullable=True)
    package_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
