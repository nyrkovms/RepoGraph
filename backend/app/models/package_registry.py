import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, ForeignKeyConstraint, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import Ecosystem


class PackageRegistry(Base):
    __tablename__ = "package_registries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["package_id", "ecosystem"], ["packages.id", "packages.ecosystem"], name="fk_pkgreg_package_ecosystem"
        ),
        ForeignKeyConstraint(
            ["registry_id", "ecosystem"], ["registries.id", "registries.ecosystem"], name="fk_pkgreg_registry_ecosystem"
        ),
    )

    package_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("packages.id"), primary_key=True)
    registry_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("registries.id"), primary_key=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    # Per-source change-detection state.
    ecosystem: Mapped[Ecosystem] = mapped_column(Enum(Ecosystem, name="ecosystem_enum"), nullable=False)
    upstream_revision: Mapped[str | None] = mapped_column(Text, nullable=True)
    etag: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_modified: Mapped[str | None] = mapped_column(Text, nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    missing_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
