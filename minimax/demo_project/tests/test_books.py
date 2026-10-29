"""Acceptance and regression tests for the demo bookstore."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def test_list_returns_all_books(client: TestClient) -> None:
    response = client.get("/books")
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 8
    assert len(payload["items"]) == 8


def test_genre_filter_is_preserved(client: TestClient) -> None:
    response = client.get("/books", params={"genre": "fantasy"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert {book["id"] for book in payload["items"]} == {3, 7}


def test_pagination_is_preserved(client: TestClient) -> None:
    response = client.get("/books", params={"page": 2, "page_size": 3})
    assert response.status_code == 200
    payload = response.json()
    assert payload["page"] == 2
    assert payload["page_size"] == 3
    assert payload["total"] == 8
    assert len(payload["items"]) == 3


def test_book_detail_and_missing_book(client: TestClient) -> None:
    found = client.get("/books/3")
    assert found.status_code == 200
    assert found.json()["title"] == "The Hobbit"
    missing = client.get("/books/999")
    assert missing.status_code == 404


def test_filter_by_price_range(client: TestClient) -> None:
    response = client.get("/books", params={"min_price": 10, "max_price": 20})
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    assert {book["id"] for book in payload["items"]} == {3, 4, 5}
    assert all(10 <= book["price"] <= 20 for book in payload["items"])


def test_price_boundaries_are_inclusive(client: TestClient) -> None:
    response = client.get("/books", params={"min_price": 18, "max_price": 22})
    assert response.status_code == 200
    payload = response.json()
    assert {book["id"] for book in payload["items"]} == {4, 6}


def test_invalid_price_range_returns_422(client: TestClient) -> None:
    response = client.get("/books", params={"min_price": 30, "max_price": 10})
    assert response.status_code == 422
    assert "min_price" in response.json()["detail"]


def test_negative_price_returns_422(client: TestClient) -> None:
    response = client.get("/books", params={"min_price": -1})
    assert response.status_code == 422
