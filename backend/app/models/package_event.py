import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Index, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import PackageEventKind


class PackageEvent(Base):
    __tablename__ = "package_events"
    __table_args__ = (
        Index("idx_package_events_detected_at", "detected_at"),
        Index("idx_package_events_package_detected", "package_id", "detected_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    package_id: Mapped[str] = mapped_column(ForeignKey("packages.id"), nullable=False)
    version: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind: Mapped[PackageEventKind] = mapped_column(Enum(PackageEventKind, name="package_event_kind_enum"), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # No FK: 0006 drops the one to update_runs, 0007 re-creates it against runs.
    run_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
