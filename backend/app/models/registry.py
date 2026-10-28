from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Enum, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import Ecosystem, RegistryProtocol, RegistryType, Tier


class Registry(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "registries"
    __table_args__ = (
        UniqueConstraint("id", "ecosystem", name="uq_registries_id_ecosystem"),
        CheckConstraint("auth_type = 'none' OR credentials_ref IS NOT NULL", name="ck_registries_auth_credentials"),
        CheckConstraint("auth_type IN ('none', 'basic', 'bearer', 'tls_client_cert')", name="ck_registries_auth_type"),
    )

    ecosystem: Mapped[Ecosystem] = mapped_column(Enum(Ecosystem, name="ecosystem_enum"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    registry_type: Mapped[RegistryType] = mapped_column(
        Enum(RegistryType, name="registry_type_enum"),
        nullable=False,
        default=RegistryType.public,
        server_default=text("'public'"),
    )
    base_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    api_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    url_pattern: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    auth_required: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    auth_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    tier: Mapped[Tier] = mapped_column(
        Enum(Tier, name="tier_enum"), default=Tier.tier1, server_default=text("'tier1'"), nullable=False
    )
    sync_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"), nullable=False)
    sync_priority: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Added by 0006 (the columns above that 0007 removes are kept until then).
    # Nullable until 0007: the 0005 code inserts registries without it.
    protocol: Mapped[RegistryProtocol | None] = mapped_column(
        Enum(RegistryProtocol, name="registry_protocol_enum", values_callable=lambda e: [m.value for m in e]),
        nullable=True,
    )
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    routing: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    auth_type: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'none'"))
    credentials_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
