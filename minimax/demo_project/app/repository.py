"""In-memory book repository used by the demo service."""

from __future__ import annotations

from app.models import Book

SEED_BOOKS: tuple[Book, ...] = (
    Book(id=1, title="The Pragmatic Programmer", author="Hunt & Thomas", genre="technology", price=39.99),
    Book(id=2, title="Clean Code", author="Robert C. Martin", genre="technology", price=32.50),
    Book(id=3, title="The Hobbit", author="J. R. R. Tolkien", genre="fantasy", price=12.99),
    Book(id=4, title="Dune", author="Frank Herbert", genre="science-fiction", price=18.00),
    Book(id=5, title="The Left Hand of Darkness", author="Ursula K. Le Guin", genre="science-fiction", price=15.75),
    Book(id=6, title="A Brief History of Time", author="Stephen Hawking", genre="science", price=22.00),
    Book(id=7, title="The Name of the Wind", author="Patrick Rothfuss", genre="fantasy", price=28.00),
    Book(id=8, title="Sapiens", author="Yuval Noah Harari", genre="history", price=24.50),
)


class BookRepository:
    """Read-only repository; the demo keeps its data in process memory."""

    def __init__(self, books: tuple[Book, ...] = SEED_BOOKS) -> None:
        self._books = list(books)

    def list(
        self,
        *,
        genre: str | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
    ) -> list[Book]:
        books = self._books
        if genre is not None:
            books = [book for book in books if book.genre == genre]
        if min_price is not None:
            books = [book for book in books if book.price >= min_price]
        if max_price is not None:
            books = [book for book in books if book.price <= max_price]
        return list(books)

    def get(self, book_id: int) -> Book | None:
        return next((book for book in self._books if book.id == book_id), None)
