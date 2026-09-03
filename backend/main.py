"""FastAPI app assembly (DESIGN.md §2, §5, §10).

Routers own their routes and apply auth.require_user router-wide; /api/health
is the single unauthenticated route. Tracing initializes at startup and flushes
at shutdown; the AI routes (logs, resolve/tier3, commit scoring) each flush
before returning per the §10 serverless rule.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

import tracing
from routes import (
    aliases,
    commit,
    diary,
    entries,
    foods,
    history,
    logs,
    meals,
    metrics,
    photo,
    quick,
    resolve,
    settings,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    tracing.init_tracing()
    yield
    try:
        tracing.flush()
    except Exception:
        pass


app = FastAPI(title="Tally API", lifespan=lifespan)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "service": "tally"}


app.include_router(logs.router)
app.include_router(resolve.router)
app.include_router(commit.router)
app.include_router(diary.router)
app.include_router(entries.router)
# M4 (§12): the + sheet's four lanes. quick (GET /api/foods/barcode) must
# precede foods, whose GET /api/foods/{food_id} would otherwise claim the
# path; photo owns /api/logs/photo + /api/resolve/confirm-estimate (§3.4b).
app.include_router(quick.router)
app.include_router(photo.router)
app.include_router(foods.router)
# M3 (§12): saved meals, the alias sheet / Library list, History, Settings +
# the goal wizard, and the §9 /experiment series.
app.include_router(meals.router)
app.include_router(aliases.router)
app.include_router(history.router)
app.include_router(settings.router)
app.include_router(metrics.router)
