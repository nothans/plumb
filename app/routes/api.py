"""The JSON API. Every action in the UI has an endpoint here, and every
endpoint calls the same domain function the UI does.

Authenticate with a bearer token (make one at /me) or the session cookie.
The machine-readable spec is served at /api/openapi.json and described at
/api.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request, Security
from fastapi.responses import HTMLResponse, Response
from fastapi.security import HTTPBearer
from pydantic import BaseModel, Field

from .. import audit, domain, ratelimit, records, transfer
from ..web import actor, base_url, conn_for, render, same_origin, signing_base_url
from ..webhooks import check_destination

router = APIRouter(dependencies=[Security(HTTPBearer(auto_error=False))])


bearer = HTTPBearer(auto_error=False, description="An API token from /me. The session cookie works too.")


def json_guard(request: Request, _token=Security(bearer)) -> None:
    """Cookie-authenticated JSON writes must come from this origin."""
    actor(request)
    if request.method in ("GET", "HEAD", "OPTIONS") or request.state.auth_via != "session":
        return
    if not same_origin(request):
        raise domain.Forbidden("cross-origin request refused")


class ErrorOut(BaseModel):
    error: str = Field(..., description="What went wrong, in plain words")


ERRORS = {
    401: {"model": ErrorOut, "description": "Not logged in: send a bearer token or a session cookie"},
    403: {"model": ErrorOut, "description": "Logged in, but this role may not do that"},
    404: {"model": ErrorOut, "description": "No such thing, or not visible to you"},
    409: {"model": ErrorOut, "description": "Not now: a deadline, a lock, or a conflicting state"},
    422: {"model": ErrorOut, "description": "Invalid input"},
    429: {"model": ErrorOut, "description": "Too many attempts; wait and retry"},
}


class ProjectOut(BaseModel):
    id: str
    event_id: str
    team_id: str
    team_name: str
    track_id: str | None
    track_name: str | None
    title: str
    summary: str
    description: str
    repo_url: str
    demo_url: str
    status: str
    submitted_at: str | None
    updated_at: str
    duplicate_of: str | None
    disqualified_reason: str | None


class GalleryOut(BaseModel):
    total: int
    projects: list[ProjectOut]


class RankedOut(BaseModel):
    rank: int
    project_id: str
    title: str
    reviews: int
    raw_mean: float
    raw_rank: int
    adjusted: float
    interval90: list[float]
    p_beats_next: float | None


class ResultsOut(BaseModel):
    method: dict
    ranking: list[RankedOut]
    judges: list[dict] | None
    excluded_reviews: list[dict]
    unreviewed_projects: list[str]


class VerifyOut(BaseModel):
    ok: bool
    reason: str
    body: dict | None = None


class PublishOut(BaseModel):
    results_record: str
    judge_records: int
    team_records: int


v1 = APIRouter(prefix="/api/v1", dependencies=[Depends(json_guard)], tags=["v1"], responses=ERRORS)


def _row(r: sqlite3.Row | None) -> dict | None:
    return dict(r) if r is not None else None


def _project(r: sqlite3.Row) -> dict:
    keep = ("id", "event_id", "team_id", "team_name", "track_id", "track_name", "title", "summary", "description",
            "repo_url", "demo_url", "status", "submitted_at", "updated_at", "duplicate_of", "disqualified_reason")
    return {k: r[k] for k in keep if k in r.keys()}


def _event(conn: sqlite3.Connection, e: sqlite3.Row) -> dict:
    return {**dict(e), "phase": domain.phase(e),
            "tracks": [dict(t) for t in domain.tracks(conn, e["id"])],
            "prizes": [dict(p) for p in domain.prizes(conn, e["id"])],
            "criteria": domain.criteria(conn, e["id"])}


# --- request bodies ------------------------------------------------------------------


class EventIn(BaseModel):
    name: str | None = None
    tagline: str | None = None
    description: str | None = None
    submissions_open: str | None = Field(None, description="ISO 8601, UTC")
    submissions_close: str | None = None
    judging_close: str | None = None
    voting_mode: Literal["off", "accounts", "participants"] | None = None
    voting_open: str | None = None
    voting_close: str | None = None
    votes_per_voter: int | None = None
    reviews_per_project: int | None = None
    max_team_size: int | None = None
    pairwise: bool | None = None
    tracks: list[str] = []
    prizes: list[dict[str, Any]] | None = None


class TeamIn(BaseModel):
    name: str


class JoinIn(BaseModel):
    code: str


class ProjectIn(BaseModel):
    title: str
    summary: str = ""
    description: str = ""
    repo_url: str = ""
    demo_url: str = ""
    track_id: str | None = None
    submit: bool = False


class ReviewIn(BaseModel):
    values: dict[str, int] = Field(..., description="criterion key -> score")
    comment: str = ""


class InviteIn(BaseModel):
    email: str
    role: Literal["judge", "organizer"] = "judge"


class AssignmentIn(BaseModel):
    judge_id: str
    project_id: str


class RubricIn(BaseModel):
    weights: dict[str, float] = Field(..., description="criterion key -> weight")


class CommentIn(BaseModel):
    body: str


class HideIn(BaseModel):
    reason: str = ""


class DuplicateIn(BaseModel):
    keep: str
    drop: str


class DisqualifyIn(BaseModel):
    reason: str | None = Field(None, description="null to reinstate")


class VoidVotesIn(BaseModel):
    voter_id: str
    reason: str


class JudgeTracksIn(BaseModel):
    track_ids: list[str]


class ComparisonIn(BaseModel):
    a: str
    b: str
    winner: str = Field(..., description="a or b's project id")


class TokenIn(BaseModel):
    label: str


class WebhookIn(BaseModel):
    url: str


class EnvelopeIn(BaseModel):
    payload: str
    signature: str
    key_id: str | None = None


# --- the checker's judge-scores route ----------------------------------------------------


@router.get("/api/judge/scores", tags=["judging"], responses=ERRORS)
def judge_scores(request: Request, judge: str | None = None, event: str | None = None):
    """A judge's own reviews. Asking for another judge's (?judge=ID) needs
    organizer rights on that judge's events; a judge gets 403."""
    rows = domain.judge_scores(conn_for(request), actor(request), judge_id=judge, event_id=event)
    return {"judge": judge or actor(request).id, "reviews": [
        {"id": r["id"], "event_id": r["event_id"], "project_id": r["project_id"], "project_title": r["project_title"],
         "values": r["values"], "comment": r["comment"], "submitted_at": r["submitted_at"], "updated_at": r["updated_at"]}
        for r in rows]}


