from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def now() -> datetime:
    return datetime.now(timezone.utc)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    title: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    versions: Mapped[list["DocumentVersion"]] = relationship(back_populates="document", cascade="all, delete-orphan")


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (UniqueConstraint("document_id", "source_sha256", name="uq_document_source"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    version_number: Mapped[int] = mapped_column(Integer)
    source_filename: Mapped[str] = mapped_column(String(500))
    source_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    document: Mapped[Document] = relationship(back_populates="versions")
    nodes: Mapped[list["VersionNode"]] = relationship(back_populates="version", cascade="all, delete-orphan")


class LogicalNode(Base):
    __tablename__ = "logical_nodes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    nodes: Mapped[list["VersionNode"]] = relationship(back_populates="logical_node")


class VersionNode(Base):
    __tablename__ = "version_nodes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"), index=True)
    logical_node_id: Mapped[str] = mapped_column(ForeignKey("logical_nodes.id"), index=True)
    node_key: Mapped[str] = mapped_column(String(100))
    heading: Mapped[str] = mapped_column(Text, default="")
    content: Mapped[str] = mapped_column(Text, default="")
    node_type: Mapped[str] = mapped_column(String(30), default="section")
    level: Mapped[int] = mapped_column(Integer, default=0)
    parent_node_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    section_path: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String(64))
    source_pages: Mapped[str] = mapped_column(Text, default="[]")
    table_rows: Mapped[str] = mapped_column(Text, default="[]")
    uncertain: Mapped[bool] = mapped_column(Boolean, default=False)
    ambiguous_match: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[DocumentVersion] = relationship(back_populates="nodes")
    logical_node: Mapped[LogicalNode] = relationship(back_populates="nodes")


class Selection(Base):
    __tablename__ = "selections"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"))
    name: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    items: Mapped[list["SelectionItem"]] = relationship(back_populates="selection", cascade="all, delete-orphan")


class SelectionItem(Base):
    __tablename__ = "selection_items"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    selection_id: Mapped[str] = mapped_column(ForeignKey("selections.id"), index=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"))
    logical_node_id: Mapped[str] = mapped_column(ForeignKey("logical_nodes.id"))
    version_node_id: Mapped[str] = mapped_column(ForeignKey("version_nodes.id"))
    source_content_hash: Mapped[str] = mapped_column(String(64))
    selection: Mapped[Selection] = relationship(back_populates="items")

