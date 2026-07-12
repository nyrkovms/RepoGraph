from pydantic import BaseModel, Field


class AnnotateRequest(BaseModel):
    ecosystem: str = Field(pattern="^(pypi|npm)$")
    name: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    present: bool
    note: str = ""
    version: str = ""  # '' — все версии


class AnnotateDeleteRequest(BaseModel):
    ecosystem: str = Field(pattern="^(pypi|npm)$")
    name: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    version: str = ""


class AnnotateResponse(BaseModel):
    ok: bool
    deleted: int | None = None