# --- events -------------------------------------------------------------------------------


@v1.get("/events")
def list_events(request: Request):
    conn = conn_for(request)
    return [_event(conn, e) for e in domain.list_events(conn)]


@v1.post("/events", status_code=201)
def create_event(request: Request, data: EventIn):
    conn = conn_for(request)
    eid = domain.create_event(conn, actor(request), data.model_dump(exclude={"tracks", "prizes"}), data.tracks,
                              data.prizes or [])
    return _event(conn, domain.get_event(conn, eid))


@v1.get("/events/{event_id}")
def get_event(request: Request, event_id: str):
    conn = conn_for(request)
    return {**_event(conn, domain.get_event(conn, event_id)),
            "your_roles": sorted(domain.roles(conn, event_id, actor(request)))}


@v1.patch("/events/{event_id}")
def update_event(request: Request, event_id: str, data: EventIn):
    conn = conn_for(request)
    domain.update_event(conn, actor(request), event_id, data.model_dump(exclude={"tracks", "prizes"}, exclude_none=True),
                        data.tracks, data.prizes)
    return _event(conn, domain.get_event(conn, event_id))


@v1.post("/events/import", status_code=201)
async def import_event(request: Request):
    """Body: a fixture-shaped event document (the format GET .../export returns)."""
    me = domain.require_user(actor(request))
    if not me.is_admin:
        raise domain.Forbidden("only admins can import events")
    try:
        doc = await request.json()
    except ValueError as exc:
        raise domain.Invalid("the body is not JSON") from exc
    return transfer.import_event(conn_for(request), doc, actor_id=me.id, organizer_ids=[me.id])


@v1.get("/events/{event_id}/export")
def export_event(request: Request, event_id: str):
    return transfer.export_event(conn_for(request), actor(request), event_id)


# --- account ----------------------------------------------------------------------------


