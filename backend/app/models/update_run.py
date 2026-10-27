from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, Integer, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin
from app.models.enums import UpdateRunStatus


class UpdateRun(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "update_runs"

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[UpdateRunStatus] = mapped_column(Enum(UpdateRunStatus, name="update_run_status_enum"), nullable=False)
    index_serial: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    packages_checked: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    packages_changed: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    packages_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    packages_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    packages_unavailable: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    events_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
