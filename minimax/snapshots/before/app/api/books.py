"""HTTP routes for the bookstore demo."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.models import Book, BookList
from app.repository import BookRepository

router = APIRouter()
repository = BookRepository()


@router.get("/books", response_model=BookList)
def list_books(
    genre: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=50),
) -> BookList:
    books = repository.list(genre=genre)
    total = len(books)
    start = (page - 1) * page_size
    return BookList(
        items=books[start : start + page_size],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/books/{book_id}", response_model=Book)
def get_book(book_id: int) -> Book:
    book = repository.get(book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="book not found")
    return book