@v1.get("/me")
def me(request: Request):
    """The calling account and its roles in every event."""
    conn = conn_for(request)
    a = domain.require_user(actor(request))
    return {"id": a.id, "email": a.email, "name": a.name, "is_admin": a.is_admin,
            "events": {e["id"]: sorted(domain.roles(conn, e["id"], a)) for e in domain.list_events(conn)
                       if domain.roles(conn, e["id"], a) - {"admin"}}}


@v1.get("/me/tokens")
def list_tokens(request: Request):
    """The caller's API tokens (never the tokens themselves)."""
    return domain.api_tokens(conn_for(request), actor(request))


@v1.post("/me/tokens", status_code=201)
def create_token(request: Request, data: TokenIn):
    """Create an API token for the caller. The token is returned once."""
    return {"token": domain.create_api_token(conn_for(request), actor(request), data.label)}


@v1.delete("/me/tokens/{hint}", status_code=204)
def revoke_token(request: Request, hint: str):
    """Revoke one of the caller's tokens by the first 8 characters of its hint."""
    domain.revoke_api_token(conn_for(request), actor(request), hint)
    return Response(status_code=204)


# --- teams and projects ------------------------------------------------------------------------


@v1.post("/events/{event_id}/teams", status_code=201)
def create_team(request: Request, event_id: str, data: TeamIn):
    conn = conn_for(request)
    tid = domain.create_team(conn, actor(request), event_id, data.name)
    return {**dict(conn.execute("SELECT * FROM teams WHERE id = ?", (tid,)).fetchone()),
            "members": [dict(m) for m in domain.team_members(conn, tid)]}


@v1.post("/teams/join")
def join_team(request: Request, data: JoinIn):
    conn = conn_for(request)
    tid = domain.join_team(conn, actor(request), data.code)
    return {"team_id": tid, "members": [dict(m) for m in domain.team_members(conn, tid)]}


@v1.get("/events/{event_id}/team")
def my_team(request: Request, event_id: str):
    conn = conn_for(request)
    me = domain.require_user(actor(request))
    team = domain.team_for(conn, event_id, me.id)
    if team is None:
        raise domain.NotFound("you are not on a team in this event")
    return {**dict(team), "members": [dict(m) for m in domain.team_members(conn, team["id"])]}


@v1.post("/teams/{team_id}/invite-link")
def reset_invite(request: Request, team_id: str):
    """Replace the team's invite link; the old one stops working."""
    conn = conn_for(request)
    domain.reset_invite(conn, actor(request), team_id)
    return {"invite_code": conn.execute("SELECT invite_code FROM teams WHERE id = ?", (team_id,)).fetchone()[0]}


@v1.post("/teams/{team_id}/leave", status_code=204)
def leave_team(request: Request, team_id: str):
    domain.leave_team(conn_for(request), actor(request), team_id)
    return Response(status_code=204)


@v1.put("/events/{event_id}/project")
def save_project(request: Request, event_id: str, data: ProjectIn):
    """Create or update your team's entry. submit=true makes it public."""
    conn = conn_for(request)
    pid = domain.save_project(conn, actor(request), event_id, data.model_dump(exclude={"submit"}), submit=data.submit)
    return _project(domain.get_project(conn, pid))


@v1.post("/projects/{project_id}/unsubmit")
def unsubmit(request: Request, project_id: str):
    conn = conn_for(request)
    domain.unsubmit_project(conn, actor(request), project_id)
    return _project(domain.get_project(conn, project_id))


@v1.get("/projects", response_model=GalleryOut)
def gallery(request: Request, event: str | None = None, track: str | None = None, q: str | None = None,
            limit: int = 50, offset: int = 0):
    rows, total = domain.gallery(conn_for(request), event_id=event, track_id=track, q=q,
                                 limit=max(1, min(limit, 200)), offset=max(offset, 0))
    return {"total": total, "projects": [_project(r) for r in rows]}


@v1.get("/projects/{project_id}", response_model=ProjectOut)
def get_project(request: Request, project_id: str):
    return _project(domain.view_project(conn_for(request), actor(request), project_id))


# --- comments and votes ------------------------------------------------------------------------------


