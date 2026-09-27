"""What anyone can see: events, the gallery, projects, published results,
signed records, and the embeddable widget. Plus comments and votes, which
need an account but live on public pages."""

from __future__ import annotations

import json
import math

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .. import domain, ratelimit, records
from ..web import actor, body, conn_for, redirect, render

router = APIRouter(include_in_schema=False)  # HTML pages; the JSON API is in api.py
PAGE_SIZE = 48


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    conn = conn_for(request)
    me = actor(request)
    events = []
    for e in domain.list_events(conn):
        count = conn.execute(
            "SELECT COUNT(*) FROM projects WHERE event_id = ? AND status = 'submitted' AND duplicate_of IS NULL", (e["id"],)
        ).fetchone()[0]
        events.append({"event": e, "phase": domain.phase(e), "projects": count, "roles": domain.roles(conn, e["id"], me)})
    return render(request, "home.html", events=events)


@router.get("/events/{event_id}", response_class=HTMLResponse)
def event_page(request: Request, event_id: str):
    conn = conn_for(request)
    me = actor(request)
    event = domain.get_event(conn, event_id)
    my_team = domain.team_for(conn, event_id, me.id) if me else None
    my_project = None
    if my_team:
        my_project = conn.execute(
            "SELECT * FROM projects WHERE team_id = ? AND duplicate_of IS NULL", (my_team["id"],)
        ).fetchone()
    count = conn.execute(
        "SELECT COUNT(*) FROM projects WHERE event_id = ? AND status = 'submitted' AND duplicate_of IS NULL", (event_id,)
    ).fetchone()[0]
    return render(
        request, "event.html", event=event, phase=domain.phase(event), tracks=domain.tracks(conn, event_id),
        prizes=domain.prizes(conn, event_id), roles=domain.roles(conn, event_id, me), my_team=my_team,
        my_project=my_project, project_count=count, criteria=domain.criteria(conn, event_id),
        turnout=domain.turnout(conn, event_id),
    )


