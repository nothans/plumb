"""The organizer side: event setup, judges, assignment, rubric, live results,
integrity checks, the audit log, publication, and exports."""

from __future__ import annotations

import hmac
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .. import audit, domain, records, transfer
from ..db import now
from ..web import actor, base_url, conn_for, form_body, redirect, render, signing_base_url, upload_form
from ..webhooks import check_destination

router = APIRouter(include_in_schema=False)  # HTML pages; the JSON API is in api.py


def _event_form_data(data: dict) -> tuple[dict, list[str], list[dict]]:
    tracks = [t for t in str(data.get("tracks", "")).splitlines() if t.strip()]
    prizes = []
    for line in str(data.get("prizes", "")).splitlines():
        if line.strip():
            parts = [p.strip() for p in line.split("|")]
            prizes.append({"name": parts[0], "description": parts[1] if len(parts) > 1 else "",
                           "track_name": parts[2] if len(parts) > 2 else ""})
    return data, tracks, prizes


# --- create and import (admins) -------------------------------------------------


@router.get("/events/new", response_class=HTMLResponse)
def new_event(request: Request):
    me = domain.require_user(actor(request))
    if not me.is_admin:
        raise domain.Forbidden("only admins can create events")
    return render(request, "event_form.html", form={}, event=None)


@router.post("/events/new")
def create_event(request: Request, data: dict = Depends(form_body)):
    form, tracks, prizes = _event_form_data(data)
    try:
        eid = domain.create_event(conn_for(request), actor(request), form, tracks, prizes)
    except (domain.Invalid, domain.Conflict) as exc:
        return render(request, "event_form.html", status_code=exc.status, form=data, event=None, error=exc.message)
    return redirect(f"/events/{eid}/manage", "Event created. You are its first organizer.")


@router.get("/events/import", response_class=HTMLResponse)
def import_form(request: Request):
    me = domain.require_user(actor(request))
    if not me.is_admin:
        raise domain.Forbidden("only admins can import events")
    return render(request, "import.html")


@router.post("/events/import")
def import_event(request: Request, form: dict = Depends(upload_form)):
    me = domain.require_user(actor(request))
    if not me.is_admin:
        raise domain.Forbidden("only admins can import events")
    # The multipart body carries its own CSRF token; check it against the session's.
    expected = request.state.csrf
    if not expected or not hmac.compare_digest(str(form["csrf"] or ""), expected):
        raise domain.Forbidden("this form expired; reload the page and try again")
    raw = form["file"]
    if raw is None:
        return render(request, "import.html", status_code=422, error="choose a JSON file to import")
    if len(raw) > 20 * 1024 * 1024:
        return render(request, "import.html", status_code=413, error="that file is over 20 MB")
    try:
        doc = json.loads(raw.decode("utf-8"))
        result = transfer.import_event(conn_for(request), doc, actor_id=me.id, organizer_ids=[me.id],
                                       event_id=(form.get("event_id") or None))
    except ValueError:
        return render(request, "import.html", status_code=422, error="that file is not valid JSON")
    except domain.DomainError as exc:
        return render(request, "import.html", status_code=exc.status, error=exc.message)
    return redirect(f"/events/{result['event_id']}/manage",
                    f"Imported {result['projects']} projects, {result['judges']} judges and {result['scores']} scores.")


# --- dashboard ---------------------------------------------------------------------


@router.get("/events/{event_id}/manage", response_class=HTMLResponse)
def dashboard(request: Request, event_id: str, all: int = 0, refresh: int = 0):
    conn = conn_for(request)
    me = actor(request)
    prog = domain.progress(conn, me, event_id)
    event = domain.get_event(conn, event_id)
    dups = [d for d in domain.duplicate_candidates(conn, event_id) if not d["confirmed"]]
    res = domain.compute_results(conn, event_id)
    flagged = [j for j in res["fit"].judges if set(j.flags) & {"flat", "harsh", "generous"}]
    return render(request, "organizer/dashboard.html", event=event, phase=domain.phase(event), prog=prog,
                  duplicates=dups, flagged=flagged, names=res["names"], turnout=domain.turnout(conn, event_id),
                  signals=domain.abuse_signals(conn, me, event_id),
                  chain=audit.verify_chain(conn, full=False), show_all=bool(all), refresh=bool(refresh), as_of=now(),
                  tracks={t["id"]: t["name"] for t in domain.tracks(conn, event_id)})