@v1.get("/projects/{project_id}/comments")
def list_comments(request: Request, project_id: str):
    conn = conn_for(request)
    me = actor(request)
    domain.view_project(conn, me, project_id)
    return [{k: c[k] for k in ("id", "author", "body", "created_at", "hidden_at", "hidden_reason")}
            for c in domain.comments(conn, me, project_id)]


@v1.post("/projects/{project_id}/comments", status_code=201)
def add_comment(request: Request, project_id: str, data: CommentIn):
    me = actor(request)
    ratelimit.check(request.app.state.limiter, "comment", me.id if me else None)
    return {"id": domain.add_comment(conn_for(request), me, project_id, data.body)}


@v1.post("/comments/{comment_id}/hide", status_code=204)
def hide_comment(request: Request, comment_id: str, data: HideIn):
    domain.hide_comment(conn_for(request), actor(request), comment_id, data.reason)
    return Response(status_code=204)


@v1.get("/events/{event_id}/ballot")
def ballot(request: Request, event_id: str):
    b = domain.ballot(conn_for(request), actor(request), event_id)
    return {"remaining": b["remaining"], "votes": sorted(b["votes"]), "projects": [_project(p) for p in b["projects"]]}


@v1.post("/projects/{project_id}/vote", status_code=204)
def vote(request: Request, project_id: str):
    me = actor(request)
    ratelimit.check(request.app.state.limiter, "vote", me.id if me else None)
    domain.cast_vote(conn_for(request), me, project_id)
    return Response(status_code=204)


@v1.delete("/projects/{project_id}/vote", status_code=204)
def unvote(request: Request, project_id: str):
    me = actor(request)
    ratelimit.check(request.app.state.limiter, "vote", me.id if me else None)
    domain.withdraw_vote(conn_for(request), me, project_id)
    return Response(status_code=204)


@v1.get("/events/{event_id}/votes/reveal")
def vote_reveal(request: Request, event_id: str):
    """After voting closes: every counted vote as seal, project and nonce (no voters), to check against the audit log."""
    return domain.vote_reveal(conn_for(request), actor(request), event_id)


@v1.get("/events/{event_id}/votes")
def tallies(request: Request, event_id: str):
    conn = conn_for(request)
    return {"turnout": domain.turnout(conn, event_id),
            "tallies": [{"project_id": r["id"], "title": r["title"], "votes": r["votes"]}
                        for r in domain.vote_tallies(conn, actor(request), event_id)]}


# --- judging -----------------------------------------------------------------------------------------------


@v1.post("/events/{event_id}/invitations", status_code=201)
def invite(request: Request, event_id: str, data: InviteIn):
    token = domain.invite(conn_for(request), actor(request), event_id, data.email, data.role)
    return {"email": data.email, "role": data.role,
            "link": f"{base_url(request)}/invitations/{token}"}


@v1.post("/invitations/{token}/accept")
def accept(request: Request, token: str):
    return {"event_id": domain.accept_invitation(conn_for(request), actor(request), token)}


@v1.get("/events/{event_id}/judges")
def judges(request: Request, event_id: str):
    conn = conn_for(request)
    domain.require_organizer(conn, event_id, actor(request))
    return domain.judges(conn, event_id)


@v1.put("/events/{event_id}/judges/{judge_id}/tracks")
def judge_tracks(request: Request, event_id: str, judge_id: str, data: JudgeTracksIn):
    domain.set_judge_tracks(conn_for(request), actor(request), event_id, judge_id, data.track_ids)
    return {"judge_id": judge_id, "track_ids": sorted(set(data.track_ids))}


@v1.post("/events/{event_id}/assignments/auto")
def auto_assign(request: Request, event_id: str):
    return domain.auto_assign(conn_for(request), actor(request), event_id)


@v1.post("/events/{event_id}/assignments", status_code=201)
def add_assignment(request: Request, event_id: str, data: AssignmentIn):
    domain.assign_manual(conn_for(request), actor(request), event_id, data.judge_id, data.project_id)
    return data.model_dump()


@v1.delete("/events/{event_id}/assignments", status_code=204)
def remove_assignment(request: Request, event_id: str, data: AssignmentIn):
    domain.unassign(conn_for(request), actor(request), event_id, data.judge_id, data.project_id)
    return Response(status_code=204)


