from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin
from app.models.enums import PackageVersionStatus


class PackageVersion(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "package_versions"

    package_id: Mapped[str] = mapped_column(ForeignKey("packages.id"), nullable=False)
    version: Mapped[str] = mapped_column(String(128), nullable=False)
    release_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[PackageVersionStatus] = mapped_column(
        Enum(PackageVersionStatus, name="package_version_status_enum"),
        nullable=False,
        default=PackageVersionStatus.active,
    )
    requires_python: Mapped[str | None] = mapped_column(String(128), nullable=True)
    download_count: Mapped[int | None] = mapped_column(nullable=True)
    features: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    needed_in_index: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    raw_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False)