# --- settings --------------------------------------------------------------------------


def _settings_page(request: Request, event_id: str, form=None, error=None, status_code=200):
    conn = conn_for(request)
    domain.require_organizer(conn, event_id, actor(request))
    event = domain.get_event(conn, event_id)
    prizes = domain.prizes(conn, event_id)
    return render(request, "event_form.html", status_code=status_code, event=event,
                  form=form or {**dict(event), "prizes": "\n".join(
                      " | ".join([p["name"], p["description"] or ""] + ([p["track_name"]] if p["track_name"] else []))
                      .rstrip(" |") for p in prizes)},
                  tracks=domain.tracks(conn, event_id), error=error)


@router.post("/events/{event_id}/manage/archive")
def archive(request: Request, event_id: str, data: dict = Depends(form_body)):
    archived = str(data.get("archived", "1")) == "1"
    domain.set_archived(conn_for(request), actor(request), event_id, archived)
    return redirect(f"/events/{event_id}", "Archived: the event has left the home page and the gallery. Its links still work."
                    if archived else "Restored to the home page and the gallery.")


@router.get("/events/{event_id}/manage/settings", response_class=HTMLResponse)
def settings(request: Request, event_id: str):
    return _settings_page(request, event_id)


@router.post("/events/{event_id}/manage/settings")
def save_settings(request: Request, event_id: str, data: dict = Depends(form_body)):
    form, tracks, prizes = _event_form_data(data)
    try:
        domain.update_event(conn_for(request), actor(request), event_id, form, tracks, prizes)
    except (domain.Invalid, domain.Conflict) as exc:
        return _settings_page(request, event_id, form=data, error=exc.message, status_code=exc.status)
    return redirect(f"/events/{event_id}/manage/settings", "Saved. Every change is in the audit log.")


# --- judges --------------------------------------------------------------------------------


def _judges_page(request: Request, event_id: str, *, status_code: int = 200, **extra):
    conn = conn_for(request)
    me = actor(request)
    domain.require_organizer(conn, event_id, me)
    return render(request, "organizer/judges.html", status_code=status_code, event=domain.get_event(conn, event_id),
                  judges=domain.judges(conn, event_id), tracks=domain.tracks(conn, event_id),
                  pending=domain.pending_invitations(conn, event_id),
                  unclaimed=domain.unclaimed_accounts(conn, me, event_id), claim_days=domain.CLAIM_DAYS,
                  **{"invite_link": None, "claim_link": None, **extra})


@router.get("/events/{event_id}/manage/judges", response_class=HTMLResponse)
def judges(request: Request, event_id: str):
    return _judges_page(request, event_id)


@router.post("/events/{event_id}/manage/invite", response_class=HTMLResponse)
def invite(request: Request, event_id: str, data: dict = Depends(form_body)):
    conn = conn_for(request)
    try:
        token = domain.invite(conn, actor(request), event_id, str(data.get("email", "")), str(data.get("role", "judge")))
    except (domain.Invalid, domain.Conflict) as exc:
        return _judges_page(request, event_id, status_code=exc.status, invite_email=data.get("email"), error=exc.message)
    return _judges_page(request, event_id, invite_link=f"{base_url(request)}/invitations/{token}",
                        invite_email=data.get("email"))


@router.post("/events/{event_id}/manage/claim-link", response_class=HTMLResponse)
def claim_link(request: Request, event_id: str, data: dict = Depends(form_body)):
    conn = conn_for(request)
    user_id = str(data.get("user_id", ""))
    try:
        token = domain.issue_claim_link(conn, actor(request), event_id, user_id)
    except (domain.Conflict, domain.Forbidden, domain.NotFound) as exc:
        return _judges_page(request, event_id, status_code=exc.status, error=exc.message)
    user = domain.get_user(conn, user_id)
    return _judges_page(request, event_id, claim_link=f"{base_url(request)}/claim/{token}", claim_email=user["email"])


@router.post("/events/{event_id}/manage/invitations/revoke")
def revoke_invitation(request: Request, event_id: str, data: dict = Depends(form_body)):
    domain.revoke_invitation(conn_for(request), actor(request), event_id, str(data.get("email", "")),
                             str(data.get("role", "")))
    return redirect(f"/events/{event_id}/manage/judges", "Invitation revoked. Its link no longer works.")


