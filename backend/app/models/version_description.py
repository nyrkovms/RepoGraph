from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class VersionDescription(UUIDPrimaryKeyMixin, Base):
    """Version-scoped description text by origin (e.g. tarball-readme).

    Package-level description stays on packages.description; this table keeps
    texts extracted from the distribution itself, which are inputs for the
    classification layer.
    """

    __tablename__ = "version_descriptions"

    version_id: Mapped[str] = mapped_column(ForeignKey("package_versions.id"), nullable=False)
    origin: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (UniqueConstraint("version_id", "origin", name="uq_version_description_origin"),)
