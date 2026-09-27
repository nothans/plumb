"""Boot tasks: create the schema, the signing key, the first admin, and (in
demo mode) the seeded fixture event with ready-made logins.

    python -m app.cli bootstrap        # what the container runs before serving
    python -m app.cli create-admin EMAIL NAME   # prompts for a password
    python -m app.cli verify-audit     # recompute the audit hash chain
    python -m app.cli backup PATH      # consistent copy of the database, safe while running
    python -m app.cli set-password EMAIL   # reset a forgotten password (prompts), ends their sessions
"""

from __future__ import annotations

import getpass
import json
from secrets import token_hex
import sqlite3
import sys
from datetime import timedelta

from . import audit, domain, records, transfer
from .config import Settings, load_settings
from .db import connect, init_schema, now, parse_ts, to_ts, transaction
from .security import hash_password, token_hash
from .signing import Signer

DEMO_PASSWORD = "plumb-demo-2026"

# Fixed demo sessions, so that .dogfood.toml can be committed with working
# headers. They exist only when PLUMB_DEMO=1, and bootstrap prints them.
DEMO_SESSIONS = {
    "organizer": ("demo-organizer-3c1f9a7e", "organizer@example.org"),
    # The two judges with the most reviews in the fixtures, filled in at seed time.
    "judge_a": ("demo-judge-a-8b27d4c0", ""),
    "judge_b": ("demo-judge-b-51e0f6a3", ""),
    "participant": ("demo-participant-e94a2b18", "priya1@example.org"),
}
DEMO_ADMIN = "admin@example.org"
FIXTURE_EVENT = "evt_01"
LIVE_EVENT = "evt_demo_live"
REPLAY_EVENT = "evt_replay"

# Invented community for the replay event, clearly demo data: two dozen
# voters who signed up before voting opened, and a planted cluster of five
# accounts made mid-vote from one documentation-range address, all voting
# for the same project, so the integrity page has something to catch.
DEMO_VOTERS = ["Amara", "Bram", "Chen", "Dalia", "Emeka", "Freya", "Goran", "Hana", "Ilse", "Joao", "Kiri", "Lena",
               "Mateo", "Nia", "Omar", "Pia", "Quinn", "Rosa", "Sven", "Tariq", "Uma", "Viktor", "Wen", "Yara"]
DEMO_COMMENTS = [
    ("Amara", "The offline mode is exactly what our meetup needed. Does it sync once the network is back?"),
    ("Bram", "Clean demo. I would love a short video of the setup."),
    ("Chen", "How does this handle two people editing at once?"),
    ("Dalia", "Great idea; the README could use a screenshot."),
]
SPAM = "Buy followers cheap!!! visit my profile"


def _judge_emails(fixtures: dict) -> dict[str, str]:
    by_id = {j["id"]: j["email"] for j in fixtures["judges"]}
    counts: dict[str, int] = {}
    for s in fixtures["scores"]:
        counts[s["judge"]] = counts.get(s["judge"], 0) + 1
    busiest = sorted(counts, key=lambda j: (-counts[j], j))
    return {"judge_a": by_id[busiest[0]], "judge_b": by_id[busiest[1]]}


