"""CSV exports and the audit log view."""

from __future__ import annotations

import csv
import io
import sqlite3

from .. import normalize
from ..security import new_token, token_hash  # noqa: F401  (new_token also seals votes)
from .core import (  # noqa: F401
    Actor,
)
from .events import (  # noqa: F401
    criteria,
    require_organizer,
)
from .judging import (  # noqa: F401
    reviews_for,
)
from .results import (  # noqa: F401
    results_for_viewer,
)


def _cell(value):
    """Neutralise spreadsheet formulas: a cell a participant controls must
    never execute when an organizer opens the export."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


class _SafeWriter:
    def __init__(self, buf):
        self._w = csv.writer(buf)

    def writerow(self, row):
        self._w.writerow([_cell(v) for v in row])


def scores_csv(conn: sqlite3.Connection, actor: Actor | None, event_id: str) -> str:
    require_organizer(conn, event_id, actor)
    crit = criteria(conn, event_id)
    buf = io.StringIO()
    w = _SafeWriter(buf)
    w.writerow(["event_id", "project_id", "project_title", "team_id", "track_id", "judge_id", "judge_name",
                *[c["key"] for c in crit], "weighted_score_0_100", "eligible", "comment", "submitted_at", "updated_at"])
    for r in reviews_for(conn, event_id=event_id):
        score = normalize.combine(r["values"], crit)
        eligible = not (r["duplicate_of"] or r["disqualified_reason"] or r["project_status"] != "submitted")
        w.writerow([event_id, r["project_id"], r["project_title"], r["team_id"], r["track_id"] or "", r["judge_id"],
                    r["judge_name"], *[r["values"].get(c["key"], "") for c in crit],
                    "" if score is None else f"{score:.2f}", "yes" if eligible else "no",
                    r["comment"], r["submitted_at"], r["updated_at"]])
    return buf.getvalue()


def results_csv(conn: sqlite3.Connection, actor: Actor | None, event_id: str) -> str:
    res = results_for_viewer(conn, actor, event_id)
    buf = io.StringIO()
    w = _SafeWriter(buf)
    w.writerow(["rank", "project_id", "project_title", "team", "track", "reviews", "raw_mean_0_100", "raw_rank",
                "adjusted_0_100", "interval90_low", "interval90_high", "p_beats_next"])
    for p in res["fit"].projects:
        proj = res["projects"][p.project]
        w.writerow([p.rank, p.project, proj["title"], proj["team_name"], proj["track_name"] or "", p.n_reviews,
                    f"{p.raw_mean:.2f}", p.raw_rank, f"{p.adjusted:.2f}", f"{p.low:.2f}", f"{p.high:.2f}",
                    "" if p.p_above_next is None else f"{p.p_above_next:.3f}"])
    return buf.getvalue()


def audit_entries(conn: sqlite3.Connection, actor: Actor | None, event_id: str, *, limit: int = 200,
                  before: int | None = None, action: str | None = None) -> list[sqlite3.Row]:
    require_organizer(conn, event_id, actor)
    where, args = ["a.event_id = ?"], [event_id]
    if before:
        where.append("a.seq < ?")
        args.append(before)
    if action:
        where.append("a.action LIKE ?")
        args.append(action.replace("%", "") + "%")
    return conn.execute(
        "SELECT a.*, u.name AS actor_name, u.email AS actor_email FROM audit_log a LEFT JOIN users u ON u.id = a.actor_id "
        f"WHERE {' AND '.join(where)} ORDER BY a.seq DESC LIMIT ?",
        (*args, limit),
    ).fetchall()
