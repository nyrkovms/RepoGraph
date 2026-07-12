from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import Ecosystem, RegistryType, Tier


class Registry(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "registries"

    ecosystem: Mapped[Ecosystem] = mapped_column(Enum(Ecosystem, name="ecosystem_enum"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    registry_type: Mapped[RegistryType] = mapped_column(
        Enum(RegistryType, name="registry_type_enum"), nullable=False, default=RegistryType.public
    )
    base_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    api_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    url_pattern: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    auth_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    auth_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    tier: Mapped[Tier] = mapped_column(Enum(Tier, name="tier_enum"), default=Tier.tier1, nullable=False)
    sync_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sync_priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
