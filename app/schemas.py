from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field


class VersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    version_number: int
    source_filename: str
    source_sha256: str
    created_at: datetime


class NodeOut(BaseModel):
    id: str
    logical_node_id: str
    version_id: str
    heading: str
    content: str
    node_type: str
    level: int
    parent_node_id: str | None
    children: list["NodeOut"] = []
    section_path: str
    content_hash: str
    source_pages: list[int]
    table_rows: list[list[str]]
    uncertain: bool
    ambiguous_match: bool


class SelectionItemIn(BaseModel):
    node_id: str
    version_id: str


class SelectionIn(BaseModel):
    document_id: str
    name: str = Field(min_length=1)
    items: list[SelectionItemIn] = Field(min_length=1)


class TestCase(BaseModel):
    title: str = Field(min_length=1)
    source_references: list[str] = Field(min_length=1)
    preconditions: list[str] = Field(min_length=1)
    test_steps: list[str] = Field(min_length=1)
    expected_result: str = Field(min_length=1)
    priority: str = Field(min_length=1)
    rationale: str = Field(min_length=1)


class GenerationOut(BaseModel):
    generation_id: str
    selection_id: str
    provider: str
    test_cases: list[TestCase]

