"""FastAPI app assembly (DESIGN.md §2, §5, §10).

Routers own their routes and apply auth.require_user router-wide; /api/health
is the single unauthenticated route. Tracing initializes at startup and flushes
at shutdown; the AI routes (logs, resolve/tier3, commit scoring) each flush
before returning per the §10 serverless rule.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

import tracing
from routes import commit, diary, entries, foods, logs, resolve


@asynccontextmanager
async def lifespan(app: FastAPI):
    tracing.init_tracing()
    yield
    try:
        tracing.flush()
    except Exception:
        pass


app = FastAPI(title="Usual API", lifespan=lifespan)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "service": "usual"}


app.include_router(logs.router)
app.include_router(resolve.router)
app.include_router(commit.router)
app.include_router(diary.router)
app.include_router(entries.router)
app.include_router(foods.router)