@router.get("/projects", response_class=HTMLResponse)
def gallery(request: Request, q: str = "", event: str = "", track: str = "", page: int = 1):
    conn = conn_for(request)
    page = max(page, 1)
    rows, total = domain.gallery(conn, event_id=event or None, track_id=track or None, q=q or None,
                                 limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
    events = domain.list_events(conn)
    track_list = domain.tracks(conn, event) if event else []
    return render(request, "gallery.html", projects=rows, total=total, q=q, event_id=event, track_id=track,
                  events=events, tracks=track_list, page=page, pages=max(1, math.ceil(total / PAGE_SIZE)))


@router.get("/projects/{project_id}", response_class=HTMLResponse)
def project_page(request: Request, project_id: str):
    conn = conn_for(request)
    me = actor(request)
    project = domain.view_project(conn, me, project_id)
    event = domain.get_event(conn, project["event_id"])
    members = domain.team_members(conn, project["team_id"])
    ph = domain.phase(event)
    voted = False
    can_vote = False
    if me and ph["voting"] == "open":
        voted = conn.execute("SELECT 1 FROM votes WHERE voter_id = ? AND project_id = ?", (me.id, project_id)).fetchone() is not None
        can_vote = not any(m["id"] == me.id for m in members)
    return render(
        request, "project.html", project=project, event=event, phase=ph, members=members,
        comments=domain.comments(conn, me, project_id), is_member=bool(me and any(m["id"] == me.id for m in members)),
        is_organizer=domain.is_organizer(conn, event["id"], me), voted=voted, can_vote=can_vote,
    )


@router.post("/projects/{project_id}/comments")
async def post_comment(request: Request, project_id: str):
    data = await body(request)
    me = actor(request)
    ratelimit.check(request.app.state.limiter, "comment", me.id if me else None)
    domain.add_comment(conn_for(request), me, project_id, data.get("body", ""))
    return redirect(f"/projects/{project_id}#comments", "Comment posted.")


@router.post("/comments/{comment_id}/hide")
async def hide_comment(request: Request, comment_id: str):
    data = await body(request)
    conn = conn_for(request)
    domain.hide_comment(conn, actor(request), comment_id, data.get("reason", ""))
    project_id = conn.execute("SELECT project_id FROM comments WHERE id = ?", (comment_id,)).fetchone()["project_id"]
    return redirect(f"/projects/{project_id}#comments", "Comment hidden. The audit log has the reason.")


# --- voting ---------------------------------------------------------------------


@router.get("/events/{event_id}/vote", response_class=HTMLResponse)
def ballot(request: Request, event_id: str):
    b = domain.ballot(conn_for(request), actor(request), event_id)
    return render(request, "ballot.html", **b)


@router.post("/projects/{project_id}/vote")
async def vote(request: Request, project_id: str):
    data = await body(request)
    me = actor(request)
    ratelimit.check(request.app.state.limiter, "vote", me.id if me else None)
    conn = conn_for(request)
    if data.get("action") == "withdraw":
        domain.withdraw_vote(conn, me, project_id)
        notice = "Vote withdrawn."
    else:
        domain.cast_vote(conn, me, project_id)
        notice = "Vote counted."
    back = data.get("back") or f"/projects/{project_id}"
    return redirect(back if back.startswith("/") and not back.startswith("//") else "/", notice)


@router.get("/events/{event_id}/votes", response_class=HTMLResponse)
def vote_results(request: Request, event_id: str):
    conn = conn_for(request)
    event = domain.get_event(conn, event_id)
    return render(request, "votes.html", event=event, tallies=domain.vote_tallies(conn, actor(request), event_id),
                  turnout=domain.turnout(conn, event_id))


# --- results and records -----------------------------------------------------------


@router.get("/events/{event_id}/results", response_class=HTMLResponse)
def results(request: Request, event_id: str):
    conn = conn_for(request)
    event = domain.get_event(conn, event_id)
    if not event["results_published_at"]:
        return render(request, "results_pending.html", event=event,
                      is_organizer=domain.is_organizer(conn, event_id, actor(request)))
    res = records.published_results(conn, event_id)
    return render(request, "results.html", event=event, res=res, fit=res["fit"], results_record=res["record"],
                  tracks={t["id"]: t["name"] for t in domain.tracks(conn, event_id)})


@router.get("/events/{event_id}/records", response_class=HTMLResponse)
def event_records(request: Request, event_id: str):
    conn = conn_for(request)
    event = domain.get_event(conn, event_id)
    rows = [{"row": r, "body": json.loads(r["payload"])} for r in records.records_for(conn, event_id)]
    return render(request, "records.html", event=event, records=rows)


@router.get("/records/{record_id}.json")
def record_json(request: Request, record_id: str):
    row = records.get_record(conn_for(request), record_id)
    return JSONResponse(records.envelope(row, request.app.state.signer))


@router.get("/records/{record_id}", response_class=HTMLResponse)
def record_page(request: Request, record_id: str):
    conn = conn_for(request)
    row = records.get_record(conn, record_id)
    signer = request.app.state.signer
    env = records.envelope(row, signer)
    check = records.check_envelope(env, signer)
    body_ = json.loads(row["payload"])
    template = "certificate.html" if row["kind"] == "team" else "record.html"
    return render(request, template, record=row, body=body_, envelope=json.dumps(env, indent=2), check=check,
                  event=domain.get_event(conn, row["event_id"]))


@router.get("/verify", response_class=HTMLResponse)
def verify_form(request: Request):
    return render(request, "verify.html", result=None, pasted="")


@router.post("/verify", response_class=HTMLResponse)
async def verify_post(request: Request):
    data = await body(request)
    pasted = str(data.get("envelope", ""))
    try:
        env = json.loads(pasted)
        result = records.check_envelope(env if isinstance(env, dict) else {}, request.app.state.signer)
    except ValueError:
        result = {"ok": False, "reason": "that is not JSON; paste the whole envelope from a record's JSON link"}
    return render(request, "verify.html", result=result, pasted=pasted)


@router.get("/.well-known/plumb-key.json")
def public_key(request: Request):
    signer = request.app.state.signer
    return JSONResponse({"keys": [signer.public_jwk()], "algorithm": "Ed25519",
                         "canonicalization": "JSON, sorted keys, separators (',', ':'), UTF-8"})


# --- embeddable widget -------------------------------------------------------------------


@router.get("/embed/events/{event_id}", response_class=HTMLResponse)
def embed(request: Request, event_id: str, track: str = "", limit: int = 12):
    conn = conn_for(request)
    event = domain.get_event(conn, event_id)
    rows, total = domain.gallery(conn, event_id=event_id, track_id=track or None, limit=max(1, min(limit, 48)))
    return render(request, "embed.html", event=event, projects=rows, total=total)