def seed_demo(conn, settings: Settings) -> None:
    if conn.execute("SELECT 1 FROM meta WHERE key = 'demo_seeded'").fetchone():
        return
    fixtures = json.loads(settings.fixtures_path.read_text(encoding="utf-8"))
    emails = _judge_emails(fixtures)
    persona_emails = [DEMO_SESSIONS["organizer"][1], emails["judge_a"], emails["judge_b"],
                      DEMO_SESSIONS["participant"][1]]

    pw = hash_password(DEMO_PASSWORD)
    admin_id = domain.create_user(conn, DEMO_ADMIN, "Ada Admin", pw, is_admin=True)
    org_id = domain.create_user(conn, "organizer@example.org", "Olu Organizer", pw)
    transfer.import_event(conn, fixtures, actor_id=admin_id, organizer_ids=[org_id])

    with transaction(conn):
        # The fixture event has no voting window. Open one from now, so the
        # community ballot can be shown against real fixture projects.
        start = parse_ts(now())
        conn.execute(
            "UPDATE events SET tagline = ?, voting_mode = 'accounts', voting_open = ?, voting_close = ?, pairwise = 1 "
            "WHERE id = ?",
            ("The DOGFOOD 2026 fixture event: 41 entries (one a duplicate), 30 judges, 8 tracks.",
             to_ts(start), to_ts(start + timedelta(days=14)), FIXTURE_EVENT),
        )
        audit.append(conn, "demo.voting_opened", actor_id=admin_id, event_id=FIXTURE_EVENT, subject=FIXTURE_EVENT)
        for email in persona_emails:
            user = domain.user_by_email(conn, email)
            if user["password_hash"] is None:
                conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (pw, user["id"]))

    with transaction(conn):
        conn.execute("UPDATE users SET name = 'Priya Anand' WHERE email = ?", (DEMO_SESSIONS["participant"][1],))

    actor = domain.Actor(id=admin_id, email=DEMO_ADMIN, name="Ada Admin", is_admin=True)
    seed_replay(conn, fixtures, settings, actor, org_id)
    domain.create_event(conn, actor, {
        "name": "Plumb Demo Jam",
        "tagline": "An empty event with submissions open, for trying the participant side.",
        "submissions_open": to_ts(start - timedelta(hours=1)),
        "submissions_close": to_ts(start + timedelta(days=7)),
        "judging_close": to_ts(start + timedelta(days=10)),
        "voting_mode": "off",
    }, ["Tools", "Games", "Hardware"], [{"name": "Best in show"}, {"name": "Best first hack"}], event_id=LIVE_EVENT)
    with transaction(conn):
        conn.execute("INSERT OR IGNORE INTO event_roles(event_id, user_id, role) VALUES (?,?, 'organizer')",
                     (LIVE_EVENT, org_id))
        conn.execute("INSERT INTO meta(key, value) VALUES ('demo_seeded', ?)", (now(),))
        conn.execute("INSERT INTO meta(key, value) VALUES ('demo_judges', ?)",
                     (json.dumps({"judge_a": emails["judge_a"], "judge_b": emails["judge_b"]}),))


