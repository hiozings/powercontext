# Bookstore API Specification

## Overview

The demo bookstore exposes a read-only HTTP API over a fixed in-memory catalogue. The
goal of this specification is to make the price-range change reviewable: the existing
genre filter, pagination and detail endpoints must keep their current behaviour.

<!-- id: SPEC-BOOKS-LIST-1 -->
## List books

`GET /books` returns a paginated collection. The response contains `items`, `total`,
`page` and `page_size`. `total` is the number of books matching the filters, before
pagination. Existing clients rely on the default `page=1` and `page_size=10`.

<!-- id: SPEC-BOOKS-FILTER-2 -->
## Genre filter

`GET /books?genre=<value>` returns only books whose `genre` exactly equals the value.
An unknown genre returns an empty collection with `total` equal to zero. The genre
filter must keep working together with pagination.

<!-- id: SPEC-BOOKS-FILTER-3 -->
## Price range filter

`GET /books` accepts `min_price` and `max_price` query parameters. Both are inclusive,
decimal values in USD. `min_price` keeps books whose price is greater than or equal to
the value; `max_price` keeps books whose price is less than or equal to the value.
When both are supplied and `min_price` is greater than `max_price`, the API returns
HTTP 422 and the `detail` mentions `min_price`.

<!-- id: SPEC-BOOKS-VALIDATION-4 -->
## Price validation

Prices are non-negative. A negative `min_price` or `max_price` returns HTTP 422. A
non-numeric price parameter returns HTTP 422 as well.

<!-- id: SPEC-BOOKS-DETAIL-5 -->
## Book detail

`GET /books/{id}` returns one book. An unknown id returns HTTP 404. The detail endpoint
does not accept filters and must not be changed by this task.
