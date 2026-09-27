"""Judge assignment. Pure: takes plain data, returns proposed pairs.

Strategy (JUDGING.md explains the why):

1. Only judges qualified for the project's track (a judge with no tracks
   listed is qualified for all of them).
2. Never a judge who is on the project's team, or shares an email domain
   with a team member when that domain is not a public mail provider.
3. Fill the projects with the fewest reviews first, so coverage evens out.
4. Among eligible judges, pick the one with the lightest current load; break
   ties with a hash of (judge, project) so the result is deterministic but
   does not always favour the same judge.
5. Prefer judges who would add a new link between parts of the judge graph
   that do not yet share a judge, because leniency can only be compared
   through shared projects.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

# Shared mail providers, where a common domain says nothing about affiliation.
# The RFC 2606 documentation domains are here too, because demo and fixture
# data use them for everyone.
PUBLIC_MAIL = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com",
    "icloud.com", "me.com", "proton.me", "protonmail.com", "aol.com", "gmx.com",
    "example.org", "example.com", "example.net",
}


@dataclass(frozen=True)
class JudgeInfo:
    id: str
    email: str
    tracks: frozenset[str]  # empty = any track


@dataclass(frozen=True)
class ProjectInfo:
    id: str
    track: str | None
    member_emails: frozenset[str]


def domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower()


def conflict(judge: JudgeInfo, project: ProjectInfo) -> str | None:
    emails = {e.lower() for e in project.member_emails}
    if judge.email.lower() in emails:
        return "judge is on this team"
    d = domain(judge.email)
    if d not in PUBLIC_MAIL and any(domain(e) == d for e in emails):
        return f"judge shares the email domain {d} with a team member"
    return None


def qualified(judge: JudgeInfo, project: ProjectInfo) -> bool:
    return not judge.tracks or (project.track is not None and project.track in judge.tracks)


def _tiebreak(judge_id: str, project_id: str) -> str:
    return hashlib.sha256(f"{judge_id}|{project_id}".encode()).hexdigest()


def propose(
    judges: list[JudgeInfo],
    projects: list[ProjectInfo],
    existing: set[tuple[str, str]],
    target: int,
) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """Return (new (judge_id, project_id) pairs, {project_id: why it is short})."""
    pairs = set(existing)
    load: dict[str, int] = {j.id: 0 for j in judges}
    count: dict[str, int] = {p.id: 0 for p in projects}
    for j, p in existing:
        if j in load:
            load[j] += 1
        if p in count:
            count[p] += 1

    # Union-find over the judge graph to prefer bridging assignments.
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for j, p in existing:
        union("j:" + j, "p:" + p)

    new: list[tuple[str, str]] = []
    short: dict[str, str] = {}
    by_id = {p.id: p for p in projects}
    while True:
        needy = sorted((pid for pid, n in count.items() if n < target and pid not in short), key=lambda pid: (count[pid], pid))
        if not needy:
            break
        pid = needy[0]
        project = by_id[pid]
        candidates = [
            j for j in judges
            if (j.id, pid) not in pairs and qualified(j, project) and conflict(j, project) is None
        ]
        if not candidates:
            short[pid] = "no qualified, conflict-free judge left for this track"
            continue
        best = min(
            candidates,
            key=lambda j: (
                load[j.id],
                0 if find("j:" + j.id) != find("p:" + pid) else 1,
                _tiebreak(j.id, pid),
            ),
        )
        pairs.add((best.id, pid))
        new.append((best.id, pid))
        load[best.id] += 1
        count[pid] += 1
        union("j:" + best.id, "p:" + pid)
    return new, short
