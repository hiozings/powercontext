"""FastAPI application entry point for the demo bookstore."""

from __future__ import annotations

from fastapi import FastAPI

from app.api.books import router

app = FastAPI(title="Demo Bookstore API", version="0.1.0")
app.include_router(router)
