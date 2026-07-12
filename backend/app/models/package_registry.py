from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class PackageRegistry(Base):
    __tablename__ = "package_registries"

    package_id: Mapped[str] = mapped_column(ForeignKey("packages.id"), primary_key=True)
    registry_id: Mapped[str] = mapped_column(ForeignKey("registries.id"), primary_key=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    name_collision: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")
    checksum_mismatch: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")
