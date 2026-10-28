import uuid

from sqlalchemy import Enum, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin
from app.models.enums import DependencyType, Ecosystem


class Dependency(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "dependencies"
    __table_args__ = (
        Index("idx_dep_version", "version_id"),
        Index("idx_dep_name", "dependency_name", "dependency_ecosystem"),
        Index("idx_dep_resolved", "resolved_version_id", postgresql_where=text("resolved_version_id IS NOT NULL")),
        Index("idx_dep_target", "dependency_ecosystem", "dependency_namespace", "dependency_name"),
        Index("idx_dep_version_variant", "version_id", "variant_registry_id"),
    )

    version_id: Mapped[str] = mapped_column(ForeignKey("package_versions.id"), nullable=False)

    dependency_name: Mapped[str] = mapped_column(Text, nullable=False)
    dependency_namespace: Mapped[str | None] = mapped_column(String(255), nullable=True, server_default=text("''"))
    dependency_ecosystem: Mapped[Ecosystem] = mapped_column(Enum(Ecosystem, name="ecosystem_enum"), nullable=False)
    version_constraint: Mapped[str] = mapped_column(Text, nullable=False)
    dependency_type: Mapped[DependencyType] = mapped_column(
        Enum(DependencyType, name="dependency_type_enum"), nullable=False
    )
    extra_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    environment_markers: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    raw_spec: Mapped[str] = mapped_column(Text, nullable=False)

    resolved_package_id: Mapped[str | None] = mapped_column(ForeignKey("packages.id"), nullable=True)
    resolved_version_id: Mapped[str | None] = mapped_column(ForeignKey("package_versions.id"), nullable=True)

    # Added by 0006.
    alias: Mapped[str | None] = mapped_column(Text, nullable=True)
    scope: Mapped[str | None] = mapped_column(String(32), nullable=True)
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    variant_registry_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("registries.id"), nullable=True)
