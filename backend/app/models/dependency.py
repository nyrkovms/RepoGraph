from sqlalchemy import Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin
from app.models.enums import DependencyType, Ecosystem


class Dependency(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "dependencies"

    version_id: Mapped[str] = mapped_column(ForeignKey("package_versions.id"), nullable=False)

    dependency_name: Mapped[str] = mapped_column(String(255), nullable=False)
    dependency_namespace: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dependency_ecosystem: Mapped[Ecosystem] = mapped_column(Enum(Ecosystem, name="ecosystem_enum"), nullable=False)
    version_constraint: Mapped[str] = mapped_column(String(255), nullable=False)
    dependency_type: Mapped[DependencyType] = mapped_column(
        Enum(DependencyType, name="dependency_type_enum"), nullable=False
    )
    extra_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    environment_markers: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    raw_spec: Mapped[str] = mapped_column(Text, nullable=False)

    resolved_package_id: Mapped[str | None] = mapped_column(ForeignKey("packages.id"), nullable=True)
    resolved_version_id: Mapped[str | None] = mapped_column(ForeignKey("package_versions.id"), nullable=True)
