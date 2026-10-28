"""寻章检索服务：seekdb 混合检索 + PowerContext 阅读记忆，同时托管站点静态资源。"""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from service import RetrievalService

ROOT = Path(__file__).resolve().parents[1]
DB_DIR = Path(os.environ.get("SEEKDB_DIR", Path(__file__).resolve().parent / "data" / "seekdb"))
SITE_DIR = ROOT / "site"

state = {}


@asynccontextmanager
async def lifespan(app):
    state["service"] = RetrievalService(DB_DIR)
    yield
    state["service"].store.close()


app = FastAPI(title="寻章检索服务", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    memory: bool = True
    limit: int = Field(default=5, ge=1, le=20)
    year_from: int | None = None
    kinds: list[str] | None = None


@app.get("/api/health")
def health():
    return state["service"].health()


@app.post("/api/search")
def search(req: SearchRequest):
    query = req.query.strip()
    if not query:
        raise HTTPException(status_code=422, detail="query 不能为空")
    return state["service"].search(
        query, use_memory=req.memory, limit=req.limit,
        year_from=req.year_from, kinds=req.kinds,
    )


@app.get("/api/memory")
def memory():
    return state["service"].memory_entries()


app.mount("/", StaticFiles(directory=SITE_DIR, html=True), name="site")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"),
                port=int(os.environ.get("PORT", "8787")))
