from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class Classification(UUIDPrimaryKeyMixin, Base):
    """Classifier verdict for one package version and one rules version.

    Versioned: rows of older classifier versions are kept for comparison;
    reclassify replaces only rows of the current CLASSIFIER_VERSION.
    """

    __tablename__ = "classifications"

    version_id: Mapped[str] = mapped_column(ForeignKey("package_versions.id"), nullable=False)
    classifier_version: Mapped[str] = mapped_column(String(32), nullable=False)
    classified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    status: Mapped[str] = mapped_column(String(32), nullable=False)  # ok | needs-data | undecidable-by-spec
    status_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")

    __table_args__ = (UniqueConstraint("version_id", "classifier_version", name="uq_classification_version_rules"),)


class ClassificationFacet(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "classification_facets"

    classification_id: Mapped[str] = mapped_column(
        ForeignKey("classifications.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[str] = mapped_column(String(8), nullable=False)  # high | low
    evidence: Mapped[str] = mapped_column(Text, nullable=False)
    provisional: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class ClassificationCapability(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "classification_capabilities"

    classification_id: Mapped[str] = mapped_column(
        ForeignKey("classifications.id", ondelete="CASCADE"), nullable=False
    )
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[str] = mapped_column(String(8), nullable=False)
    evidence: Mapped[str] = mapped_column(Text, nullable=False)


class UserCapability(UUIDPrimaryKeyMixin, Base):
    """Curated capability annotations — the only non-reproducible data of the
    classification layer. Never touched by the classifier. version='' means
    "all versions". Effective capabilities = machine minus (present=false)
    plus (present=true); user wins on conflict."""

    __tablename__ = "user_capabilities"

    ecosystem: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)  # normalized package name
    version: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    present: Mapped[bool] = mapped_column(Boolean, nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("ecosystem", "name", "version", "capability", name="uq_user_capability"),
    )


class RoleOverride(UUIDPrimaryKeyMixin, Base):
    """Curated role sets: terminal — heuristics never append roles on top."""

    __tablename__ = "role_overrides"

    ecosystem: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    roles: Mapped[list] = mapped_column(JSONB, nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")

    __table_args__ = (UniqueConstraint("ecosystem", "name", name="uq_role_override"),)


class EntryGroupRule(UUIDPrimaryKeyMixin, Base):
    """PyPI entry-point group taxonomy; pattern ending with '*' is a prefix."""

    __tablename__ = "entry_group_rules"

    pattern: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    group_type: Mapped[str] = mapped_column(String(32), nullable=False)
    role_effect: Mapped[str | None] = mapped_column(String(64), nullable=True)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    host: Mapped[str | None] = mapped_column(String(255), nullable=True)


class PackageRelation(Base):
    """plugin-of (package -> host it extends) and platform-payload
    (package -> its per-platform binary artifacts)."""

    __tablename__ = "package_relations"

    package_id: Mapped[str] = mapped_column(ForeignKey("packages.id"), primary_key=True)
    related_name: Mapped[str] = mapped_column(String(255), primary_key=True)
    relation: Mapped[str] = mapped_column(String(32), primary_key=True)


class LlmVerdict(UUIDPrimaryKeyMixin, Base):
    """Reserved for the semantic (L2/LLM) layer."""

    __tablename__ = "llm_verdicts"

    version_id: Mapped[str] = mapped_column(ForeignKey("package_versions.id"), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    verdict: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (UniqueConstraint("version_id", "model", "prompt_version", name="uq_llm_verdict"),)
