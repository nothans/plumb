"""Log in, sign up, log out, and the personal dashboard (with API tokens)."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from .. import audit, domain, ratelimit
from ..cli import demo_tokens
from ..db import now, parse_ts, to_ts, transaction
from ..security import hash_password, new_token, password_problem, token_hash, verify_password
from ..web import SESSION_COOKIE, actor, client_ip, conn_for, form_body, redirect, render

router = APIRouter(include_in_schema=False)  # HTML pages; the JSON API is in api.py
SESSION_DAYS = 14


def _safe_next(url: str | None) -> str:
    url = url or "/"
    return url if url.startswith("/") and not url.startswith("//") else "/"


def start_session(request: Request, user_id: str, next_url: str, notice: str | None = None):
    conn = conn_for(request)
    token = new_token()
    with transaction(conn):
        domain.prune_sessions(conn)
        conn.execute(
            "INSERT INTO sessions(token_hash, user_id, csrf, created_at, expires_at) VALUES (?,?,?,?,?)",
            (token_hash(token), user_id, new_token(18), now(), to_ts(parse_ts(now()) + timedelta(days=SESSION_DAYS))),
        )
        audit.append(conn, "session.started", actor_id=user_id, subject=user_id, ip=client_ip(request))
    response = redirect(_safe_next(next_url), notice)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                        secure=request.app.state.settings.secure_cookies, max_age=SESSION_DAYS * 86400)
    return response


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    return render(request, "login.html", next=_safe_next(next), email="")


@router.post("/login")
def login(request: Request, data: dict = Depends(form_body)):
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
def signup(request: Request, data: dict = Depends(form_body)):
    next_url = _safe_next(data.get("next"))
    ratelimit.check(request.app.state.limiter, "signup", client_ip(request))
    password = str(data.get("password", ""))
    try:
        if problem := password_problem(password, str(data.get("email", ""))):
            raise domain.Invalid(problem)
        uid = domain.create_user(conn_for(request), str(data.get("email", "")), str(data.get("name", "")),
                                  hash_password(password), ip=client_ip(request))
    except domain.DomainError as exc:
        return render(request, "signup.html", status_code=exc.status, error=exc.message, next=next_url,
                      form={"email": data.get("email", ""), "name": data.get("name", "")})
    return start_session(request, uid, next_url)


@router.post("/logout")
def logout(request: Request, _: dict = Depends(form_body)):
    token = request.cookies.get(SESSION_COOKIE)
    # The demo's fixed sessions are shared by everyone holding .dogfood.toml;
    # one visitor logging out must not break them for the checker and the
    # other judges. Their cookie is cleared, the shared session stays.
    if token and not (request.app.state.settings.demo and token in demo_tokens()):
        conn = conn_for(request)
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))
    response = redirect("/", "Logged out.")
    response.delete_cookie(SESSION_COOKIE)
    return response


def _dashboard(request: Request, me: domain.Actor, new_token_value: str | None = None, *, error: str | None = None,
               status_code: int = 200):
    conn = conn_for(request)
    rows = []
    for e in domain.list_events(conn, archived=None):
        r = domain.roles(conn, e["id"], me)
        if r - {"admin"}:
            rows.append({"event": e, "roles": r, "team": domain.team_for(conn, e["id"], me.id), "phase": domain.phase(e)})
    tokens = domain.api_tokens(conn, me)
    return render(request, "me.html", status_code=status_code, rows=rows, tokens=tokens, new_token=new_token_value,
                  error=error, demo=request.app.state.settings.demo)


@router.post("/me/password", response_class=HTMLResponse)
def change_password(request: Request, data: dict = Depends(form_body)):
    """Change your own password. Every session and API token of the account
    ends (whoever might have had the old password is out); this browser gets
    a fresh session."""
    me = domain.require_user(actor(request))
    conn = conn_for(request)
    try:
        if request.app.state.settings.demo:
            raise domain.Conflict("demo logins are shared by everyone trying the demo, so their passwords stay as published")
        ratelimit.check(request.app.state.limiter, "login", me.email.lower())
        user = domain.get_user(conn, me.id)
        if not verify_password(str(data.get("current", "")), user["password_hash"]):
            raise domain.Invalid("the current password is not right")
        new = str(data.get("password", ""))
        if problem := password_problem(new, me.email):
            raise domain.Invalid(problem)
        with transaction(conn):
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(new), me.id))
            domain.end_credentials(conn, me.id)
            audit.append(conn, "user.password_changed", actor_id=me.id, subject=me.id, ip=client_ip(request))
    except domain.DomainError as exc:
        return _dashboard(request, me, error=exc.message, status_code=exc.status)
    return start_session(request, me.id, "/me", "Password changed. Every other session and API token has ended.")


@router.get("/me", response_class=HTMLResponse)
def dashboard(request: Request):
    return _dashboard(request, domain.require_user(actor(request)))


@router.post("/me/tokens/revoke")
def revoke_token(request: Request, data: dict = Depends(form_body)):
    me = domain.require_user(actor(request))
    domain.revoke_api_token(conn_for(request), me, str(data.get("hint", "")))
    return redirect("/me", "Token revoked.")


@router.post("/me/tokens", response_class=HTMLResponse)
def create_token(request: Request, data: dict = Depends(form_body)):
    me = domain.require_user(actor(request))
    token = domain.create_api_token(conn_for(request), me, str(data.get("label", "")), data.get("days", 90))
    # Shown once, never stored in the clear.
    return _dashboard(request, me, token)
