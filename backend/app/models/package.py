from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import Ecosystem


class Package(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "packages"

    ecosystem: Mapped[Ecosystem] = mapped_column(Enum(Ecosystem, name="ecosystem_enum"), nullable=False)
    namespace: Mapped[str | None] = mapped_column(String(255), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False)
    primary_registry_id: Mapped[str] = mapped_column(ForeignKey("registries.id"), nullable=False)

    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    homepage_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    repository_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    documentation_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    license_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    license_spdx: Mapped[str | None] = mapped_column(String(128), nullable=True)
    keywords: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)

    latest_version: Mapped[str] = mapped_column(String(128), nullable=False)
    downloads_total: Mapped[int | None] = mapped_column(nullable=True)
    downloads_recent: Mapped[int | None] = mapped_column(nullable=True)

    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    deprecated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deprecation_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    deprecation_alternative: Mapped[str | None] = mapped_column(String(255), nullable=True)

    raw_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False)
