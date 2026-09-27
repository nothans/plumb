"""Log in, sign up, log out, and the personal dashboard (with API tokens)."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .. import audit, domain, ratelimit
from ..db import now, parse_ts, to_ts, transaction
from ..security import hash_password, new_token, token_hash, verify_password
from ..web import SESSION_COOKIE, actor, body, client_ip, conn_for, redirect, render

router = APIRouter(include_in_schema=False)  # HTML pages; the JSON API is in api.py
SESSION_DAYS = 14


def _safe_next(url: str | None) -> str:
    url = url or "/"
    return url if url.startswith("/") and not url.startswith("//") else "/"


def start_session(request: Request, user_id: str, next_url: str):
    conn = conn_for(request)
    token = new_token()
    with transaction(conn):
        conn.execute(
            "INSERT INTO sessions(token_hash, user_id, csrf, created_at, expires_at) VALUES (?,?,?,?,?)",
            (token_hash(token), user_id, new_token(18), now(), to_ts(parse_ts(now()) + timedelta(days=SESSION_DAYS))),
        )
        audit.append(conn, "session.started", actor_id=user_id, subject=user_id, ip=client_ip(request))
    response = redirect(_safe_next(next_url))
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                        secure=request.app.state.settings.secure_cookies, max_age=SESSION_DAYS * 86400)
    return response


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    return render(request, "login.html", next=_safe_next(next), email="")


@router.post("/login")
async def login(request: Request):
    data = await body(request)
    email = str(data.get("email", "")).strip().lower()
    next_url = _safe_next(data.get("next"))
    ratelimit.check(request.app.state.limiter, "login", email)
    ratelimit.check(request.app.state.limiter, "login-ip", client_ip(request))
    conn = conn_for(request)
    user = domain.user_by_email(conn, email) if email else None
    if not verify_password(str(data.get("password", "")), user["password_hash"] if user else None):
        return render(request, "login.html", status_code=401, error="That email and password do not match.",
                      next=next_url, email=email)
    return start_session(request, user["id"], next_url)


@router.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request, next: str = "/"):
    return render(request, "signup.html", next=_safe_next(next), form={})


@router.post("/signup")
async def signup(request: Request):
    data = await body(request)
    next_url = _safe_next(data.get("next"))
    ratelimit.check(request.app.state.limiter, "signup", client_ip(request))
    password = str(data.get("password", ""))
    try:
        if len(password) < 10:
            raise domain.Invalid("use a password of at least 10 characters")
        uid = domain.create_user(conn_for(request), str(data.get("email", "")), str(data.get("name", "")),
                                  hash_password(password), ip=client_ip(request))
    except domain.DomainError as exc:
        return render(request, "signup.html", status_code=exc.status, error=exc.message, next=next_url,
                      form={"email": data.get("email", ""), "name": data.get("name", "")})
    return start_session(request, uid, next_url)


@router.post("/logout")
async def logout(request: Request):
    await body(request)
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        conn = conn_for(request)
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))
    response = redirect("/", "Logged out.")
    response.delete_cookie(SESSION_COOKIE)
    return response


def _dashboard(request: Request, me: domain.Actor, new_token_value: str | None = None):
    conn = conn_for(request)
    rows = []
    for e in domain.list_events(conn):
        r = domain.roles(conn, e["id"], me)
        if r - {"admin"}:
            rows.append({"event": e, "roles": r, "team": domain.team_for(conn, e["id"], me.id), "phase": domain.phase(e)})
    tokens = conn.execute(
        "SELECT label, created_at, last_used_at, substr(token_hash, 1, 8) AS hint FROM api_tokens WHERE user_id = ? "
        "ORDER BY created_at DESC", (me.id,)
    ).fetchall()
    return render(request, "me.html", rows=rows, tokens=tokens, new_token=new_token_value)


@router.get("/me", response_class=HTMLResponse)
def dashboard(request: Request):
    return _dashboard(request, domain.require_user(actor(request)))


@router.post("/me/tokens/revoke")
async def revoke_token(request: Request):
    data = await body(request)
    me = domain.require_user(actor(request))
    domain.revoke_api_token(conn_for(request), me, str(data.get("hint", "")))
    return redirect("/me", "Token revoked.")


@router.post("/me/tokens", response_class=HTMLResponse)
async def create_token(request: Request):
    data = await body(request)
    me = domain.require_user(actor(request))
    token = domain.create_api_token(conn_for(request), me, str(data.get("label", "")))
    # Shown once, never stored in the clear.
    return _dashboard(request, me, token)
