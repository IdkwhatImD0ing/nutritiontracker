"""The places around a photo taken now — DESIGN.md §3.4b (2026-09-12).

The photo lane's web lane can only look a menu up when something names a
place (routes/photo._photo_context), and until now only the profile notes
could. The phone knows where it is: the photo sheet asks for its location
when it opens, a CAMERA shot carries the fix (a library pick may have been
taken anywhere), and this module turns it into the places around it, nearest
first, for the vision call to weigh.

Google Places API (New) Nearby Search, read on developers.google.com
2026-09-12: POST places:searchNearby, the fields named in X-Goog-FieldMask.
displayName, primaryTypeDisplayName and shortFormattedAddress are all
Nearby Search Pro fields — one Pro call per photo, 5,000 free a month, then
$32 per 1,000. Owner only (§8.9), like the web lane it feeds.

Best effort, always: no key, a fix too coarse to name a place, a slow or
failing Google — each answers an empty list and the photo goes on exactly as
before. The coordinates are never stored and never traced; the place names
are, because they are what the model was told.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from typing import NamedTuple

import httpx

log = logging.getLogger(__name__)

_URL = "https://places.googleapis.com/v1/places:searchNearby"
_FIELDS = "places.displayName,places.primaryTypeDisplayName,places.shortFormattedAddress"
_TIMEOUT_S = 3.0  # the whole lookup: it runs before vision, on the slowest lane in the app
_MAX_RESULTS = 5
_MAX_TEXT = 80  # one name, type label or address, as the model reads it
# The circle is the fix's own uncertainty, floored so a precise fix still
# reaches the building it is in. A fix vaguer than the ceiling cannot tell one
# restaurant from the next, and a wrong place is worse than none: the model
# would go and read the wrong menu.
_MIN_RADIUS_M = 50.0
_MAX_ACCURACY_M = 200.0


class Fix(NamedTuple):
    """Where the phone was: the Geolocation API's coords, accuracy in meters."""

    lat: float
    lng: float
    accuracy_m: float


class Place(NamedTuple):
    name: str
    kind: str | None  # Google's own label for its primary type: "Mexican restaurant"
    address: str | None

    def line(self) -> str:
        """One line for the model: "Chipotle (Mexican restaurant), 1 Main St, SF"."""
        return self.name + (f" ({self.kind})" if self.kind else "") + (
            f", {self.address}" if self.address else ""
        )


def fix(
    lat: str | float | None, lng: str | float | None, accuracy: str | float | None
) -> Fix | None:
    """The form's three fields as a Fix — None when any is missing or is not a
    real coordinate. A location is a hint, so a bad one is dropped rather than
    refused: it must never cost the person their photo. That is why the route
    takes them as strings — a float field would 422 the whole turn instead."""
    try:
        where = Fix(float(lat), float(lng), float(accuracy))
    except (TypeError, ValueError):  # missing, or not a number
        return None
    if not all(map(math.isfinite, where)):
        return None
    if not (-90 <= where.lat <= 90 and -180 <= where.lng <= 180 and where.accuracy_m >= 0):
        return None
    return where


def _client() -> httpx.AsyncClient:  # the seam the tests hand a MockTransport through
    return httpx.AsyncClient(timeout=_TIMEOUT_S)


def _clean(text: object) -> str:
    """Google's text as one short line. Anyone can name a business, and a
    newline in a name would break out of the list the vision call reads."""
    return " ".join(str(text or "").split())[:_MAX_TEXT]


def _place(raw: dict) -> Place | None:
    name = _clean((raw.get("displayName") or {}).get("text"))
    if not name:
        return None
    kind = _clean((raw.get("primaryTypeDisplayName") or {}).get("text"))
    address = _clean(raw.get("shortFormattedAddress"))
    return Place(name, kind or None, address or None)


async def nearby(where: Fix) -> list[Place]:
    """The places around `where`, nearest first — empty when there is nothing
    worth saying (the module docstring lists when)."""
    key = os.environ.get("GOOGLE_PLACES_API_KEY", "").strip()
    if not key or where.accuracy_m > _MAX_ACCURACY_M:
        return []
    import tracing  # lazy: stubbed in route tests

    radius = max(where.accuracy_m, _MIN_RADIUS_M)
    async with tracing.run(
        "places", kind="retriever", provider="google",
        # never the coordinates — how sure the fix was is all a trace needs
        input={"accuracy_m": round(where.accuracy_m), "radius_m": round(radius)},
    ) as span:
        try:
            # a TOTAL cap: httpx's own timeout is per phase (connect, write,
            # each read), so a slow edge could otherwise hold vision for ~10 s
            async with asyncio.timeout(_TIMEOUT_S), _client() as client:
                r = await client.post(
                    _URL,
                    json={
                        "maxResultCount": _MAX_RESULTS,
                        "rankPreference": "DISTANCE",
                        "locationRestriction": {
                            "circle": {
                                "center": {"latitude": where.lat, "longitude": where.lng},
                                "radius": radius,
                            }
                        },
                    },
                    headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": _FIELDS},
                )
            r.raise_for_status()
            body = r.json()
            raw = body.get("places") if isinstance(body, dict) else None
            found = [p for p in map(_place, raw or []) if p is not None]
        except Exception as e:  # slow, refused, not JSON, or a shape we did not expect
            log.warning("places lookup failed: %s", type(e).__name__)
            span.set(output={"error": type(e).__name__}, status="error")
            return []
        # names only: with their addresses, a trace kept for good would be a
        # map of every meal ever photographed
        span.set(output=[p.name for p in found])
        return found
