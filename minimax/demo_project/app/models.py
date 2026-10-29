"""Book models for the demo bookstore service."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Book(BaseModel):
    id: int
    title: str
    author: str
    genre: str
    price: float = Field(ge=0)


class BookList(BaseModel):
    items: list[Book]
    total: int
    page: int
    page_size: int