@router.post("/events/{event_id}/manage/judges/{judge_id}/remove")
def remove_judge(request: Request, event_id: str, judge_id: str, _: dict = Depends(form_body)):
    domain.remove_judge(conn_for(request), actor(request), event_id, judge_id)
    return redirect(f"/events/{event_id}/manage/judges", "Judge removed, with their open assignments.")


@router.post("/events/{event_id}/manage/judges/{judge_id}/tracks")
def judge_tracks(request: Request, event_id: str, judge_id: str, data: dict = Depends(form_body)):
    chosen = data.get("track", [])
    chosen = chosen if isinstance(chosen, list) else [chosen]
    domain.set_judge_tracks(conn_for(request), actor(request), event_id, judge_id, [t for t in chosen if t])
    return redirect(f"/events/{event_id}/manage/judges", "Tracks updated.")


# --- assignments ------------------------------------------------------------------------------


@router.get("/events/{event_id}/manage/assignments", response_class=HTMLResponse)
def assignments(request: Request, event_id: str):
    conn = conn_for(request)
    me = actor(request)
    prog = domain.progress(conn, me, event_id)
    rows = conn.execute(
        "SELECT a.*, u.name AS judge_name, p.title, p.track_id, v.id AS review_id FROM assignments a "
        "JOIN users u ON u.id = a.judge_id JOIN projects p ON p.id = a.project_id "
        "LEFT JOIN reviews v ON v.judge_id = a.judge_id AND v.project_id = a.project_id "
        "WHERE a.event_id = ? ORDER BY p.id, u.name", (event_id,)
    ).fetchall()
    by_project: dict[str, list] = {}
    for r in rows:
        by_project.setdefault(r["project_id"], []).append(r)
    return render(request, "organizer/assignments.html", event=domain.get_event(conn, event_id), prog=prog,
                  by_project=by_project, judges=prog["judges"],
                  tracks={t["id"]: t["name"] for t in domain.tracks(conn, event_id)})


@router.post("/events/{event_id}/manage/assignments/auto")
def auto(request: Request, event_id: str, _: dict = Depends(form_body)):
    result = domain.auto_assign(conn_for(request), actor(request), event_id)
    note = f"Added {result['added']} assignments."
    if result["short"]:
        note += f" {len(result['short'])} projects are still short of the target; see the list."
    return redirect(f"/events/{event_id}/manage/assignments", note)


@router.post("/events/{event_id}/manage/assignments/release")
def release_assignments(request: Request, event_id: str, data: dict = Depends(form_body)):
    n = domain.release_open_assignments(conn_for(request), actor(request), event_id, str(data.get("judge_id", "")))
    return redirect(f"/events/{event_id}/manage/assignments",
                    f"Released {n} open assignment{'' if n == 1 else 's'}. Assign automatically to give them to other judges.")


@router.post("/events/{event_id}/manage/assignments/add")
def add_assignment(request: Request, event_id: str, data: dict = Depends(form_body)):
    domain.assign_manual(conn_for(request), actor(request), event_id, str(data.get("judge_id", "")),
                         str(data.get("project_id", "")))
    return redirect(f"/events/{event_id}/manage/assignments#{data.get('project_id', '')}", "Judge assigned.")


@router.post("/events/{event_id}/manage/assignments/remove")
def remove_assignment(request: Request, event_id: str, data: dict = Depends(form_body)):
    domain.unassign(conn_for(request), actor(request), event_id, str(data.get("judge_id", "")),
                    str(data.get("project_id", "")))
    return redirect(f"/events/{event_id}/manage/assignments#{data.get('project_id', '')}", "Assignment removed.")


# --- rubric -------------------------------------------------------------------------------------


@router.get("/events/{event_id}/manage/rubric", response_class=HTMLResponse)
def rubric(request: Request, event_id: str):
    conn = conn_for(request)
    domain.require_organizer(conn, event_id, actor(request))
    has_reviews = conn.execute("SELECT 1 FROM reviews WHERE event_id = ?", (event_id,)).fetchone() is not None
    return render(request, "organizer/rubric.html", event=domain.get_event(conn, event_id),
                  criteria=domain.criteria(conn, event_id), has_reviews=has_reviews,
                  history=domain.rubric_history(conn, event_id))


@router.post("/events/{event_id}/manage/rubric/lock")
def lock_rubric(request: Request, event_id: str, _: dict = Depends(form_body)):
    domain.lock_rubric(conn_for(request), actor(request), event_id)
    return redirect(f"/events/{event_id}/manage/rubric", "Rubric locked. The weights can no longer change.")


