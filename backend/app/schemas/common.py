"""Gemeinsame API-Schemas."""

from __future__ import annotations

from pydantic import BaseModel


class Message(BaseModel):
    message: str


class Page[T](BaseModel):
    items: list[T]
    total: int
    page: int
    page_size: int
