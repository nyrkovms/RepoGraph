import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    LargeBinary,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin
from app.models.enums import DependenciesState, PackageVersionStatus


class PackageVersion(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "package_versions"
    __table_args__ = (
        UniqueConstraint("package_id", "version", name="uq_package_versions_pkg_version"),
        Index("idx_ver_release", "release_date"),
        Index("idx_ver_pkg_sort", "package_id", text("sort_key DESC")),
    )

    package_id: Mapped[str] = mapped_column(ForeignKey("packages.id"), nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    release_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[PackageVersionStatus] = mapped_column(
        Enum(PackageVersionStatus, name="package_version_status_enum"),
        nullable=False,
        default=PackageVersionStatus.active,
    )
    download_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    features: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    raw_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False)

    sort_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)   # VersionScheme.sort_key
    is_prerelease: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    deprecation: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    engines: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    metadata_registry_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("registries.id"), nullable=True)
    dependencies_state: Mapped[DependenciesState] = mapped_column(
        Enum(DependenciesState, name="dependencies_state_enum"),
        nullable=False,
        default=DependenciesState.pending,
        server_default=text("'pending'"),
    )


class VersionSource(Base):
    """Which registry knows a version, with its status there (the version row holds the merged view)."""

    __tablename__ = "version_sources"
    __table_args__ = (Index("idx_versrc_registry", "registry_id"),)

    version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("package_versions.id", ondelete="CASCADE"), primary_key=True
    )
    registry_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("registries.id"), primary_key=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[PackageVersionStatus] = mapped_column(
        Enum(PackageVersionStatus, name="package_version_status_enum"),
        nullable=False,
        default=PackageVersionStatus.active,
        server_default=text("'active'"),
    )
    metadata_digest: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
