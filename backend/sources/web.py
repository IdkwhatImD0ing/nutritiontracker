"""Web page fetch adapter — the tier-3 web lane's deterministic fallback
(DESIGN.md §3.4, §13).

``fetch_page(url)`` is a plain ``httpx`` GET plus readability-lite text
extraction, size-capped on both the download and the returned text. It exists
for chain nutrition pages the hosted ``web_search`` tool handles poorly
(JS calculators, PDF menus): no JS is executed, so the tool returns whatever
text ships in the HTML — enough for server-rendered nutrition tables, and an
honest error for binary/PDF content.

Policy errors (bad scheme, blocked host, non-text content, HTTP >= 400) are
returned as ``{"url", "error"}`` dicts — deterministic tool results the agent
can react to. Network failures raise and are handled by the caller's tool
failure function.
"""

from __future__ import annotations

import html as _html
import ipaddress
import re
from urllib.parse import urlsplit

import httpx

_TIMEOUT = 20.0
_MAX_FETCH_BYTES = 2_000_000  # download cap
_MAX_TEXT_CHARS = 15_000  # extracted-text cap (what the agent sees)
_USER_AGENT = "TallyNutritionLogger/0.1 (personal single-user nutrition app)"

_TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")

# -- readability-lite ---------------------------------------------------------

_COMMENT_RE = re.compile(r"(?s)<!--.*?-->")
_BLOCK_RE = re.compile(r"(?is)<(script|style|noscript|template|svg|iframe|head)\b.*?</\1\s*>")
_CHROME_RE = re.compile(r"(?is)<(nav|header|footer|aside|form)\b.*?</\1\s*>")
_TITLE_RE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")
_MAIN_RE = re.compile(r"(?is)<(main|article)\b[^>]*>(.*?)</\1\s*>")
_BREAK_RE = re.compile(r"(?i)</(?:p|div|li|tr|h[1-6]|table|section|ul|ol|dl)>|<br\s*/?>")
_TAG_RE = re.compile(r"(?s)<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NL_RE = re.compile(r"\n\s*\n+")


def _blocked_host(host: str) -> bool:
    """Refuse obviously non-public hosts (the URL is model-supplied)."""
    host = (host or "").strip().lower().rstrip(".")
    if not host or host == "localhost" or host.endswith((".local", ".internal")):
        return True
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        return False  # a normal DNS name


def _collapse(text: str) -> str:
    text = _WS_RE.sub(" ", text)
    text = _NL_RE.sub("\n", text)
    return "\n".join(ln.strip() for ln in text.split("\n") if ln.strip())


def _extract(html_text: str) -> tuple[str | None, str]:
    """(title, text) from raw HTML: strip script/style/head, prefer the largest
    <main>/<article> block (else drop nav/header/footer/aside chrome), turn
    structural closers into newlines, strip tags, unescape entities."""
    m = _TITLE_RE.search(html_text)
    title = _WS_RE.sub(" ", _html.unescape(m.group(1))).strip()[:300] if m else None

    body = _COMMENT_RE.sub(" ", html_text)
    body = _BLOCK_RE.sub(" ", body)
    mains = [mm.group(2) for mm in _MAIN_RE.finditer(body)]
    body = max(mains, key=len) if mains else _CHROME_RE.sub(" ", body)
    body = _BREAK_RE.sub("\n", body)
    text = _TAG_RE.sub(" ", body)
    return title, _collapse(_html.unescape(text))


async def fetch_page(url: str) -> dict:
    """Fetch one page and return ``{url, title, text, truncated}`` (or
    ``{url, error}`` on a policy refusal). ``url`` in the result is the final
    URL after redirects."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return {"url": url, "error": "only http(s) URLs can be fetched"}
    if _blocked_host(parts.hostname or ""):
        return {"url": url, "error": "host not allowed"}

    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
    }
    async with httpx.AsyncClient(
        timeout=_TIMEOUT, follow_redirects=True, headers=headers
    ) as client:
        async with client.stream("GET", url) as r:
            final_url = str(r.url)
            if _blocked_host(r.url.host or ""):  # redirect landed somewhere private
                return {"url": final_url, "error": "host not allowed"}
            if r.status_code >= 400:
                return {"url": final_url, "error": f"HTTP {r.status_code}"}
            ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
            if ctype and ctype not in _TEXT_TYPES:
                return {
                    "url": final_url,
                    "error": f"unsupported content type {ctype!r} — only HTML/plain-text "
                             "pages can be extracted (PDFs and binaries cannot)",
                }
            chunks: list[bytes] = []
            size = 0
            fetch_truncated = False
            async for chunk in r.aiter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size >= _MAX_FETCH_BYTES:
                    fetch_truncated = True
                    break
            raw = b"".join(chunks)[:_MAX_FETCH_BYTES]
            encoding = r.encoding or "utf-8"

    decoded = raw.decode(encoding, errors="replace")
    if ctype == "text/plain":
        title, text = None, _collapse(decoded)
    else:
        title, text = _extract(decoded)
    return {
        "url": final_url,
        "title": title,
        "text": text[:_MAX_TEXT_CHARS],
        "truncated": fetch_truncated or len(text) > _MAX_TEXT_CHARS,
    }