@router.post("/events/{event_id}/manage/rubric")
def save_rubric(request: Request, event_id: str, data: dict = Depends(form_body)):
    weights = {k[len("weight_"):]: v for k, v in data.items() if k.startswith("weight_")}
    new = {"name": data.get("new_name"), "description": data.get("new_description"), "weight": data.get("new_weight")}
    domain.update_rubric(conn_for(request), actor(request), event_id, weights, new)
    return redirect(f"/events/{event_id}/manage/rubric", "Rubric saved. Live results use the new weights now.")


# --- reviews, results, publication ------------------------------------------------------------------


@router.get("/events/{event_id}/manage/reviews", response_class=HTMLResponse)
def all_reviews(request: Request, event_id: str, judge: str = ""):
    conn = conn_for(request)
    domain.require_organizer(conn, event_id, actor(request))
    rows = domain.reviews_for(conn, event_id=event_id, judge_id=judge or None)
    return render(request, "organizer/reviews.html", event=domain.get_event(conn, event_id), rows=rows,
                  criteria=domain.criteria(conn, event_id), judges=domain.judges(conn, event_id), judge=judge)


@router.get("/events/{event_id}/manage/results", response_class=HTMLResponse)
def live_results(request: Request, event_id: str):
    conn = conn_for(request)
    domain.require_organizer(conn, event_id, actor(request))
    res = domain.compute_results(conn, event_id)
    event = domain.get_event(conn, event_id)
    pw = domain.pairwise_results(conn, actor(request), event_id)
    suggestions = domain.default_awards(conn, event_id, res) if res["fit"].identifiable else []
    open_duplicates = [d for d in domain.duplicate_candidates(conn, event_id) if not d["confirmed"]]
    return render(request, "organizer/results.html", event=event, phase=domain.phase(event), res=res, fit=res["fit"],
                  pw=pw, suggestions=suggestions, leaders=domain.track_leaders(res["fit"], res["projects"]),
                  open_duplicates=open_duplicates, set_aside=domain.reviews_set_aside(conn, event_id),
                  tracks={t["id"]: t["name"] for t in domain.tracks(conn, event_id)})


@router.post("/events/{event_id}/manage/publish")
def publish(request: Request, event_id: str, data: dict = Depends(form_body)):
    if str(data.get("confirm", "")) != "1":
        return redirect(f"/events/{event_id}/manage/results#publish", "Tick the box to confirm; publishing cannot be undone.")
    awards = {k[len("award_"):]: str(v) for k, v in data.items() if k.startswith("award_")}
    reasons = {k[len("reason_"):]: str(v) for k, v in data.items() if k.startswith("reason_")}
    out = records.publish_results(conn_for(request), actor(request), event_id, request.app.state.signer,
                                  signing_base_url(request), awards, reasons)
    return redirect(f"/events/{event_id}/results",
                    f"Results published with {out['judge_records']} judge records and {out['team_records']} team certificates, all signed.")


# --- integrity -------------------------------------------------------------------------------------------


@router.get("/events/{event_id}/manage/integrity", response_class=HTMLResponse)
def integrity(request: Request, event_id: str):
    conn = conn_for(request)
    me = actor(request)
    signals = domain.abuse_signals(conn, me, event_id)
    return render(request, "organizer/integrity.html", event=domain.get_event(conn, event_id),
                  duplicates=domain.duplicate_candidates(conn, event_id), signals=signals,
                  chain=audit.verify_chain(conn), head=audit.head(conn),
                  frozen=domain.get_event(conn, event_id)["results_published_at"] is not None,
                  refused=dict(request.app.state.limiter.refused),
                  live_projects=domain.gallery(conn, event_id=event_id, limit=10000)[0],
                  disqualified=conn.execute(
                      "SELECT id, title, disqualified_reason FROM projects WHERE event_id = ? AND disqualified_reason IS NOT NULL",
                      (event_id,)).fetchall())


@router.post("/events/{event_id}/manage/duplicates")
def resolve_duplicate(request: Request, event_id: str, data: dict = Depends(form_body)):
    domain.resolve_duplicate(conn_for(request), actor(request), str(data.get("keep", "")), str(data.get("drop", "")))
    return redirect(f"/events/{event_id}/manage/integrity", "Marked as a duplicate. It has left the gallery and the ranking.")


