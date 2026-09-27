"""The participant side: teams, invite links, and the project form."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .. import domain
from ..web import actor, body, conn_for, redirect, render, wants_json

router = APIRouter(include_in_schema=False)  # HTML pages; the JSON API is in api.py


@router.get("/events/{event_id}/team", response_class=HTMLResponse)
def team_page(request: Request, event_id: str):
    conn = conn_for(request)
    me = domain.require_user(actor(request))
    event = domain.get_event(conn, event_id)
    team = domain.team_for(conn, event_id, me.id)
    project = None
    members = []
    if team:
        members = domain.team_members(conn, team["id"])
        project = conn.execute(
            "SELECT * FROM projects WHERE team_id = ? AND duplicate_of IS NULL", (team["id"],)
        ).fetchone()
    return render(request, "team.html", event=event, phase=domain.phase(event), team=team, members=members,
                  project=project,
                  is_judge="judge" in domain.roles(conn, event_id, me))


@router.post("/events/{event_id}/teams")
async def create_team(request: Request, event_id: str):
    data = await body(request)
    domain.create_team(conn_for(request), actor(request), event_id, data.get("name", ""))
    return redirect(f"/events/{event_id}/team", "Team created. Share the invite link with your teammates.")


@router.get("/join/{code}", response_class=HTMLResponse)
def join_page(request: Request, code: str):
    conn = conn_for(request)
    team = domain.team_by_code(conn, code)
    event = domain.get_event(conn, team["event_id"])
    return render(request, "join.html", team=team, event=event, code=code,
                  members=domain.team_members(conn, team["id"]))


@router.post("/join/{code}")
async def join(request: Request, code: str):
    await body(request)
    conn = conn_for(request)
    team_id = domain.join_team(conn, actor(request), code)
    event_id = conn.execute("SELECT event_id FROM teams WHERE id = ?", (team_id,)).fetchone()["event_id"]
    return redirect(f"/events/{event_id}/team", "You joined the team.")


@router.post("/teams/{team_id}/leave")
async def leave(request: Request, team_id: str):
    await body(request)
    conn = conn_for(request)
    event_id = conn.execute("SELECT event_id FROM teams WHERE id = ?", (team_id,)).fetchone()
    domain.leave_team(conn, actor(request), team_id)
    return redirect(f"/events/{event_id['event_id']}" if event_id else "/", "You left the team.")


@router.post("/teams/{team_id}/reset-invite")
async def reset_invite(request: Request, team_id: str):
    await body(request)
    conn = conn_for(request)
    domain.reset_invite(conn, actor(request), team_id)
    event_id = conn.execute("SELECT event_id FROM teams WHERE id = ?", (team_id,)).fetchone()["event_id"]
    return redirect(f"/events/{event_id}/team", "New invite link made. The old one no longer works.")


# --- the project form ---------------------------------------------------------------


def _form_page(request: Request, event_id: str, project=None, form=None, error=None, status_code=200):
    conn = conn_for(request)
    event = domain.get_event(conn, event_id)
    return render(request, "project_form.html", status_code=status_code, event=event, phase=domain.phase(event),
                  tracks=domain.tracks(conn, event_id), project=project, form=form or (dict(project) if project else {}),
                  error=error)


@router.get("/events/{event_id}/projects/new", response_class=HTMLResponse)
def new_project(request: Request, event_id: str):
    conn = conn_for(request)
    me = domain.require_user(actor(request))
    team = domain.team_for(conn, event_id, me.id)
    if team:
        existing = conn.execute("SELECT id FROM projects WHERE team_id = ? AND duplicate_of IS NULL", (team["id"],)).fetchone()
        if existing:
            return redirect(f"/projects/{existing['id']}/edit")
    return _form_page(request, event_id)


@router.post("/events/{event_id}/projects/new")
async def create_project(request: Request, event_id: str):
    """Create or save the team's entry. This is also the route the DOGFOOD
    checker posts to after the deadline, and must refuse."""
    data = await body(request)
    submit = str(data.get("action", "submit" if wants_json(request) else "draft")) == "submit"
    try:
        pid = domain.save_project(conn_for(request), actor(request), event_id, data, submit=submit)
    except domain.DomainError as exc:
        if wants_json(request) or isinstance(exc, domain.Unauthorized):
            raise
        return _form_page(request, event_id, form=data, error=exc.message, status_code=exc.status)
    if wants_json(request):
        return JSONResponse({"id": pid, "status": "submitted" if submit else "draft"}, status_code=201)
    return redirect(f"/projects/{pid}", "Submitted. You can keep editing until the deadline." if submit
                    else "Draft saved. Only your team can see it until you submit.")


@router.get("/projects/{project_id}/edit", response_class=HTMLResponse)
def edit_project(request: Request, project_id: str):
    conn = conn_for(request)
    me = domain.require_user(actor(request))
    project = domain.get_project(conn, project_id)
    if not conn.execute("SELECT 1 FROM team_members WHERE team_id = ? AND user_id = ?", (project["team_id"], me.id)).fetchone():
        raise domain.Forbidden("that is not your team's project")
    return _form_page(request, project["event_id"], project=project)


@router.post("/projects/{project_id}/edit")
async def save_edit(request: Request, project_id: str):
    data = await body(request)
    conn = conn_for(request)
    project = domain.get_project(conn, project_id)
    submit = data.get("action") == "submit" or project["status"] == "submitted"
    try:
        domain.save_project(conn, actor(request), project["event_id"], data, submit=submit, project_id=project_id)
    except domain.DomainError as exc:
        if wants_json(request) or isinstance(exc, domain.Unauthorized):
            raise
        return _form_page(request, project["event_id"], project=project, form=data, error=exc.message,
                          status_code=exc.status)
    return redirect(f"/projects/{project_id}", "Saved." if project["status"] == "submitted" or not submit
                    else "Submitted. You can keep editing until the deadline.")


@router.post("/projects/{project_id}/unsubmit")
async def unsubmit(request: Request, project_id: str):
    await body(request)
    domain.unsubmit_project(conn_for(request), actor(request), project_id)
    return redirect(f"/projects/{project_id}", "Moved back to draft. It is hidden from the gallery until you submit again.")
