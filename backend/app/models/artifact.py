from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class Artifact(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        Index("idx_art_version", "version_id"),
        Index("idx_art_type", "version_id", "artifact_type"),
        Index("uq_artifacts_version_registry_filename", "version_id", "registry_id", "filename", unique=True),
    )

    version_id: Mapped[str] = mapped_column(ForeignKey("package_versions.id"), nullable=False)
    registry_id: Mapped[str] = mapped_column(ForeignKey("registries.id"), nullable=False)

    filename: Mapped[str] = mapped_column(Text, nullable=False)
    download_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    checksum_sha256: Mapped[str | None] = mapped_column(String(128), nullable=True)
    checksum_md5: Mapped[str | None] = mapped_column(String(128), nullable=True)
    artifact_type: Mapped[str] = mapped_column(String(32), nullable=False)
    platform_tags: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Added by 0006.
    arch: Mapped[str | None] = mapped_column(String(32), nullable=True)
    checksum_sha1: Mapped[str | None] = mapped_column(String(40), nullable=True)
    checksum_sha512: Mapped[str | None] = mapped_column(String(128), nullable=True)
    checksum_integrity: Mapped[str | None] = mapped_column(Text, nullable=True)
    has_metadata_file: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    metadata_blob: Mapped[str | None] = mapped_column(Text, nullable=True)


class DistTag(Base):
    __tablename__ = "dist_tags"

    package_id: Mapped[str] = mapped_column(ForeignKey("packages.id"), primary_key=True)
    tag: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[str] = mapped_column(String(128), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
