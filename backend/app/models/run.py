import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import Ecosystem


class Run(Base):
    """Journal of operations (daily update, seed, presence probe, retry; IMPL R1)."""

    __tablename__ = "runs"
    __table_args__ = (Index("idx_runs_op_eco_started", "operation", "ecosystem", text("started_at DESC")),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    ecosystem: Mapped[Ecosystem | None] = mapped_column(Enum(Ecosystem, name="ecosystem_enum"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    packages_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    packages_checked: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    packages_changed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    packages_updated: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    packages_failed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    packages_unavailable: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    events_created: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    per_registry: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    events_by_kind: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
