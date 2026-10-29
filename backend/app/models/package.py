from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import Ecosystem, PackageOrigin


class Package(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "packages"
    __table_args__ = (
        UniqueConstraint("id", "ecosystem", name="uq_packages_id_ecosystem"),
        Index("idx_pkg_eco_ns_name", "ecosystem", "namespace", "normalized_name", unique=True),
        Index(
            "idx_pkg_name_trgm",
            "normalized_name",
            postgresql_using="gin",
            postgresql_ops={"normalized_name": "gin_trgm_ops"},
        ),
        Index("idx_pkg_registry", "primary_registry_id"),
        Index("idx_pkg_license", "license_spdx", postgresql_where=text("license_spdx IS NOT NULL")),
        Index("idx_pkg_downloads", text("downloads_total DESC NULLS LAST")),
    )

    ecosystem: Mapped[Ecosystem] = mapped_column(Enum(Ecosystem, name="ecosystem_enum"), nullable=False)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False, server_default=text("''"))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False)
    primary_registry_id: Mapped[str] = mapped_column(ForeignKey("registries.id"), nullable=False)

    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    homepage_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    repository_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    documentation_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    license_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    license_spdx: Mapped[str | None] = mapped_column(String(128), nullable=True)
    keywords: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)

    latest_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    downloads_total: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    downloads_recent: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    unavailable_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    raw_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False)

    origin: Mapped[PackageOrigin] = mapped_column(
        Enum(PackageOrigin, name="package_origin_enum"),
        nullable=False,
        default=PackageOrigin.dependency,
        server_default=text("'dependency'"),
    )
    deprecation: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