@v1.get("/events/{event_id}/queue")
def judge_queue(request: Request, event_id: str):
    """The calling judge's assigned projects, unreviewed first."""
    return [{**_project(r), "review_id": r["review_id"], "reviewed_at": r["reviewed_at"]}
            for r in domain.judge_queue(conn_for(request), actor(request), event_id)]


@v1.put("/projects/{project_id}/review")
def save_review(request: Request, project_id: str, data: ReviewIn):
    conn = conn_for(request)
    me = actor(request)
    rid = domain.save_review(conn, me, project_id, data.values, data.comment)
    return {"id": rid, **(domain.own_review(conn, me, project_id) or {})}


@v1.get("/events/{event_id}/compare/next")
def compare_next(request: Request, event_id: str):
    """The pair this judge should compare next (least certain order), or null when done."""
    nxt = domain.pairwise_next(conn_for(request), actor(request), event_id)
    return {"pair": [_project(p) for p in nxt["pair"]] if nxt["pair"] else None,
            "done": nxt["done"], "possible": nxt["possible"]}


@v1.post("/events/{event_id}/comparisons", status_code=204)
def compare(request: Request, event_id: str, data: ComparisonIn):
    domain.record_comparison(conn_for(request), actor(request), event_id, data.a, data.b, data.winner)
    return Response(status_code=204)


@v1.get("/events/{event_id}/pairwise")
def pairwise_results(request: Request, event_id: str):
    """Bradley-Terry strengths from direct comparisons and from preferences implied by scores."""
    r = domain.pairwise_results(conn_for(request), actor(request), event_id)

    def dump(bt):
        return None if bt is None else [
            {"rank": s.rank, "project_id": s.project, "strength": s.strength, "sd": s.sd,
             "comparisons": s.comparisons, "wins": s.wins} for s in bt.strengths]

    return {"comparisons": r["n_direct"], "agreement_with_normalized": {"direct": r["agree_direct"],
            "implied": r["agree_implied"]}, "direct": dump(r["direct"]), "implied": dump(r["implied"])}


@v1.get("/events/{event_id}/progress")
def progress(request: Request, event_id: str):
    p = domain.progress(conn_for(request), actor(request), event_id)
    return {**{k: v for k, v in p.items() if k not in ("projects", "judges", "idle_judges")},
            "projects": [dict(r) for r in p["projects"]], "judges": p["judges"],
            "idle_judges": [j["id"] for j in p["idle_judges"]]}


@v1.put("/events/{event_id}/rubric")
def rubric(request: Request, event_id: str, data: RubricIn):
    conn = conn_for(request)
    by_key = {c["key"]: c["id"] for c in domain.criteria(conn, event_id)}
    unknown = set(data.weights) - set(by_key)
    if unknown:
        raise domain.Invalid(f"unknown criteria: {', '.join(sorted(unknown))}")
    domain.update_rubric(conn, actor(request), event_id, {by_key[k]: w for k, w in data.weights.items()})
    return domain.criteria(conn, event_id)


# --- results, integrity, records --------------------------------------------------------------------------


@v1.post("/events/{event_id}/rubric/lock", status_code=204)
def lock_rubric(request: Request, event_id: str):
    """Freeze the rubric for good (organizers). The signed results say whether and when it was locked."""
    domain.lock_rubric(conn_for(request), actor(request), event_id)
    return Response(status_code=204)


@v1.get("/events/{event_id}/results", response_model=ResultsOut)
def results(request: Request, event_id: str):
    res = domain.results_for_viewer(conn_for(request), actor(request), event_id)
    fit = res["fit"]
    return {
        "method": {"judge_sd": fit.judge_sd, "noise_sd": fit.noise_sd, "spread_sd": fit.spread_sd,
                   "observations": fit.n_observations, "graph_components": fit.components},
        "ranking": [{"rank": p.rank, "project_id": p.project, "title": res["projects"][p.project]["title"],
                     "reviews": p.n_reviews, "raw_mean": p.raw_mean, "raw_rank": p.raw_rank, "adjusted": p.adjusted,
                     "interval90": [p.low, p.high], "p_beats_next": p.p_above_next} for p in fit.projects],
        "judges": [{"judge_id": j.judge, "reviews": j.n_reviews, "leniency": j.leniency, "sd": j.sd, "flags": j.flags}
                   for j in fit.judges] if domain.is_organizer(conn_for(request), event_id, actor(request)) else None,
        "excluded_reviews": res["excluded"],
        "unreviewed_projects": [p["id"] for p in res["unreviewed"]],
    }


