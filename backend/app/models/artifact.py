from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class Artifact(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "artifacts"

    version_id: Mapped[str] = mapped_column(ForeignKey("package_versions.id"), nullable=False)
    registry_id: Mapped[str] = mapped_column(ForeignKey("registries.id"), nullable=False)

    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    download_url: Mapped[str] = mapped_column(String(4096), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(nullable=True)
    checksum_sha256: Mapped[str | None] = mapped_column(String(128), nullable=True)
    checksum_md5: Mapped[str | None] = mapped_column(String(128), nullable=True)
    artifact_type: Mapped[str] = mapped_column(String(32), nullable=False)
    platform_tags: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    requires_dist_metadata: Mapped[bool] = mapped_column(default=False, nullable=False)

    __table_args__ = (UniqueConstraint("version_id", "filename", name="uq_artifact_version_filename"),)


class DistTag(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "dist_tags"

    package_id: Mapped[str] = mapped_column(ForeignKey("packages.id"), nullable=False)
    tag: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(128), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
