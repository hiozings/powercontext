# Demo Bookstore

一个只有 8 本书、一个只读接口和一个测试套件的小型 FastAPI 业务工程，用于验证 SpecLens 的规格驱动修改流程。

## 运行

```bash
cd demo_project
../.venv/bin/python -m pytest -q
../.venv/bin/python -m uvicorn app.main:app --reload
```

接口：

- `GET /books`：`genre`、`page`、`page_size`，本次任务新增 `min_price`、`max_price`；
- `GET /books/{id}`：单本详情，未知 id 返回 404。

## 任务

给 `GET /books` 增加价格区间过滤：

- `min_price`、`max_price` 均为闭区间、USD、非负；
- `min_price > max_price` 返回 422，detail 包含 `min_price`；
- genre 过滤、分页、详情行为保持不变。

## 规格与测试

- `specs/books_api.md`：当前规格（v1）；`books_api.v1.md` 是初始副本，`books_api.v2.md` 是规格更新样例。
- `tests/test_books.py`：4 个目标测试 + 4 个回归测试。
- 基线（`snapshots/before/`）只实现 genre 过滤，因此目标测试失败，回归测试通过。

测试文件在任务前后保持不变；MiniMax 只允许修改 `app/`。
