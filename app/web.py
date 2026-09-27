"""Web plumbing shared by every route: the connection, the actor, CSRF,
request bodies, rendering, and turning domain errors into responses."""

from __future__ import annotations

import hmac
import sqlite3
from typing import Any
from urllib.parse import quote, unquote, urlparse

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import domain
from .db import connect, now
from .security import new_token, token_hash

SESSION_COOKIE = "plumb_session"
ANON_CSRF_COOKIE = "plumb_csrf"
FLASH_COOKIE = "plumb_flash"

templates = Jinja2Templates(directory=str(__import__("pathlib").Path(__file__).with_name("templates")))


def conn_for(request: Request) -> sqlite3.Connection:
    """One connection per request, closed by the middleware in main.py."""
    conn = getattr(request.state, "conn", None)
    if conn is None:
        conn = connect(request.app.state.settings.database_path)
        request.state.conn = conn
    return conn


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _session(request: Request) -> sqlite3.Row | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return conn_for(request).execute(
        "SELECT s.csrf, u.* FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ? AND s.expires_at > ?",
        (token_hash(token), now()),
    ).fetchone()


def _bearer(request: Request) -> sqlite3.Row | None:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    conn = conn_for(request)
    th = token_hash(auth[7:].strip())
    row = conn.execute(
        "SELECT u.* FROM api_tokens t JOIN users u ON u.id = t.user_id WHERE t.token_hash = ?", (th,)
    ).fetchone()
    if row is not None:
        conn.execute("UPDATE api_tokens SET last_used_at = ? WHERE token_hash = ?", (now(), th))
    return row


def actor(request: Request) -> domain.Actor | None:
    if hasattr(request.state, "actor"):
        return request.state.actor
    row = _bearer(request)
    via = "bearer" if row is not None else None
    if row is None:
        row = _session(request)
        via = "session" if row is not None else None
    request.state.auth_via = via
    request.state.csrf = row["csrf"] if via == "session" else None
    request.state.actor = (
        domain.Actor(id=row["id"], email=row["email"], name=row["name"], is_admin=bool(row["is_admin"]), ip=client_ip(request))
        if row is not None else None
    )
    return request.state.actor


def csrf_token(request: Request) -> str:
    actor(request)
    if request.state.csrf:
        return request.state.csrf
    token = request.cookies.get(ANON_CSRF_COOKIE)
    if not token:
        token = new_token(18)
        request.state.set_anon_csrf = token
    return token


def same_origin(request: Request) -> bool:
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return True  # non-browser clients (curl, the checker) send neither
    host = request.headers.get("host", "")
    return urlparse(origin).netloc == host


async def body(request: Request) -> dict[str, Any]:
    """The request body as a dict, from JSON or a form, with CSRF enforced.

    Bearer-token requests are not cookie-authenticated, so they cannot be
    forged cross-site and skip the check. JSON requests cannot be sent
    cross-site without a CORS preflight that Plumb never grants, so an
    Origin match is enough. Form posts must echo the CSRF token.
    """
    actor(request)
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            data = await request.json()
        except ValueError as exc:
            raise domain.Invalid("the request body is not valid JSON") from exc
        if not isinstance(data, dict):
            raise domain.Invalid("the request body must be a JSON object")
        if request.state.auth_via != "bearer" and not same_origin(request):
            raise domain.Forbidden("cross-origin request refused")
        return data
    form = await request.form()
    data: dict[str, Any] = {}
    for key in form.keys():
        values = form.getlist(key)
        data[key] = values if len(values) > 1 or key.endswith("[]") else values[0]
    if request.state.auth_via != "bearer":
        expected = request.state.csrf or request.cookies.get(ANON_CSRF_COOKIE)
        sent = str(data.get("csrf") or "")
        if not expected or not hmac.compare_digest(sent, expected):
            raise domain.Forbidden("this form expired; reload the page and try again")
    data.pop("csrf", None)
    return data


def wants_json(request: Request) -> bool:
    if request.url.path.startswith("/api/") or request.url.path.endswith(".json"):
        return True
    accept = request.headers.get("accept", "")
    ctype = request.headers.get("content-type", "")
    return "application/json" in ctype or ("application/json" in accept and "text/html" not in accept)


def base_url(request: Request) -> str:
    """Links the portal hands out (invitations, team invites, embeds). An
    explicit PLUMB_BASE_URL wins; otherwise the address the visitor used."""
    configured = request.app.state.settings.base_url
    return configured or str(request.base_url).rstrip("/")


def signing_base_url(request: Request) -> str:
    """The issuer written into signed records: only ever the configured
    address, never the request's Host header."""
    return request.app.state.settings.base_url


def render(request: Request, template: str, status_code: int = 200, **ctx: Any) -> HTMLResponse:
    a = actor(request)
    ctx.setdefault("error", None)
    # Notices come from a cookie set by our own redirect, never from the URL,
    # so nobody can craft a link that shows a fake success message.
    flash = request.cookies.get(FLASH_COOKIE)
    ctx.setdefault("notice", unquote(flash) if flash else None)
    ctx.setdefault("base_url", base_url(request))
    response = templates.TemplateResponse(
        request, template, {"me": a, "csrf": csrf_token(request), **ctx}, status_code=status_code
    )
    if flash:
        response.delete_cookie(FLASH_COOKIE)
    return response


_secure_cookies = False  # set from settings by main.create_app


def redirect(url: str, notice: str | None = None) -> RedirectResponse:
    response = RedirectResponse(url, status_code=303)
    if notice:
        response.set_cookie(FLASH_COOKIE, quote(notice), max_age=60, httponly=True, samesite="lax",
                            secure=_secure_cookies)
    return response


def error_response(request: Request, exc: domain.DomainError) -> Response:
    if wants_json(request):
        return JSONResponse({"error": exc.message}, status_code=exc.status)
    if isinstance(exc, domain.Unauthorized) and request.method == "GET":
        return RedirectResponse(f"/login?next={quote(str(request.url.path))}", status_code=303)
    return render(request, "error.html", status_code=exc.status, message=exc.message, status=exc.status)


# --- template filters ----------------------------------------------------------


def _fmt_ts_clean(value: str | None) -> str:
    if not value:
        return ""
    # "2026-03-01T18:00:00Z" -> "2026-03-01 18:00 UTC"
    return f"{value[:10]} {value[11:16]} UTC"


def _local_input(value: str | None) -> str:
    """ISO UTC -> the value an <input type=datetime-local> expects."""
    return value[:16] if value else ""


templates.env.filters["ts"] = _fmt_ts_clean
templates.env.filters["dtlocal"] = _local_input
templates.env.filters["num"] = lambda v, d=1: "" if v is None else f"{v:.{d}f}"
templates.env.filters["pct"] = lambda v: "" if v is None else f"{100 * v:.0f}%"
