from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    request_id: str | None = Field(default=None, min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    message: str = Field(min_length=1, max_length=1500)

    @field_validator("message", mode="before")
    @classmethod
    def strip_message(cls, text):
        return text.strip() if isinstance(text, str) else text


class AnalyticsRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1500)


class FeedbackRequest(BaseModel):
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    rating: int = Field(ge=1, le=5)
    comment: str = Field(default="", max_length=500)


class FeedbackReview(BaseModel):
    status: Literal["reviewed"]
    note: str = Field(default="", max_length=500)
