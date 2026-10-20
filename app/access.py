"""Who may call each route, declared in one place and enforced before the route runs.

Every route the app serves must appear in ACCESS, or the app refuses to
start (check_declared): a new route is closed until someone decides who may
call it. The declared level is checked by enforce, a dependency of every
route, before the request body is read, so a refusal never depends on the
handler remembering to check.

This is the outer wall. The domain functions keep their own, finer checks
(the team member who may edit this project, the judge assigned to this
review), so both layers have to agree for a request to go through, and
tests/test_access.py calls every route as an anonymous visitor and as a
participant to prove the outer one holds.

Levels:
  public     anyone; the domain may still refuse by phase (results before
             publication, tallies while voting is open)
  link       anyone holding a secret link (an invitation, a claim link, a
             team's join code); a wrong secret is a 404
  user       any signed-in account
  judge      a judge of the route's event
  organizer  an organizer of the route's event (or an admin)
  admin      an admin
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.routing import APIRoute

from . import domain
from .web import actor, conn_for

PUBLIC, LINK, USER, JUDGE, ORGANIZER, ADMIN = "public", "link", "user", "judge", "organizer", "admin"

ACCESS: dict[tuple[str, str], str] = {
    # --- pages: public --------------------------------------------------------------------
    ("GET", "/"): PUBLIC,
    ("GET", "/events/{event_id}"): PUBLIC,
    ("GET", "/projects"): PUBLIC,
    ("GET", "/projects/{project_id}"): PUBLIC,
    ("GET", "/events/{event_id}/votes"): PUBLIC,
    ("GET", "/events/{event_id}/results"): PUBLIC,
    ("GET", "/events/{event_id}/export/results.csv"): PUBLIC,
    ("GET", "/events/{event_id}/records"): PUBLIC,
    ("GET", "/events/{event_id}/method.json"): PUBLIC,
    ("GET", "/records/{record_id}.json"): PUBLIC,
    ("GET", "/records/{record_id}"): PUBLIC,
    ("GET", "/verify"): PUBLIC,
    ("POST", "/verify"): PUBLIC,
    ("GET", "/.well-known/plumb-key.json"): PUBLIC,
    ("GET", "/embed/events/{event_id}"): PUBLIC,
    ("GET", "/api"): PUBLIC,
    ("GET", "/api/openapi.json"): PUBLIC,
    ("GET", "/login"): PUBLIC,
    ("POST", "/login"): PUBLIC,
    ("GET", "/signup"): PUBLIC,
    ("POST", "/signup"): PUBLIC,
    ("POST", "/logout"): PUBLIC,
    # --- pages: secret links --------------------------------------------------------------
    ("GET", "/invitations/{token}"): LINK,
    ("POST", "/invitations/{token}"): LINK,
    ("GET", "/claim/{token}"): LINK,
    ("POST", "/claim/{token}"): LINK,
    ("GET", "/join/{code}"): LINK,
    # --- pages: signed in -----------------------------------------------------------------
    ("POST", "/join/{code}"): USER,
    ("GET", "/me"): USER,
    ("POST", "/me/password"): USER,
    ("POST", "/me/tokens"): USER,
    ("POST", "/me/tokens/revoke"): USER,
    ("GET", "/events/{event_id}/team"): USER,
    ("POST", "/events/{event_id}/teams"): USER,
    ("POST", "/teams/{team_id}/leave"): USER,
    ("POST", "/teams/{team_id}/reset-invite"): USER,
    ("GET", "/events/{event_id}/projects/new"): USER,
    ("POST", "/events/{event_id}/projects/new"): USER,
    ("GET", "/projects/{project_id}/edit"): USER,
    ("POST", "/projects/{project_id}/edit"): USER,
    ("POST", "/projects/{project_id}/unsubmit"): USER,
    ("POST", "/projects/{project_id}/comments"): USER,
    ("GET", "/events/{event_id}/vote"): USER,
    ("POST", "/projects/{project_id}/vote"): USER,
    ("GET", "/api/judge/scores"): USER,
    ("GET", "/projects/{project_id}/explain"): USER,
    ("GET", "/projects/{project_id}/receipt"): USER,
    ("GET", "/receipts/{number}.json"): USER,
    # --- pages: judges --------------------------------------------------------------------
    ("GET", "/events/{event_id}/judge"): JUDGE,
    ("GET", "/projects/{project_id}/review"): JUDGE,
    ("POST", "/projects/{project_id}/review"): JUDGE,
    ("GET", "/projects/{project_id}/review/receipt"): JUDGE,
    ("GET", "/events/{event_id}/compare"): JUDGE,
    ("POST", "/events/{event_id}/compare"): JUDGE,
    # --- pages: organizers ----------------------------------------------------------------
    ("GET", "/events/{event_id}/manage"): ORGANIZER,
    ("POST", "/events/{event_id}/manage/archive"): ORGANIZER,
    ("POST", "/events/{event_id}/manage/extensions"): ORGANIZER,
    ("POST", "/events/{event_id}/manage/extensions/{team_id}/revoke"): ORGANIZER,
    ("GET", "/events/{event_id}/manage/settings"): ORGANIZER,
    ("POST", "/events/{event_id}/manage/settings"): ORGANIZER,
    ("GET", "/events/{event_id}/manage/judges"): ORGANIZER,
    ("POST", "/events/{event_id}/manage/invite"): ORGANIZER,
    ("POST", "/events/{event_id}/manage/claim-link"): ORGANIZER,
    ("POST", "/events/{event_id}/manage/invitations/revoke"): ORGANIZER,
    ("POST", "/events/{event_id}/manage/judges/{judge_id}/remove"): ORGANIZER,
    ("POST", "/events/{event_id}/manage/judges/{judge_id}/tracks"): ORGANIZER,
    ("GET", "/events/{event_id}/manage/assignments"): ORGANIZER,
    ("POST", "/events/{event_id}/manage/assignments/auto"): ORGANIZER,
    ("POST", "/events/{event_id}/manage/assignments/release"): ORGANIZER,
    ("POST", "/events/{event_id}/manage/assignments/prize-line"): ORGANIZER,
    ("POST", "/events/{event_id}/manage/assignments/add"): ORGANIZER,
    ("POST", "/events/{event_id}/manage/assignments/remove"): ORGANIZER,
    ("GET", "/events/{event_id}/manage/rubric"): ORGANIZER,
    ("POST", "/events/{event_id}/manage/rubric"): ORGANIZER,
    ("POST", "/events/{event_id}/manage/rubric/lock"): ORGANIZER,
    ("GET", "/events/{event_id}/manage/reviews"): ORGANIZER,
    ("GET", "/events/{event_id}/manage/results"): ORGANIZER,
    ("POST", "/events/{event_id}/manage/publish"): ORGANIZER,
    ("GET", "/events/{event_id}/manage/integrity"): ORGANIZER,
    ("POST", "/events/{event_id}/manage/duplicates"): ORGANIZER,
    ("POST", "/events/{event_id}/manage/disqualify"): ORGANIZER,
    ("POST", "/events/{event_id}/manage/void-votes"): ORGANIZER,
    ("GET", "/events/{event_id}/manage/audit"): ORGANIZER,
    ("GET", "/events/{event_id}/manage/integrations"): ORGANIZER,
    ("POST", "/events/{event_id}/manage/integrations/webhooks"): ORGANIZER,
    ("POST", "/events/{event_id}/manage/integrations/webhooks/{hook_id}/delete"): ORGANIZER,
    ("GET", "/events/{event_id}/export/scores.csv"): ORGANIZER,
    ("GET", "/events/{event_id}/export/event.json"): ORGANIZER,
    ("POST", "/projects/{project_id}/disqualify"): ORGANIZER,
    ("POST", "/comments/{comment_id}/hide"): ORGANIZER,
    # --- pages: admins --------------------------------------------------------------------
    ("GET", "/events/new"): ADMIN,
    ("POST", "/events/new"): ADMIN,
    ("GET", "/events/import"): ADMIN,
    ("POST", "/events/import"): ADMIN,
    # --- API: public ----------------------------------------------------------------------
    ("GET", "/api/v1/events"): PUBLIC,
    ("GET", "/api/v1/events/{event_id}"): PUBLIC,
    ("GET", "/api/v1/projects"): PUBLIC,
    ("GET", "/api/v1/projects/{project_id}"): PUBLIC,
    ("GET", "/api/v1/projects/{project_id}/comments"): PUBLIC,
    ("GET", "/api/v1/events/{event_id}/votes"): PUBLIC,
    ("GET", "/api/v1/events/{event_id}/votes/reveal"): PUBLIC,
    ("GET", "/api/v1/events/{event_id}/votes/log"): PUBLIC,
    ("GET", "/api/v1/events/{event_id}/results"): PUBLIC,
    ("GET", "/api/v1/events/{event_id}/records"): PUBLIC,
    ("POST", "/api/v1/verify"): PUBLIC,
    # --- API: signed in -------------------------------------------------------------------
    ("GET", "/api/v1/me"): USER,
    ("GET", "/api/v1/me/tokens"): USER,
    ("POST", "/api/v1/me/tokens"): USER,
    ("DELETE", "/api/v1/me/tokens/{hint}"): USER,
    ("POST", "/api/v1/events/{event_id}/teams"): USER,
    ("POST", "/api/v1/teams/join"): USER,
    ("GET", "/api/v1/events/{event_id}/team"): USER,
    ("POST", "/api/v1/teams/{team_id}/invite-link"): USER,
    ("POST", "/api/v1/teams/{team_id}/leave"): USER,
    ("PUT", "/api/v1/events/{event_id}/project"): USER,
    ("POST", "/api/v1/projects/{project_id}/unsubmit"): USER,
    ("POST", "/api/v1/projects/{project_id}/comments"): USER,
    ("GET", "/api/v1/events/{event_id}/ballot"): USER,
    ("POST", "/api/v1/projects/{project_id}/vote"): USER,
    ("DELETE", "/api/v1/projects/{project_id}/vote"): USER,
    ("POST", "/api/v1/invitations/{token}/accept"): USER,
    # --- API: judges ----------------------------------------------------------------------
    ("GET", "/api/v1/events/{event_id}/queue"): JUDGE,
    ("PUT", "/api/v1/projects/{project_id}/review"): JUDGE,
    ("GET", "/api/v1/events/{event_id}/compare/next"): JUDGE,
    ("POST", "/api/v1/events/{event_id}/comparisons"): JUDGE,
    # --- API: organizers ------------------------------------------------------------------
    ("PATCH", "/api/v1/events/{event_id}"): ORGANIZER,
    ("POST", "/api/v1/events/{event_id}/archive"): ORGANIZER,
    ("POST", "/api/v1/events/{event_id}/extensions"): ORGANIZER,
    ("DELETE", "/api/v1/events/{event_id}/extensions/{team_id}"): ORGANIZER,
    ("DELETE", "/api/v1/events/{event_id}/archive"): ORGANIZER,
    ("GET", "/api/v1/events/{event_id}/export"): ORGANIZER,
    ("POST", "/api/v1/comments/{comment_id}/hide"): ORGANIZER,
    ("POST", "/api/v1/events/{event_id}/invitations"): ORGANIZER,
    ("GET", "/api/v1/events/{event_id}/judges"): ORGANIZER,
    ("PUT", "/api/v1/events/{event_id}/judges/{judge_id}/tracks"): ORGANIZER,
    ("POST", "/api/v1/events/{event_id}/assignments/auto"): ORGANIZER,
    ("POST", "/api/v1/events/{event_id}/assignments"): ORGANIZER,
    ("DELETE", "/api/v1/events/{event_id}/assignments"): ORGANIZER,
    ("GET", "/api/v1/events/{event_id}/pairwise"): ORGANIZER,
    ("GET", "/api/v1/events/{event_id}/progress"): ORGANIZER,
    ("PUT", "/api/v1/events/{event_id}/rubric"): ORGANIZER,
    ("POST", "/api/v1/events/{event_id}/rubric/lock"): ORGANIZER,
    ("POST", "/api/v1/events/{event_id}/publish"): ORGANIZER,
    ("GET", "/api/v1/events/{event_id}/awards/suggested"): ORGANIZER,
    ("GET", "/api/v1/events/{event_id}/duplicates"): ORGANIZER,
    ("POST", "/api/v1/events/{event_id}/duplicates"): ORGANIZER,
    ("POST", "/api/v1/projects/{project_id}/disqualify"): ORGANIZER,
    ("POST", "/api/v1/events/{event_id}/void-votes"): ORGANIZER,
    ("GET", "/api/v1/events/{event_id}/abuse"): ORGANIZER,
    ("GET", "/api/v1/events/{event_id}/audit"): ORGANIZER,
    ("GET", "/api/v1/events/{event_id}/webhooks"): ORGANIZER,
    ("POST", "/api/v1/events/{event_id}/webhooks"): ORGANIZER,
    ("DELETE", "/api/v1/events/{event_id}/webhooks/{hook_id}"): ORGANIZER,
    # --- API: admins ----------------------------------------------------------------------
    ("POST", "/api/v1/events"): ADMIN,
    ("POST", "/api/v1/events/import"): ADMIN,
}


def routes_of(app: FastAPI) -> list[tuple[str, str]]:
    """Every (method, path) the app answers, the OpenAPI document included."""
    out = []
    for route in app.routes:
        if isinstance(route, APIRoute) or getattr(route, "methods", None):
            out += [(m, route.path) for m in sorted(route.methods) if m != "HEAD"]
    return out


def check_declared(app: FastAPI) -> None:
    """Refuse to start if a route has no declared access, or a declaration
    names a route that does not exist (a rename would leave it stale)."""
    served = set(routes_of(app))
    undeclared = sorted(served - set(ACCESS))
    stale = sorted(set(ACCESS) - served)
    if undeclared or stale:
        raise RuntimeError("access policy out of date: " + "; ".join(
            [f"undeclared {m} {p}" for m, p in undeclared] + [f"declared but not served {m} {p}" for m, p in stale]))


def _event_of(conn, params: dict) -> str | None:
    """The event a route's path is about, or None when its row does not
    exist (the handler then answers 404)."""
    if "event_id" in params:
        return params["event_id"]
    lookups = (("project_id", "SELECT event_id FROM projects WHERE id = ?"),
               ("team_id", "SELECT event_id FROM teams WHERE id = ?"),
               ("comment_id", "SELECT p.event_id FROM comments c JOIN projects p ON p.id = c.project_id WHERE c.id = ?"))
    for key, sql in lookups:
        if key in params:
            row = conn.execute(sql, (params[key],)).fetchone()
            return row[0] if row else None
    return None


def enforce(request: Request) -> None:
    route = request.scope.get("route")
    method = "GET" if request.method == "HEAD" else request.method
    level = ACCESS.get((method, getattr(route, "path", "")))
    if level is None:
        raise domain.Forbidden("this route has no declared access, so it is closed")
    if level in (PUBLIC, LINK):
        return
    me = domain.require_user(actor(request))
    if level == USER:
        return
    if level == ADMIN:
        if not me.is_admin:
            raise domain.Forbidden("only admins can do that")
        return
    conn = conn_for(request)
    event_id = _event_of(conn, request.path_params)
    if event_id is None or not conn.execute("SELECT 1 FROM events WHERE id = ?", (event_id,)).fetchone():
        return  # nothing to protect; the handler answers 404
    if level == ORGANIZER:
        domain.require_organizer(conn, event_id, me)
    elif level == JUDGE:
        domain.require_judge(conn, event_id, me)