@router.post("/projects/{project_id}/disqualify")
def disqualify(request: Request, project_id: str, data: dict = Depends(form_body)):
    conn = conn_for(request)
    project = domain.get_project(conn, project_id)
    reason = str(data.get("reason", "")) if data.get("action") != "restore" else None
    if data.get("action") != "restore" and not (reason or "").strip():
        raise domain.Invalid("give a reason; it goes in the audit log")
    domain.set_disqualified(conn, actor(request), project_id, reason)
    return redirect(f"/events/{project['event_id']}/manage/integrity", "Updated.")


@router.post("/events/{event_id}/manage/disqualify")
def disqualify_by_id(request: Request, event_id: str, data: dict = Depends(form_body)):
    conn = conn_for(request)
    project = domain.get_project(conn, str(data.get("project_id", "")).strip())
    if project["event_id"] != event_id:
        raise domain.NotFound("no such project in this event")
    if not str(data.get("reason", "")).strip():
        raise domain.Invalid("give a reason; it goes in the audit log")
    domain.set_disqualified(conn, actor(request), project["id"], str(data["reason"]))
    return redirect(f"/events/{event_id}/manage/integrity", f"{project['title']} is disqualified.")


@router.post("/events/{event_id}/manage/void-votes")
def void_votes(request: Request, event_id: str, data: dict = Depends(form_body)):
    n = domain.remove_votes_of(conn_for(request), actor(request), event_id, str(data.get("voter_id", "")),
                               str(data.get("reason", "")))
    return redirect(f"/events/{event_id}/manage/integrity", f"Removed {n} votes.")


@router.get("/events/{event_id}/manage/audit", response_class=HTMLResponse)
def audit_log(request: Request, event_id: str, before: int | None = None, action: str = ""):
    conn = conn_for(request)
    rows = domain.audit_entries(conn, actor(request), event_id, before=before, action=action or None)
    entries = [{**dict(r), "detail_obj": json.loads(r["detail"])} for r in rows]
    return render(request, "organizer/audit.html", event=domain.get_event(conn, event_id), entries=entries,
                  action=action, chain=audit.verify_chain(conn, full=False))


# --- integrations ------------------------------------------------------------------------


@router.get("/events/{event_id}/manage/integrations", response_class=HTMLResponse)
def integrations(request: Request, event_id: str):
    conn = conn_for(request)
    return render(request, "organizer/integrations.html", event=domain.get_event(conn, event_id),
                  hooks=domain.webhooks_for(conn, actor(request), event_id), new_secret=None)


@router.post("/events/{event_id}/manage/integrations/webhooks", response_class=HTMLResponse)
def add_webhook(request: Request, event_id: str, data: dict = Depends(form_body)):
    conn = conn_for(request)
    try:
        hook = domain.create_webhook(conn, actor(request), event_id, str(data.get("url", "")), check_destination)
    except domain.Invalid as exc:
        return render(request, "organizer/integrations.html", status_code=422, event=domain.get_event(conn, event_id),
                      hooks=domain.webhooks_for(conn, actor(request), event_id), new_secret=None, error=exc.message)
    return render(request, "organizer/integrations.html", event=domain.get_event(conn, event_id),
                  hooks=domain.webhooks_for(conn, actor(request), event_id), new_secret=hook["secret"])


@router.post("/events/{event_id}/manage/integrations/webhooks/{hook_id}/delete")
def remove_webhook(request: Request, event_id: str, hook_id: str, _: dict = Depends(form_body)):
    domain.delete_webhook(conn_for(request), actor(request), event_id, hook_id)
    return redirect(f"/events/{event_id}/manage/integrations", "Webhook removed.")


# --- exports --------------------------------------------------------------------------------------------------


def _csv(text: str, filename: str) -> Response:
    return Response(text, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/events/{event_id}/export/scores.csv")
def export_scores(request: Request, event_id: str):
    return _csv(domain.scores_csv(conn_for(request), actor(request), event_id), f"{event_id}-scores.csv")


@router.get("/events/{event_id}/export/results.csv")
def export_results(request: Request, event_id: str):
    return _csv(domain.results_csv(conn_for(request), actor(request), event_id), f"{event_id}-results.csv")


@router.get("/events/{event_id}/export/event.json")
def export_event(request: Request, event_id: str):
    doc = transfer.export_event(conn_for(request), actor(request), event_id)
    return JSONResponse(doc, headers={"Content-Disposition": f'attachment; filename="{event_id}.json"'})
