from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class SyncFailure(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "sync_failures"
    __table_args__ = (
        UniqueConstraint("operation", "package_name", name="uq_sync_failures_operation_package"),
    )

    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    package_name: Mapped[str] = mapped_column(String(255), nullable=False)
    error_class: Mapped[str] = mapped_column(String(255), nullable=False)
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    traceback: Mapped[str | None] = mapped_column(Text, nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