class PublishIn(BaseModel):
    awards: dict[str, str] | None = Field(None, description="prize id -> project id; omitted prizes get the suggested "
                                                            "winner, an empty string leaves a prize unawarded")


@v1.post("/events/{event_id}/publish", response_model=PublishOut)
def publish(request: Request, event_id: str, data: PublishIn | None = None):
    """Sign and freeze the results, award prizes, issue judge records and team certificates. Final."""
    return records.publish_results(conn_for(request), actor(request), event_id, request.app.state.signer,
                                   signing_base_url(request), (data.awards if data else None))


@v1.get("/events/{event_id}/awards/suggested")
def suggested_awards(request: Request, event_id: str):
    """The winner Plumb would pick for each prize, with how sure the model is."""
    conn = conn_for(request)
    res = domain.results_for_viewer(conn, actor(request), event_id)
    domain.require_organizer(conn, event_id, actor(request))
    return [{"prize_id": s["prize"]["id"], "prize": s["prize"]["name"], "project_id": s["project"],
             "runner_up": s["runner_up"], "p_beats_runner_up": s["p"]}
            for s in domain.default_awards(conn, event_id, res["fit"], res["projects"])]


@v1.get("/events/{event_id}/records")
def list_records(request: Request, event_id: str):
    """Every signed record of the event, as verifiable envelopes."""
    domain.get_event(conn_for(request), event_id)
    return [records.envelope(r, request.app.state.signer) | {"id": r["id"], "kind": r["kind"]}
            for r in records.records_for(conn_for(request), event_id)]


@v1.post("/verify", response_model=VerifyOut)
def verify(request: Request, data: EnvelopeIn):
    return records.check_envelope(data.model_dump(), request.app.state.signer)


@v1.get("/events/{event_id}/duplicates")
def duplicates(request: Request, event_id: str):
    conn = conn_for(request)
    domain.require_organizer(conn, event_id, actor(request))
    return [{"first": d["first"]["id"], "second": d["second"]["id"], "reasons": d["reasons"], "resolved": d["resolved"]}
            for d in domain.duplicate_candidates(conn, event_id)]


@v1.post("/events/{event_id}/duplicates")
def resolve_duplicate(request: Request, event_id: str, data: DuplicateIn):
    domain.resolve_duplicate(conn_for(request), actor(request), data.keep, data.drop)
    return data.model_dump()


@v1.post("/projects/{project_id}/disqualify")
def disqualify(request: Request, project_id: str, data: DisqualifyIn):
    conn = conn_for(request)
    domain.set_disqualified(conn, actor(request), project_id, data.reason)
    return _project(domain.get_project(conn, project_id))


@v1.post("/events/{event_id}/void-votes")
def void_votes(request: Request, event_id: str, data: VoidVotesIn):
    return {"removed": domain.remove_votes_of(conn_for(request), actor(request), event_id, data.voter_id, data.reason)}


@v1.get("/events/{event_id}/abuse")
def abuse(request: Request, event_id: str):
    s = domain.abuse_signals(conn_for(request), actor(request), event_id)
    return {k: ([dict(r) for r in v] if isinstance(v, list) else v) for k, v in s.items()}


@v1.get("/events/{event_id}/audit")
def audit_log(request: Request, event_id: str, before: int | None = None, action: str | None = None, limit: int = 200):
    conn = conn_for(request)
    rows = domain.audit_entries(conn, actor(request), event_id, before=before, action=action, limit=max(1, min(limit, 1000)))
    return {"chain": audit.verify_chain(conn), "entries": [
        {**{k: r[k] for k in ("seq", "at", "actor_id", "action", "subject", "prev_hash", "hash")},
         "detail": json.loads(r["detail"])} for r in rows]}


# --- webhooks ------------------------------------------------------------------------------------------------


@v1.get("/events/{event_id}/webhooks")
def list_webhooks(request: Request, event_id: str):
    """This event's webhooks with the outcome of each one's latest delivery."""
    return domain.webhooks_for(conn_for(request), actor(request), event_id)


