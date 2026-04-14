from typing import Literal

from pydantic import BaseModel, Field


class RenderRequest(BaseModel):
    url: str
    timeout_ms: int = Field(default=30000, ge=1000, le=120000)
    wait_until: Literal["load", "domcontentloaded", "networkidle"] = "networkidle"
    wait_after_load_ms: int = Field(default=0, ge=0, le=30000)


class RenderResponse(BaseModel):
    html: str
    status_code: int
    url: str
    elapsed_ms: float


class ErrorResponse(BaseModel):
    error: str
    detail: str