def seed_replay(conn, fixtures: dict, settings: Settings, admin: domain.Actor, org_id: str) -> None:
    """The fixture event again, run to the end: judged, voted, moderated and
    published. Seeded through the same domain functions a real event uses,
    so everything on its pages is produced by Plumb, not faked into tables."""
    from .signing import Signer
    doc = json.loads(json.dumps(fixtures))
    doc["event"]["name"] = "Sample Hack 2026 (published replay)"
    doc["plumb"] = {"event": {
        "tagline": "The fixture event replayed to the end: prizes awarded, a closed community vote with planted abuse, "
                   "signed records and certificates. Demo data.",
        "pairwise": 1,
    }}
    transfer.import_event(conn, doc, actor_id=admin.id, organizer_ids=[org_id], event_id=REPLAY_EVENT)
    org = domain.Actor(id=org_id, email="organizer@example.org", name="Olu Organizer", is_admin=False, ip="198.51.100.10")
    close = parse_ts(doc["event"]["submissions_close"])
    v_open, v_close = close + timedelta(days=1), close + timedelta(days=8)

    tracks = {t["name"]: t["id"] for t in domain.tracks(conn, REPLAY_EVENT)}
    with transaction(conn):
        conn.execute("UPDATE events SET voting_mode = 'accounts', voting_open = ?, voting_close = ?, judging_close = ? "
                     "WHERE id = ?", (to_ts(v_open), to_ts(v_close), to_ts(close + timedelta(days=10)), REPLAY_EVENT))
        for pos, (name, track) in enumerate([("Grand prize", None), ("Runner-up", None),
                                             ("Best security hack", tracks.get("Security")),
                                             ("Best accessibility hack", tracks.get("Accessibility"))]):
            conn.execute("INSERT INTO prizes(id, event_id, track_id, name, description, position) VALUES (?,?,?,?,?,?)",
                         (f"prz_replay_{pos}", REPLAY_EVENT, track, name, "", pos))
    projects = [r["id"] for r in domain.gallery(conn, event_id=REPLAY_EVENT, limit=10000)[0]]

    # The organizer resolves the duplicate the way a real one would.
    dup = conn.execute("SELECT id, duplicate_of FROM projects WHERE event_id = ? AND duplicate_of IS NOT NULL",
                       (REPLAY_EVENT,)).fetchone()
    if dup:
        domain.resolve_duplicate(conn, org, dup["duplicate_of"], dup["id"])

    # Community: honest voters (accounts older than the vote), then the sock puppets.
    rng = __import__("random").Random(2026)
    pw = hash_password(DEMO_PASSWORD)
    honest = []
    for i, name in enumerate(DEMO_VOTERS):
        uid = domain.create_user(conn, f"voter{i + 1:02d}@example.net", f"{name} (demo voter)", pw,
                                 ip=f"198.51.100.{20 + i}")
        honest.append((uid, name, f"198.51.100.{20 + i}"))
    socks = []
    for i in range(5):
        uid = domain.create_user(conn, f"fresh{i + 1}@example.net", f"fresh{i + 1}", pw, ip="203.0.113.7")
        socks.append(uid)
    with transaction(conn):
        for k, (uid, _, _) in enumerate(honest):
            conn.execute("UPDATE users SET created_at = ? WHERE id = ?", (to_ts(close - timedelta(days=20 - k % 10)), uid))
        for k, uid in enumerate(socks):
            conn.execute("UPDATE users SET created_at = ? WHERE id = ?", (to_ts(v_open + timedelta(days=3, minutes=k)), uid))
        target = projects[len(projects) // 2]
        for k, (uid, _, ip) in enumerate(honest):
            for pid in rng.sample(projects, 3):
                conn.execute("INSERT INTO votes(event_id, voter_id, project_id, created_at, ip, nonce) VALUES (?,?,?,?,?,?)",
                             (REPLAY_EVENT, uid, pid, to_ts(v_open + timedelta(hours=2 + 5 * k)), ip, token_hex(16)))
        for k, uid in enumerate(socks):
            conn.execute("INSERT INTO votes(event_id, voter_id, project_id, created_at, ip, nonce) VALUES (?,?,?,?,?,?)",
                         (REPLAY_EVENT, uid, target, to_ts(v_open + timedelta(days=3, minutes=2 + k)), "203.0.113.7",
                          token_hex(16)))
        audit.append(conn, "demo.votes_seeded", actor_id=admin.id, event_id=REPLAY_EVENT, subject=REPLAY_EVENT,
                     detail={"honest_voters": len(honest), "planted_sock_puppets": len(socks), "target": target})

    # Comments, one of them spam that an organizer hid.
    for (name, text), pid in zip(DEMO_COMMENTS, projects[:4]):
        uid = next(u for u, n, _ in honest if n == name)
        voter = domain.Actor(id=uid, email="", name=name, is_admin=False)
        domain.add_comment(conn, voter, pid, text)
    spammer = domain.Actor(id=socks[0], email="", name="fresh1", is_admin=False)
    cid = domain.add_comment(conn, spammer, target, SPAM)
    domain.hide_comment(conn, org, cid, "spam from a sock-puppet account")

    # An organizer has already voided two of the five; the other three are
    # still waiting on the integrity page for a decision.
    for uid in socks[:2]:
        domain.remove_votes_of(conn, org, REPLAY_EVENT, uid, "account made mid-vote, same address as four others")

    # A few direct comparisons from the three busiest judges, as their own scores order them.
    from . import pairwise, normalize
    crit = domain.criteria(conn, REPLAY_EVENT)
    by_judge: dict[str, list] = {}
    for r in domain.reviews_for(conn, event_id=REPLAY_EVENT):
        score = normalize.combine(r["values"], crit)
        if score is not None and not r["duplicate_of"]:
            by_judge.setdefault(r["judge_id"], []).append(normalize.Observation(r["judge_id"], r["project_id"], score))
    busiest = sorted(by_judge, key=lambda j: -len(by_judge[j]))[:3]
    with transaction(conn):
        for j in busiest:
            for c in pairwise.implied(by_judge[j]):
                if c.weight < 1:
                    continue
                a, b = sorted((c.winner, c.loser))
                conn.execute("INSERT OR IGNORE INTO comparisons(event_id, judge_id, project_a, project_b, winner, created_at) "
                             "VALUES (?,?,?,?,?,?)", (REPLAY_EVENT, j, a, b, c.winner, to_ts(close + timedelta(days=2))))

    # Publish, with the suggested prize winners, signed with this portal's key.
    signer = Signer.load_or_create(settings.signing_key_path)
    records.publish_results(conn, org, REPLAY_EVENT, signer, settings.base_url or "http://localhost:8080")


def ensure_demo_sessions(conn) -> dict[str, tuple[str, str]]:
    row = conn.execute("SELECT value FROM meta WHERE key = 'demo_judges'").fetchone()
    sessions = dict(DEMO_SESSIONS)
    if row:
        judges = json.loads(row["value"])
        sessions["judge_a"] = (sessions["judge_a"][0], judges["judge_a"])
        sessions["judge_b"] = (sessions["judge_b"][0], judges["judge_b"])
    with transaction(conn):
        for token, email in sessions.values():
            user = domain.user_by_email(conn, email)
            conn.execute(
                "INSERT INTO sessions(token_hash, user_id, csrf, created_at, expires_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(token_hash) DO UPDATE SET expires_at = excluded.expires_at",
                (token_hash(token), user["id"], "demo-csrf-" + token[-8:], now(), "2099-01-01T00:00:00Z"),
            )
    return sessions


def bootstrap(settings: Settings, out=sys.stdout) -> None:
    conn = connect(settings.database_path)
    init_schema(conn)
    signer = Signer.load_or_create(settings.signing_key_path)
    if settings.admin_email and settings.admin_password and not domain.user_by_email(conn, settings.admin_email):
        domain.create_user(conn, settings.admin_email, "Admin", hash_password(settings.admin_password), is_admin=True)
        print(f"created admin {settings.admin_email}", file=out)
    print(f"plumb: database {settings.database_path}", file=out)
    print(f"plumb: signing key {signer.key_id}", file=out)
    demo_db = conn.execute("SELECT 1 FROM meta WHERE key = 'demo_seeded'").fetchone() is not None
    if demo_db and not settings.demo:
        conn.close()
        raise SystemExit(
            "plumb: this database was seeded in demo mode, and its logins and sessions are public.\n"
            "plumb: refusing to start with PLUMB_DEMO=0. Start from an empty volume "
            "(docker compose down -v, then up), or keep PLUMB_DEMO=1 for the demo.")
    if settings.demo:
        seed_demo(conn, settings)
        sessions = ensure_demo_sessions(conn)
        width = max(len(r) for r in sessions)
        print("seeded. test logins:", file=out)
        for role, (token, email) in sessions.items():
            print(f"  {role:<{width}}  Cookie: plumb_session={token}   ({email})", file=out)
        print(f"  password for every demo account above, and {DEMO_ADMIN}: {DEMO_PASSWORD}", file=out)
    elif not conn.execute("SELECT 1 FROM users WHERE is_admin = 1").fetchone():
        print("plumb: no admin yet. Set PLUMB_ADMIN_EMAIL and PLUMB_ADMIN_PASSWORD, "
              "or run: python -m app.cli create-admin EMAIL NAME", file=out)
    out.flush()
    conn.close()


def main(argv: list[str]) -> int:
    settings = load_settings()
    if not argv or argv[0] == "bootstrap":
        bootstrap(settings)
        return 0
    if argv[0] == "create-admin" and len(argv) == 3:
        conn = connect(settings.database_path)
        init_schema(conn)
        password = getpass.getpass("password (12+ characters): ")
        if len(password) < 12:
            print("too short", file=sys.stderr)
            return 1
        domain.create_user(conn, argv[1], argv[2], hash_password(password), is_admin=True)
        print("admin created")
        return 0
    if argv[0] == "set-password" and len(argv) == 2:
        conn = connect(settings.database_path)
        user = domain.user_by_email(conn, argv[1])
        if user is None:
            print("no such account", file=sys.stderr)
            return 1
        password = getpass.getpass("new password (10+ characters): ")
        if len(password) < 10:
            print("too short", file=sys.stderr)
            return 1
        with transaction(conn):
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user["id"]))
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
            audit.append(conn, "user.password_reset", actor_id=None, subject=user["id"], detail={"via": "cli"})
        print("password set; existing sessions ended")
        return 0
    if argv[0] == "backup" and len(argv) == 2:
        src = connect(settings.database_path)
        dst = sqlite3.connect(argv[1])
        with dst:
            src.backup(dst)
        dst.close()
        print(f"backed up to {argv[1]}")
        return 0
    if argv[0] == "verify-audit":
        conn = connect(settings.database_path)
        result = audit.verify_chain(conn)
        print(json.dumps(result))
        return 0 if result["ok"] else 2
    print(__doc__, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
