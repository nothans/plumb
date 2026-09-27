from app.assign import JudgeInfo, ProjectInfo, conflict, propose


def J(i, tracks=(), email=None):
    return JudgeInfo(id=i, email=email or f"{i}@gmail.com", tracks=frozenset(tracks))


def P(i, track, members=()):
    return ProjectInfo(id=i, track=track, member_emails=frozenset(members))


def test_fills_to_target_within_tracks_and_balances_load():
    judges = [J("a", ["t1"]), J("b", ["t1"]), J("c", ["t1"]), J("d", ["t2"]), J("e", ["t2"]), J("f", ["t2"])]
    projects = [P(f"p{i}", "t1" if i < 4 else "t2") for i in range(8)]
    new, short = propose(judges, projects, set(), target=3)
    assert not short
    by_project = {}
    for j, p in new:
        by_project.setdefault(p, set()).add(j)
    track_of = {j.id: next(iter(j.tracks)) for j in judges}
    proj_track = {p.id: p.track for p in projects}
    assert all(len(v) == 3 for v in by_project.values())
    assert all(track_of[j] == proj_track[p] for j, p in new)
    loads = {}
    for j, _ in new:
        loads[j] = loads.get(j, 0) + 1
    assert max(loads.values()) - min(loads.values()) <= 1


def test_never_assigns_a_conflicted_judge():
    judges = [J("a", email="ana@acme.io"), J("b", email="bo@gmail.com"), J("c", email="cy@gmail.com")]
    projects = [P("p1", None, ["zed@acme.io"]), P("p2", None, ["bo@gmail.com"])]
    new, short = propose(judges, projects, set(), target=2)
    assert ("a", "p1") not in new and ("b", "p2") not in new
    assert conflict(judges[0], projects[0]) and conflict(judges[1], projects[1])
    assert conflict(judges[2], projects[0]) is None  # a shared public mail domain is not a conflict


def test_reports_projects_it_cannot_cover_and_keeps_existing():
    judges = [J("a", ["t1"])]
    projects = [P("p1", "t1"), P("p2", "t9")]
    new, short = propose(judges, projects, {("a", "p1")}, target=2)
    assert new == []
    assert set(short) == {"p1", "p2"}


def test_deterministic():
    judges = [J(x) for x in "abcdef"]
    projects = [P(f"p{i}", None) for i in range(10)]
    assert propose(judges, projects, set(), 3) == propose(judges, projects, set(), 3)