@v1.post("/events/{event_id}/webhooks", status_code=201)
def create_webhook(request: Request, event_id: str, data: WebhookIn):
    """Every audited action in this event, from now on, is POSTed to url as
    JSON with an X-Plumb-Signature: sha256=<hex HMAC of the body> header.
    The secret is shown once."""
    return domain.create_webhook(conn_for(request), actor(request), event_id, data.url, check_destination)


@v1.delete("/events/{event_id}/webhooks/{hook_id}", status_code=204)
def delete_webhook(request: Request, event_id: str, hook_id: str):
    domain.delete_webhook(conn_for(request), actor(request), event_id, hook_id)
    return Response(status_code=204)



# One line per operation for the OpenAPI document and /api. (Longer
# explanations live in the docstrings of the operations that need them.)
DESCRIPTIONS = {
    "list_events": "All events, with their phase, tracks, prizes and rubric.",
    "create_event": "Create an event (admins). The creator becomes its first organizer.",
    "get_event": "One event, plus the caller's roles in it.",
    "update_event": "Change an event's settings (organizers). Schedule changes that would reopen decided things are refused.",
    "export_event": "The whole event in the fixture format plus a plumb block; importable into another Plumb.",
    "create_team": "Start a team in an event; the caller is its first member.",
    "join_team": "Join a team with its invite code.",
    "my_team": "The caller's team in this event.",
    "leave_team": "Leave a team (until submissions close).",
    "unsubmit": "Move the caller's team project back to draft (until submissions close).",
    "gallery": "Submitted projects; search with q, filter by event and track.",
    "get_project": "One project. Drafts are visible to their team and organizers only.",
    "list_comments": "Comments on a project (organizers also see hidden ones).",
    "add_comment": "Comment on a submitted project.",
    "hide_comment": "Hide a comment with a reason (organizers). Nothing is deleted.",
    "ballot": "The caller's ballot, shuffled for them alone, with votes left.",
    "vote": "Vote for a project while voting is open.",
    "unvote": "Withdraw a vote while voting is open.",
    "tallies": "Vote counts, available to everyone only after voting closes.",
    "invite": "Create a single-use invitation link for a judge or organizer (organizers).",
    "accept": "Accept an invitation as the logged-in account it was made for.",
    "judges": "Judges of the event with their tracks and progress (organizers).",
    "judge_tracks": "Set which tracks a judge covers (organizers).",
    "auto_assign": "Bring every project up to the review target, respecting tracks and conflicts of interest.",
    "add_assignment": "Assign one judge to one project (organizers); conflicts of interest are refused.",
    "remove_assignment": "Remove an assignment that has no review yet (organizers).",
    "save_review": "Create or update the caller's review of an assigned project while judging is open.",
    "compare": "Record which of two assigned projects the calling judge thinks is better.",
    "progress": "Review coverage per project and per judge (organizers).",
    "rubric": "Reweight the rubric (organizers, before publication).",
    "results": "The normalized ranking with intervals: organizers any time, everyone after publication.",
    "verify": "Check a record envelope against this portal's signing key.",
    "duplicates": "Pairs of submissions that share a team, repository or title (organizers).",
    "resolve_duplicate": "Choose which of a duplicate pair counts (organizers, before publication).",
    "disqualify": "Disqualify with a reason, or reinstate with a null reason (organizers, before publication).",
    "void_votes": "Remove every vote of one account, with a reason (organizers).",
    "abuse": "Voting patterns worth a human look (organizers).",
    "audit_log": "The event's audit entries, newest first, with a chain verification result (organizers).",
    "delete_webhook": "Remove a webhook (organizers).",
}
for _route in v1.routes:
    if not getattr(_route, "description", "") and _route.name in DESCRIPTIONS:
        _route.description = DESCRIPTIONS[_route.name]

router.include_router(v1)


@router.get("/api", response_class=HTMLResponse, include_in_schema=False)
def api_reference(request: Request):
    spec = request.app.openapi()
    ops = []
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            ops.append({"method": method.upper(), "path": path, "summary": op.get("summary", ""),
                        "doc": (op.get("description") or "").strip()})
    return render(request, "api.html", ops=ops)
