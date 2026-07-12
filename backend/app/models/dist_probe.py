from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class DistProbe(UUIDPrimaryKeyMixin, Base):
    """Offline facts extracted from one distribution of a package version.

    A version may ship several distributions (sdist + platform wheels), so the
    key is (version_id, artifact_filename). Rows are immutable: published
    distributions never change, re-probe requires deleting the row first.
    The artifact is referenced by registry_slug + filename, not by FK to
    artifacts: artifact rows are recreated on every sync.
    """

    __tablename__ = "dist_probes"

    version_id: Mapped[str] = mapped_column(ForeignKey("package_versions.id"), nullable=False)
    registry_slug: Mapped[str] = mapped_column(String(255), nullable=False)
    artifact_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    is_representative: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    entry_points_txt: Mapped[str | None] = mapped_column(Text, nullable=True)
    top_level_txt: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_facts: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    readme_extracted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (UniqueConstraint("version_id", "artifact_filename", name="uq_dist_probe_version_filename"),)
