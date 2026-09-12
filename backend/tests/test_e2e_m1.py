"""M1 end-to-end story (integrator) — the full friction-killer loop, live.

In-process TestClient over the assembled app (main.app, all routers wired),
against the LIVE services: Neon (production DB until the dev branch exists),
the GPT-5.6 Terra chat agent and tier-3 agent, USDA FDC, Langfuse.

The story (DESIGN.md §12 M1 exit criteria):
  1. log "chicken and rice"  → the chat agent → tier 3 per item (kick, answer/manual
     as needed) → everything resolved
  2. EDIT a portion via POST /api/resolve (spoken qty 2) → commit
     → was_edited TRUE, aliases learned, portion default recomputed to the edit
  3. re-log the same phrase → tiers 1/2 only, ZERO food-search network,
     the corrected portion pre-filled → commit with zero edits
     ("a repeat meal commits with zero edits and one tap, and a correction
      changes the very next proposal")
  4. qty-PATCH one committed entry → the stored macro snapshot scales
     proportionally (§3.5), never re-derived

Dated 2000-01-04 so diary noise never lands on a real day; every row this test
creates (logs, item_resolutions, entries, aliases, foods) is deleted in the
finally block — pre-existing rows are never touched (created_at >= t0 guards).

Run:  uv run --with pytest pytest backend/tests/test_e2e_m1.py -q -s
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if sys.platform == "win32":  # psycopg async needs the selector loop on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _load_env() -> None:
    env_path = BACKEND.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import db  # noqa: E402
from tenancy_harness import override_user  # noqa: E402
import main  # noqa: E402
from resolver.norm import norm  # noqa: E402

pytestmark = pytest.mark.live

UTTERANCE = "chicken and rice"
CAPTURED_AT_1 = "2000-01-04T18:30:00+00:00"
CAPTURED_AT_2 = "2000-01-04T19:00:00+00:00"
TZ = "America/New_York"
EDIT_QTY = 2.0


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def client():
    main.app.dependency_overrides[auth.require_user] = override_user(
        email="billzhangsc@gmail.com", user_id="e2e-m1"
    )
    try:
        with TestClient(main.app) as c:
            yield c
    finally:
        main.app.dependency_overrides.pop(auth.require_user, None)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _resolve_all(client: TestClient, log_id: str, items: list[dict]) -> dict[int, dict]:
    """§3.4 client contract: kick tier 3 for searching rows; tap an id-bearing
    question option when asked; fall back to the §5 manual-search escape hatch
    for anything still unresolved. Returns resolved items by item_index."""
    resolved: dict[int, dict] = {}
    for it in items:
        cur = it
        if cur["status"] == "searching":
            r = client.post(
                "/api/resolve/tier3",
                json={"log_id": log_id, "item_index": cur["item_index"]},
            )
            assert r.status_code == 200, r.text
            cur = r.json()
        if cur["status"] == "needs_input" and cur.get("question"):
            opt = next(
                (
                    o
                    for o in cur["question"]["options"]
                    if o.get("source") and o.get("source_id")
                ),
                None,
            )
            if opt is not None:  # id-bearing tap resolves locally (§3.4)
                r = client.post(
                    "/api/resolve/answer",
                    json={
                        "log_id": log_id,
                        "item_index": cur["item_index"],
                        "answer": {
                            "label": opt["label"],
                            "source": opt["source"],
                            "source_id": opt["source_id"],
                        },
                    },
                )
                assert r.status_code == 200, r.text
                cur = r.json()
        if cur["status"] in ("needs_input", "not_found"):
            # the boring escape hatch (§5): manual search, select first hit
            r = client.get(
                "/api/foods/search", params={"q": cur["phrase"], "source": "all"}
            )
            assert r.status_code == 200, r.text
            body = r.json()
            hits = body["local"] + body["generic"] + body["branded"]
            assert hits, f"manual search found nothing for {cur['phrase']!r}"
            hit = hits[0]
            if hit.get("food_id"):  # cached food → answer chip path
                r = client.post(
                    "/api/resolve/answer",
                    json={
                        "log_id": log_id,
                        "item_index": cur["item_index"],
                        "answer": {"label": hit["label"], "food_id": hit["food_id"]},
                    },
                )
            else:
                r = client.post(
                    "/api/foods/select",
                    json={
                        "source": hit["source"],
                        "source_id": hit["source_id"],
                        "display_name": hit["label"],
                        "log_id": log_id,
                        "item_index": cur["item_index"],
                    },
                )
                assert r.status_code == 200, r.text
                cur = r.json()["item"]
                assert cur is not None, "select degraded to a food-less item"
                resolved[cur["item_index"]] = cur
                continue
            assert r.status_code == 200, r.text
            cur = r.json()
        assert cur["status"] == "resolved", cur
        resolved[cur["item_index"]] = cur
    return resolved


async def _cleanup(t0, log_ids: list[str]) -> None:
    await db.execute("DELETE FROM entries WHERE log_id = ANY(%s::uuid[])", (log_ids,))
    alias_rows = await db.fetchall(
        "SELECT DISTINCT alias_id FROM item_resolutions "
        "WHERE log_id = ANY(%s::uuid[]) AND alias_id IS NOT NULL",
        (log_ids,),
    )
    alias_ids = [str(r["alias_id"]) for r in alias_rows]
    await db.execute(
        "DELETE FROM item_resolutions WHERE log_id = ANY(%s::uuid[])", (log_ids,)
    )
    await db.execute("DELETE FROM logs WHERE id = ANY(%s::uuid[])", (log_ids,))
    if alias_ids:  # only aliases THIS test created — upserted older rows stay
        await db.execute(
            "DELETE FROM aliases WHERE id = ANY(%s::uuid[]) AND created_at >= %s",
            (alias_ids, t0),
        )
    new_foods = await db.fetchall(
        "SELECT id FROM foods WHERE created_at >= %s", (t0,)
    )
    for row in new_foods:
        try:
            await db.execute("DELETE FROM foods WHERE id = %s", (row["id"],))
        except Exception:
            pass  # still referenced by a pre-existing row — leave it cached


async def _res_rows(log_id: str) -> dict[int, dict]:
    rows = await db.fetchall(
        "SELECT * FROM item_resolutions WHERE log_id = %s ORDER BY item_index",
        (log_id,),
    )
    return {r["item_index"]: r for r in rows}


# ---------------------------------------------------------------------------
# the story
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("needs_002")  # real commit path: alias learning targets 002's constraint
def test_m1_full_story(client: TestClient):
    t0 = _run(db.fetchone("SELECT now() AS t"))["t"]
    l1, l2 = str(uuid4()), str(uuid4())
    try:
        # -- 1. log "chicken and rice" ------------------------------------
        r = client.post(
            "/api/logs",
            json={
                "id": l1,
                "text": UTTERANCE,
                "captured_at": CAPTURED_AT_1,
                "tz": TZ,
            },
        )
        assert r.status_code == 200, r.text
        log1 = r.json()
        assert log1["items"], "the agent staged no items"
        phrases = [it["phrase"] for it in log1["items"]]

        # the story requires a cold start — pre-existing aliases would flip
        # the first log to tier 1 and the assertions below would test nothing
        pre = _run(
            db.fetchall(
                "SELECT alias_norm FROM aliases WHERE alias_norm = ANY(%s) "
                "AND created_at < %s",
                ([norm(p) for p in phrases] + [norm(UTTERANCE)], t0),
            )
        )
        if pre:
            pytest.skip(f"pre-existing aliases for {[p['alias_norm'] for p in pre]}")

        items1 = _resolve_all(client, l1, log1["items"])
        assert len(items1) >= 2, f"expected >=2 items from {UTTERANCE!r}: {phrases}"
        idx0 = min(items1)  # the item whose portion gets corrected
        edited_phrase = items1[idx0]["phrase"]

        # every resolved row carries the food's serving list (§7.1 portion sheet)
        for it in items1.values():
            assert it.get("servings"), f"no servings in staged payload: {it}"
            assert all(s["serving_id"] and s["description"] for s in it["servings"])

        # -- 2. EDIT a portion via the resolve path, then commit ----------
        r = client.post(
            "/api/resolve",
            json={
                "log_id": l1,
                "item_index": idx0,
                "phrase": edited_phrase,
                "qty": EDIT_QTY,
            },
        )
        assert r.status_code == 200, r.text
        edited = r.json()
        if edited["status"] != "resolved":
            # A never-committed phrase has no alias yet, so tiers 1–2 can't
            # fire and /api/resolve re-enters tier 3 (§5) — where the agent may
            # ask "which chicken?" again (observed live, Phase 3). Answer it
            # through the same client contract as step 1; the spoken qty rides
            # the staged payload into the answer (§7.3 rule 1).
            edited = _resolve_all(client, l1, [edited])[idx0]
        assert edited["status"] == "resolved", edited
        assert edited["qty"] == EDIT_QTY
        items1[idx0] = edited

        r = client.post(
            "/api/logs/{}/commit".format(l1),
            json={"items": list(items1.values())},
        )
        assert r.status_code == 200, r.text
        totals1 = r.json()
        assert totals1["kcal"] > 0

        rows1 = _run(_res_rows(l1))
        assert rows1[idx0]["was_edited"] is True  # §9: the honest edit signal
        for idx, it in items1.items():
            assert rows1[idx]["committed_at"] is not None
            assert rows1[idx]["alias_id"] is not None, "alias not learned"
        # the correction became the alias default (mode of last 10 = the edit)
        alias0 = _run(
            db.fetchone(
                "SELECT * FROM aliases WHERE id = %s", (rows1[idx0]["alias_id"],)
            )
        )
        assert alias0 is not None
        assert float(alias0["default_qty"]) == EDIT_QTY

        # -- 3. re-log the same phrase → tier 1/2, zero network, edit
        #       pre-filled → zero-edit one-tap commit -----------------------
        r = client.post(
            "/api/logs",
            json={
                "id": l2,
                "text": UTTERANCE,
                "captured_at": CAPTURED_AT_2,
                "tz": TZ,
            },
        )
        assert r.status_code == 200, r.text
        log2 = r.json()
        assert len(log2["items"]) >= 2
        for it in log2["items"]:
            assert it["status"] == "resolved", f"repeat item not resolved: {it}"
            assert it["tier"] in (1, 2), f"repeat item escaped tiers 1-2: {it}"

        repeat0 = next(
            (
                it
                for it in log2["items"]
                if norm(it["phrase"]) == norm(edited_phrase)
            ),
            None,
        )
        assert repeat0 is not None, (
            f"the agent's phrasing drifted: {[i['phrase'] for i in log2['items']]}"
        )
        # the correction changed the very next proposal (M1 gate)
        assert repeat0["qty"] == EDIT_QTY
        assert repeat0["serving"]["id"] == items1[idx0]["serving"]["id"]

        rows2 = _run(_res_rows(l2))
        assert all(r["network_calls"] == 0 for r in rows2.values()), (
            "repeat log touched the network"
        )

        r = client.post(
            "/api/logs/{}/commit".format(l2),
            json={"items": log2["items"]},  # zero edits, one tap
        )
        assert r.status_code == 200, r.text

        log2_row = _run(db.fetchone("SELECT * FROM logs WHERE id = %s", (l2,)))
        assert log2_row["status"] == "committed"
        assert log2_row["edited_items"] == 0  # zero-edit commit, recorded as such
        rows2 = _run(_res_rows(l2))
        assert all(r["was_edited"] is False for r in rows2.values())

        # -- 4. qty-PATCH an entry → snapshot scales proportionally --------
        entry = _run(
            db.fetchone(
                "SELECT * FROM entries WHERE log_id = %s AND item_index = %s",
                (l2, repeat0["item_index"]),
            )
        )
        assert entry is not None
        old_qty = float(entry["qty"])
        old_macros = dict(entry["macros"])
        new_qty = old_qty * 1.5
        r = client.patch(f"/api/entries/{entry['id']}", json={"qty": new_qty})
        assert r.status_code == 200, r.text
        patched = r.json()["entry"]
        assert patched["qty"] == new_qty
        for k in ("kcal", "protein_g", "carb_g", "fat_g"):
            assert patched["macros"][k] == pytest.approx(
                old_macros[k] * 1.5, rel=1e-3, abs=0.02
            ), f"{k} did not scale from the snapshot"

        # day totals reflect the scaled snapshot (§4: SUM over entries)
        r = client.get("/api/diary", params={"date": "2000-01-04"})
        assert r.status_code == 200, r.text
        assert r.json()["totals"]["kcal"] > totals1["kcal"]
    finally:
        _run(_cleanup(t0, [l1, l2]))
