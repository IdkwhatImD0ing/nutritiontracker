"""GET /api/me — whose JWT this is and what the demo quota says (services/quota.py).

  {email, is_owner, demo: null | {limit, used_today, resets_at}}

`demo` is null for the owner (unlimited). Exposes an APIRouter; the integrator
wires it into main.py.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from auth import require_user
from services import quota

router = APIRouter(dependencies=[Depends(require_user)])


class DemoQuota(BaseModel):
    limit: int
    used_today: int
    resets_at: str  # ISO 8601, the next UTC midnight


class MeResponse(BaseModel):
    email: str
    is_owner: bool
    demo: DemoQuota | None


@router.get("/api/me", response_model=MeResponse)
async def get_me(user: dict = Depends(require_user)) -> MeResponse:
    demo = await quota.demo_status(user)
    return MeResponse(
        email=user["email"],
        is_owner=user["is_owner"],
        demo=DemoQuota(**demo) if demo is not None else None,
    )
