"""The judge side: invitations, the review queue, and the scoring form."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .. import domain
from ..db import transaction
from ..security import hash_password
from ..web import actor, body, client_ip, conn_for, redirect, render
from .account import start_session

router = APIRouter(include_in_schema=False)  # HTML pages; the JSON API is in api.py


@router.get("/invitations/{token}", response_class=HTMLResponse)
def invitation(request: Request, token: str):
    conn = conn_for(request)
    inv = domain.get_invitation(conn, token)
    existing = domain.user_by_email(conn, inv["email"])
    return render(request, "invitation.html", inv=inv, token=token,
                  needs_account=existing is None or existing["password_hash"] is None,
                  can_claim=domain.may_claim_with(conn, inv), used=inv["accepted_at"] is not None)


@router.post("/invitations/{token}")
async def accept(request: Request, token: str):
    data = await body(request)
    conn = conn_for(request)
    inv = domain.get_invitation(conn, token)
    me = actor(request)
    if me is None:
        # Holding the link is the proof of the address, so it may create or
        # claim the invited account, and only that one, and only if the
        # account does not already belong to another event (see
        # domain.may_claim_with). Claim and acceptance are one transaction.
        password = str(data.get("password", ""))
        try:
            if len(password) < 10:
                raise domain.Invalid("use a password of at least 10 characters")
            if inv["accepted_at"]:
                raise domain.Conflict("this invitation has already been used")
            if not domain.may_claim_with(conn, inv):
                raise domain.Forbidden("this address already has an account; log in to accept")
            with transaction(conn):
                uid = domain.create_user(conn, inv["email"], str(data.get("name", "")), hash_password(password),
                                         ip=client_ip(request), allow_claim=True)
                user = domain.get_user(conn, uid)
                me = domain.Actor(id=uid, email=user["email"], name=user["name"], is_admin=bool(user["is_admin"]),
                                  ip=client_ip(request))
                event_id = domain.accept_invitation(conn, me, token)
        except domain.DomainError as exc:
            return render(request, "invitation.html", status_code=exc.status, inv=inv, token=token, needs_account=True,
                          can_claim=False, used=False, error=exc.message)
        return start_session(request, uid, f"/events/{event_id}")
    event_id = domain.accept_invitation(conn, me, token)
    return redirect(f"/events/{event_id}", f"You are now a {inv['role']} for {inv['event_name']}.")


@router.get("/events/{event_id}/judge", response_class=HTMLResponse)
def queue(request: Request, event_id: str):
    conn = conn_for(request)
    me = actor(request)
    rows = domain.judge_queue(conn, me, event_id)
    event = domain.get_event(conn, event_id)
    done = sum(1 for r in rows if r["review_id"])
    return render(request, "judge_queue.html", event=event, phase=domain.phase(event), rows=rows, done=done)


def _review_page(request: Request, project_id: str, *, form=None, error=None, status_code=200):
    conn = conn_for(request)
    me = domain.require_user(actor(request))
    project = domain.get_project(conn, project_id)
    domain.require_judge(conn, project["event_id"], me)
    assignment = conn.execute("SELECT source FROM assignments WHERE judge_id = ? AND project_id = ?", (me.id, project_id)).fetchone()
    if not assignment:
        raise domain.Forbidden("this project is not assigned to you")
    event = domain.get_event(conn, project["event_id"])
    review = domain.own_review(conn, me, project_id)
    values = form or (review["values"] if review else {})
    comment = (form or {}).get("comment", review["comment"] if review else "")
    return render(request, "review_form.html", status_code=status_code, project=project, event=event,
                  phase=domain.phase(event), criteria=domain.criteria(conn, project["event_id"]), review=review,
                  values=values, comment=comment, error=error, members=domain.team_members(conn, project["team_id"]),
                  imported=assignment["source"] == "import")


@router.get("/projects/{project_id}/review", response_class=HTMLResponse)
def review_form(request: Request, project_id: str):
    return _review_page(request, project_id)


@router.post("/projects/{project_id}/review")
async def save_review(request: Request, project_id: str):
    data = await body(request)
    conn = conn_for(request)
    project = domain.get_project(conn, project_id)
    try:
        domain.save_review(conn, actor(request), project_id, data, str(data.get("comment", "")))
    except (domain.Invalid, domain.Conflict) as exc:
        return _review_page(request, project_id, form=data, error=exc.message, status_code=exc.status)
    return redirect(f"/events/{project['event_id']}/judge", f"Review saved for {project['title']}.")


# --- pairwise ---------------------------------------------------------------------


@router.get("/events/{event_id}/compare", response_class=HTMLResponse)
def compare(request: Request, event_id: str, skip: str = ""):
    conn = conn_for(request)
    skipped = {frozenset(s.split("~")) for s in skip.split(",") if s.count("~") == 1}
    nxt = domain.pairwise_next(conn, actor(request), event_id, skip=skipped)
    return render(request, "compare.html", **nxt, phase=domain.phase(nxt["event"]))


@router.post("/events/{event_id}/compare")
async def record_compare(request: Request, event_id: str):
    data = await body(request)
    domain.record_comparison(conn_for(request), actor(request), event_id, str(data.get("a", "")),
                             str(data.get("b", "")), str(data.get("winner", "")))
    return redirect(f"/events/{event_id}/compare", "Recorded.")
