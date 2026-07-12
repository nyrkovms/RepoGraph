from sqlalchemy import Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class Author(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "authors"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    url: Mapped[str | None] = mapped_column(String(2048), nullable=True)


class PackageAuthor(Base):
    __tablename__ = "package_authors"

    package_id: Mapped[str] = mapped_column(ForeignKey("packages.id"), primary_key=True)
    author_id: Mapped[str] = mapped_column(ForeignKey("authors.id"), primary_key=True)
    role: Mapped[str] = mapped_column(Enum("author", "maintainer", name="author_role_enum"), primary_key=True)

    __table_args__ = (UniqueConstraint("package_id", "author_id", "role", name="uq_package_author_role"),)
