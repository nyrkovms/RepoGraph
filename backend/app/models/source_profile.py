import uuid

from sqlalchemy import Boolean, Enum, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import Ecosystem


class SourceProfile(Base):
    """Named, ordered set of registries of one ecosystem; one profile per ecosystem is the default."""

    __tablename__ = "source_profiles"
    __table_args__ = (
        UniqueConstraint("ecosystem", "slug"),
        UniqueConstraint("id", "ecosystem"),
        Index("uq_source_profiles_default", "ecosystem", unique=True, postgresql_where=text("is_default")),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    ecosystem: Mapped[Ecosystem] = mapped_column(Enum(Ecosystem, name="ecosystem_enum"), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class SourceProfileRegistry(Base):
    __tablename__ = "source_profile_registries"
    __table_args__ = (
        UniqueConstraint("profile_id", "position"),
        ForeignKeyConstraint(
            ["profile_id", "ecosystem"], ["source_profiles.id", "source_profiles.ecosystem"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(["registry_id", "ecosystem"], ["registries.id", "registries.ecosystem"]),
    )

    profile_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    registry_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    ecosystem: Mapped[Ecosystem] = mapped_column(Enum(Ecosystem, name="ecosystem_enum"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    routing_override: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
