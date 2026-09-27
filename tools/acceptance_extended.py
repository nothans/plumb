#!/usr/bin/env python3
"""Plumb extended acceptance checker: tiers T3 and T4.

The DOGFOOD checker (tools/acceptance_run.py, the published run.py) has
checks for T1 and T2 only. This file checks the rest of the tier list the
same way: from outside, over plain HTTP, against a running portal, with the
standard library only. It trusts nothing the server says about itself:
signatures are verified with a pure-Python Ed25519 below, webhook deliveries
are received by a listener this script runs, and vote seals are recomputed.

    python3 tools/acceptance_extended.py .dogfood.toml > acceptance-report-extended.txt

It works on a scratch copy of the fixture event that it imports itself
(through the bulk import API), so the demo events are left alone. It needs
the admin header ([auth] admin in .dogfood.toml) to import, and the portal
must be able to reach this script's webhook listener: with the demo
docker-compose.yml that is host.docker.internal, which the compose file
allows. Takes about a minute: a real community vote is opened, run and
closed.
"""

import argparse
import hashlib
import hmac
import http.cookiejar
import http.server
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

VOTING_WINDOW = 40  # seconds the scratch event's community vote stays open

# --- Ed25519 verification (RFC 8032), so no crypto library is needed -----------------

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _inv(x):
    return pow(x, _P - 2, _P)


def _recover_x(y, sign):
    if y >= _P:
        return None
    x2 = (y * y - 1) * _inv(_D * y * y + 1) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


def _add(a, b):
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    A = (y1 - x1) * (y2 - x2) % _P
    B = (y1 + x1) * (y2 + x2) % _P
    C = 2 * t1 * t2 * _D % _P
    Dd = 2 * z1 * z2 % _P
    E, F, G, H = B - A, Dd - C, Dd + C, B + A
    return (E * F % _P, G * H % _P, F * G % _P, E * H % _P)


def _mul(s, p):
    q = (0, 1, 1, 0)
    while s:
        if s & 1:
            q = _add(q, p)
        p = _add(p, p)
        s >>= 1
    return q


def _equal(a, b):
    return (a[0] * b[2] - b[0] * a[2]) % _P == 0 and (a[1] * b[2] - b[1] * a[2]) % _P == 0


def _decode_point(data):
    y = int.from_bytes(data, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


_BY = 4 * _inv(5) % _P
_B = (_recover_x(_BY, 0), _BY, 1, _recover_x(_BY, 0) * _BY % _P)


def ed25519_verify(public: bytes, message: bytes, signature: bytes) -> bool:
    if len(public) != 32 or len(signature) != 64:
        return False
    A = _decode_point(public)
    R = _decode_point(signature[:32])
    s = int.from_bytes(signature[32:], "little")
    if A is None or R is None or s >= _L:
        return False
    h = int.from_bytes(hashlib.sha512(signature[:32] + public + message).digest(), "little") % _L
    return _equal(_mul(s, _B), _add(R, _mul(h, A)))


def b64u(text: str) -> bytes:
    import base64
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# --- HTTP ---------------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    """One visitor: a cookie jar, or a fixed auth header."""

    def __init__(self, base: str, header: str | None = None):
        self.base = base
        self.header = header
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), _NoRedirect)

    def request(self, method, path, *, body=None, form=None, headers=None):
        req = urllib.request.Request(self.base + path, method=method)
        if self.header:
            name, _, value = self.header.partition(":")
            req.add_header(name.strip(), value.strip())
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        if body is not None:
            req.data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        elif form is not None:
            req.data = urllib.parse.urlencode(form).encode()
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with self.opener.open(req, timeout=20) as r:
                return r.status, r.read().decode("utf-8", "replace"), {k.lower(): v for k, v in r.headers.items()}
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), {k.lower(): v for k, v in e.headers.items()}

    def json(self, method, path, body=None):
        status, text, _ = self.request(method, path, body=body)
        try:
            return status, json.loads(text) if text else None
        except ValueError:
            return status, text

    def form(self, path, data, csrf_from):
        _, page, _ = self.request("GET", csrf_from)
        m = re.search(r'name="csrf" value="([^"]+)"', page)
        return self.request("POST", path, form={**data, "csrf": m.group(1) if m else ""})


# --- reporting ------------------------------------------------------------------------------


class Report:
    def __init__(self):
        self.checks = []

    def check(self, tier, label, ok, *notes):
        self.checks.append((tier, label, bool(ok), [n for n in notes if n]))
        return bool(ok)

    def print(self, claimed):
        width = max(len(c[1]) for c in self.checks) + 2
        for tier, label, ok, notes in self.checks:
            print(f"{tier}  {label} {'.' * (width - len(label))} {'PASS' if ok else 'FAIL'}")
            if not ok:
                for n in notes:
                    print(f"       {n}")
        verified = [t for t in ("T3", "T4") if all(c[2] for c in self.checks if c[0] == t)]
        print()
        print(f"checked T3 T4, verified {' '.join(verified) or 'nothing'}")
        return verified


# --- webhook listener ------------------------------------------------------------------------


class Receiver:
    def __init__(self):
        self.received = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.received.append((dict(self.headers), body))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


# --- config ------------------------------------------------------------------------------------


def load_config(path):
    if tomllib:
        with open(path, "rb") as f:
            return tomllib.load(f)
    data, section = {}, None
    for raw in open(path, encoding="utf-8").read().splitlines():
        line = raw.split("#")[0].strip()
        if not line:
            continue
        head = re.fullmatch(r"\[([A-Za-z0-9_.]+)\]", line)
        if head:
            section = data.setdefault(head.group(1), {})
            continue
        key, sep, value = line.partition("=")
        if sep and section is not None:
            value = value.strip()
            section[key.strip()] = re.findall(r'"([^"]*)"', value) if value.startswith("[") else value.strip('"')
    return data


def ts(seconds: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- the checks ----------------------------------------------------------------------------------


def run(cfg, fixtures, webhook_host, rep: Report):
    base = cfg["portal"]["base_url"].rstrip("/")
    auth = cfg.get("auth", {})
    admin = Client(base, auth.get("admin"))
    judge = Client(base, auth.get("judge_a"))
    participant = Client(base, auth.get("participant"))
    anon = Client(base)

    # Setup: a scratch copy of the fixture event, imported through the API (itself a T4 check).
    eid = "evt_chk_" + hex(int(time.time()))[2:]
    doc = json.loads(json.dumps(fixtures))
    doc["event"]["id"] = eid
    doc["event"]["name"] = f"Acceptance check {eid}"
    status, imported = admin.json("POST", "/api/v1/events/import", doc)
    if not rep.check("T4", "bulk import creates an event", status == 201,
                     f"POST /api/v1/events/import as admin: got {status}", str(imported)[:200]):
        return
    receiver = Receiver()
    status, hook = admin.json("POST", f"/api/v1/events/{eid}/webhooks",
                              {"url": f"http://{webhook_host}:{receiver.port}/hook"})
    rep.check("T4", "webhook registered", status == 201, f"got {status}: {hook}")
    secret = hook.get("secret", "") if isinstance(hook, dict) else ""

    status, _ = admin.json("PATCH", f"/api/v1/events/{eid}", {
        "voting_mode": "accounts", "voting_open": ts(-60), "voting_close": ts(VOTING_WINDOW), "votes_per_voter": 2})
    closes_at = time.time() + VOTING_WINDOW
    rep.check("T3", "organizer opens a community vote", status == 200, f"PATCH voting window: got {status}")

    _, gallery = anon.json("GET", f"/api/v1/projects?event={eid}&limit=200")
    projects = [p["id"] for p in (gallery or {}).get("projects", [])]

    # --- T3: voting ---
    voters = []
    for i in range(4):
        c = Client(base)
        email = f"check-{eid}-{i}@example.net"
        status, _, _ = c.form("/signup", {"name": f"Check voter {i}", "email": email, "password": "check-voter-pass",
                                          "next": "/"}, "/signup")
        voters.append((c, email))
    rep.check("T3", "voters can sign up", all(c.jar and any(k.name == "plumb_session" for k in c.jar) for c, _ in voters))

    orders = []
    for c, _ in voters[:2]:
        s1, b1 = c.json("GET", f"/api/v1/events/{eid}/ballot")
        s2, b2 = c.json("GET", f"/api/v1/events/{eid}/ballot")
        orders.append(([p["id"] for p in b1["projects"]], [p["id"] for p in b2["projects"]]) if s1 == s2 == 200 else ([], [1]))
    rep.check("T3", "ballot order is random per voter", orders[0][0] != orders[1][0] and
              sorted(orders[0][0]) == sorted(orders[1][0]), "two voters got the same order, or different projects")
    rep.check("T3", "ballot order is stable for a voter", all(a == b for a, b in orders), "a reload changed the order")

    v1, v2, v3, v4 = (c for c, _ in voters)
    s_a = v1.request("POST", f"/api/v1/projects/{projects[1]}/vote")[0]
    s_dup = v1.request("POST", f"/api/v1/projects/{projects[1]}/vote")[0]
    s_b = v1.request("POST", f"/api/v1/projects/{projects[2]}/vote")[0]
    s_over = v1.request("POST", f"/api/v1/projects/{projects[3]}/vote")[0]
    rep.check("T3", "a vote counts once per project", (s_a, s_dup) == (204, 409), f"vote then repeat: {s_a}, {s_dup}")
    rep.check("T3", "votes per voter are capped", (s_b, s_over) == (204, 409), f"second then third vote: {s_b}, {s_over}")
    v2.request("POST", f"/api/v1/projects/{projects[1]}/vote")
    v3.request("POST", f"/api/v1/projects/{projects[4]}/vote")
    v3.request("POST", f"/api/v1/projects/{projects[5]}/vote")
    _, team = participant.json("GET", f"/api/v1/events/{eid}/team")
    own = next((p["id"] for p in gallery["projects"] if isinstance(team, dict) and p["team_id"] == team.get("id")), None)
    s_own = participant.request("POST", f"/api/v1/projects/{own}/vote")[0] if own else 0
    rep.check("T3", "no voting for your own team", s_own == 403, f"participant voting for own project {own}: {s_own}")
    s_judge = judge.request("POST", f"/api/v1/projects/{projects[6]}/vote")[0]
    rep.check("T3", "judges cannot vote", s_judge == 403, f"judge vote: {s_judge}")
    rep.check("T3", "anonymous visitors cannot vote", anon.request("POST", f"/api/v1/projects/{projects[6]}/vote")[0] == 401)

    # --- T3: results hidden during voting, organizers included ---
    s_tally = admin.request("GET", f"/api/v1/events/{eid}/votes")[0]
    rep.check("T3", "vote counts hidden from organizers", s_tally == 403, f"tallies while open, as organizer: {s_tally}")
    _, log = admin.json("GET", f"/api/v1/events/{eid}/audit?action=vote.cast")
    cast = (log or {}).get("entries", [])
    leaked = [e for e in cast if any(p in json.dumps(e) for p in projects)]
    rep.check("T3", "audit log does not reveal votes", cast and not leaked,
              f"{len(cast)} vote.cast entries, {len(leaked)} name a project")
    _, abuse = admin.json("GET", f"/api/v1/events/{eid}/abuse")
    named = [c for c in (abuse or {}).get("concentrated", []) if c.get("project_id")]
    rep.check("T3", "abuse signals do not name projects", not named, f"{named}")
    s_short = admin.request("PATCH", f"/api/v1/events/{eid}", body={"voting_close": ts(5)})[0]
    rep.check("T3", "an open vote cannot be cut short", s_short == 409, f"moving the close earlier: {s_short}")
    s_pub = admin.request("POST", f"/api/v1/events/{eid}/publish")[0]
    rep.check("T3", "results unpublishable during voting", s_pub == 409, f"publish while open: {s_pub}")

    # --- T3: comments and moderation ---
    status, made = v1.json("POST", f"/api/v1/projects/{projects[0]}/comments", {"body": f"check comment {eid}"})
    page = anon.request("GET", f"/projects/{projects[0]}")[1]
    rep.check("T3", "comments are posted and public", status == 201 and f"check comment {eid}" in page, f"post: {status}")
    s_hide = admin.request("POST", f"/api/v1/comments/{made['id']}/hide", body={"reason": "check"})[0] if status == 201 else 0
    page = anon.request("GET", f"/projects/{projects[0]}")[1]
    rep.check("T3", "organizers can hide a comment", s_hide == 204 and f"check comment {eid}" not in page, f"hide: {s_hide}")

    # --- T3: an answer to cheating ---
    _, abuse = admin.json("GET", f"/api/v1/events/{eid}/abuse")
    fresh = {a["email"] for a in (abuse or {}).get("fresh_accounts", [])}
    rep.check("T3", "new accounts voting are flagged", {e for _, e in voters[:3]} <= fresh, f"flagged: {sorted(fresh)}")
    v3_id = next((a["id"] for a in abuse["fresh_accounts"] if a["email"] == voters[2][1]), None)
    status, voided = admin.json("POST", f"/api/v1/events/{eid}/void-votes", {"voter_id": v3_id, "reason": "check"})
    rep.check("T3", "organizers void votes with a reason", status == 200 and voided.get("removed") == 2, f"{status} {voided}")
    codes = [v4.request("DELETE", f"/api/v1/projects/{projects[1]}/vote")[0] for _ in range(35)]
    rep.check("T3", "vote flooding is rate limited", 429 in codes, f"35 rapid requests: {sorted(set(codes))}")
    _, log = admin.json("GET", f"/api/v1/events/{eid}/audit")
    actions = {e["action"] for e in (log or {}).get("entries", [])}
    rep.check("T3", "audit trail records the moderation", {"comment.hidden", "votes.voided"} <= actions and
              (log or {}).get("chain", {}).get("ok"), f"actions: {sorted(actions)}")

    # --- close the vote ---
    time.sleep(max(0.0, closes_at - time.time()) + 2)
    status, tallies = admin.json("GET", f"/api/v1/events/{eid}/votes")
    total = sum(t["votes"] for t in (tallies or {}).get("tallies", []))
    rep.check("T3", "counts appear when voting closes", status == 200 and total == 3,
              f"got {status}, {total} votes (expected 3: 2 + 1, the voided voter's 2 removed)")
    status, reveal = admin.json("GET", f"/api/v1/events/{eid}/votes/reveal")
    seals = {e["detail"]["seal"] for e in cast}
    good = status == 200 and reveal["votes"] and all(
        hashlib.sha256(f"{v['nonce']}:{v['project']}".encode()).hexdigest() == v["seal"] and v["seal"] in seals
        for v in reveal["votes"])
    rep.check("T3", "revealed votes match the sealed log", good, f"reveal: {status}")
    s_reopen = admin.request("PATCH", f"/api/v1/events/{eid}", body={"voting_close": ts(3600)})[0]
    rep.check("T3", "a closed vote cannot reopen", s_reopen == 409, f"reopen: {s_reopen}")

    # --- T4: publication, certificates, verifiable judge records ---
    status, published = admin.json("POST", f"/api/v1/events/{eid}/publish")
    rep.check("T4", "results publish once voting closed", status == 200, f"{status} {published}")
    _, key_doc = anon.json("GET", "/.well-known/plumb-key.json")
    key = b64u(key_doc["keys"][0]["x"])
    _, recs = anon.json("GET", f"/api/v1/events/{eid}/records")
    recs = recs or []
    ok_all = recs and all(ed25519_verify(key, r["payload"].encode(), b64u(r["signature"])) for r in recs)
    rep.check("T4", "every record's Ed25519 signature verifies", ok_all, f"{len(recs)} records")
    forged = recs[0]["payload"].replace('"type"', '"typf"', 1) if recs else ""
    rep.check("T4", "a tampered record fails verification",
              recs and not ed25519_verify(key, forged.encode(), b64u(recs[0]["signature"])))
    team = next((r for r in recs if r["kind"] == "team"), None)
    cert_page = anon.request("GET", f"/records/{team['id']}")[1] if team else ""
    rep.check("T4", "teams get certificates", team and "CERTIFICATE" in cert_page)
    _, mine = judge.json("GET", "/api/v1/me")
    jrec = next((r for r in recs if r["kind"] == "judge" and json.loads(r["payload"])["judge"]["id"] == mine["id"]), None)
    _, scores = judge.json("GET", f"/api/judge/scores?event={eid}")
    items = sorted(({"project": s["project_id"], "values": s["values"], "comment": s["comment"],
                     "updated_at": s["updated_at"]} for s in scores["reviews"]), key=lambda x: x["project"])
    digest = hashlib.sha256(canonical(items).encode()).hexdigest()
    rep.check("T4", "judge record matches the judge's own scores",
              jrec and json.loads(jrec["payload"])["review_digest"] == digest, "digest recomputed from /api/judge/scores")

    # --- T4: webhooks ---
    deadline = time.time() + 20
    while time.time() < deadline and not any(b'"results.published"' in b for _, b in receiver.received):
        time.sleep(0.5)
    got = receiver.received
    signed = all(h.get("X-Plumb-Signature") == "sha256=" + hmac.new(secret.encode(), b, hashlib.sha256).hexdigest()
                 for h, b in got)
    seqs = [json.loads(b)["seq"] for _, b in got]
    acts = [json.loads(b)["action"] for _, b in got]
    rep.check("T4", "webhooks deliver every action", {"vote.cast", "comment.hidden", "results.published"} <= set(acts),
              f"received {len(got)}: {sorted(set(acts))}", f"listener at {webhook_host}:{receiver.port}")
    rep.check("T4", "webhook deliveries are signed", got and signed)
    rep.check("T4", "webhook deliveries arrive in order", got and seqs == sorted(set(seqs)))

    # --- T4: embeddable widget ---
    s_w, w_page, w_head = anon.request("GET", f"/embed/events/{eid}?limit=48")
    _, _, e_head = anon.request("GET", f"/events/{eid}")
    csp_w = w_head.get("content-security-policy", "")
    csp_e = e_head.get("content-security-policy", "")
    rep.check("T4", "gallery widget can be framed", s_w == 200 and "frame-ancestors *" in csp_w and
              fixtures["projects"][0]["title"] in w_page, f"widget CSP: {csp_w}")
    rep.check("T4", "other pages refuse framing", "frame-ancestors 'none'" in csp_e, f"event page CSP: {csp_e}")

    # --- T4: bulk export and re-import ---
    _, exported = admin.json("GET", f"/api/v1/events/{eid}/export")
    copy = json.loads(json.dumps(exported))
    copy["event"]["id"] = eid + "_rt"
    s_imp, _ = admin.json("POST", "/api/v1/events/import", copy)
    _, again = admin.json("GET", f"/api/v1/events/{eid}_rt/export")

    def shape(d):
        by = {p["id"]: (p["title"], p["submitted_at"]) for p in d["projects"]}
        return (len(d["teams"]), len(d["judges"]), sorted((by[s["project"]], canonical(s["criteria"])) for s in d["scores"]))
    rep.check("T4", "export then import round-trips", s_imp == 201 and again and shape(exported) == shape(again),
              f"re-import: {s_imp}")

    # --- T4: the REST API covers the UI ---
    _, spec = anon.json("GET", "/api/openapi.json")
    paths = {(m.upper(), p) for p, ops in (spec or {}).get("paths", {}).items() for m in ops}
    needed = [("POST", "/api/v1/events"), ("PATCH", "/api/v1/events/{event_id}"), ("POST", "/api/v1/events/{event_id}/teams"),
              ("POST", "/api/v1/teams/join"), ("PUT", "/api/v1/events/{event_id}/project"),
              ("POST", "/api/v1/events/{event_id}/invitations"), ("POST", "/api/v1/events/{event_id}/assignments/auto"),
              ("PUT", "/api/v1/projects/{project_id}/review"), ("PUT", "/api/v1/events/{event_id}/rubric"),
              ("POST", "/api/v1/events/{event_id}/publish"), ("POST", "/api/v1/projects/{project_id}/vote"),
              ("POST", "/api/v1/projects/{project_id}/comments"), ("POST", "/api/v1/events/{event_id}/webhooks"),
              ("POST", "/api/v1/events/import"), ("GET", "/api/v1/events/{event_id}/export"),
              ("POST", "/api/v1/me/tokens"), ("POST", "/api/v1/teams/{team_id}/invite-link")]
    missing = [f"{m} {p}" for m, p in needed if (m, p) not in paths]
    rep.check("T4", "OpenAPI covers the UI's actions", not missing and "securitySchemes" in spec.get("components", {}),
              f"missing: {missing}")
    status, tok = admin.json("POST", "/api/v1/me/tokens", {"label": f"check {eid}"})
    bearer = Client(base, f"Authorization: Bearer {tok['token']}") if status == 201 else None
    s_me = bearer.request("GET", "/api/v1/me")[0] if bearer else 0
    hint = next((t["hint"] for t in admin.json("GET", "/api/v1/me/tokens")[1] if t["label"] == f"check {eid}"), "")
    admin.request("DELETE", f"/api/v1/me/tokens/{hint}")
    s_after = bearer.request("GET", "/api/v1/me")[0] if bearer else 0
    rep.check("T4", "API tokens work and can be revoked", (s_me, s_after) == (200, 401), f"before/after revoke: {s_me}, {s_after}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("config", help="path to .dogfood.toml")
    ap.add_argument("--fixtures", default=None, help="path to fixtures.json (default: beside the config)")
    ap.add_argument("--webhook-host", default=os.environ.get("PLUMB_CHECK_WEBHOOK_HOST", "host.docker.internal"),
                    help="how the portal reaches this machine (default host.docker.internal)")
    args = ap.parse_args()
    cfg = load_config(args.config)
    fixtures_path = args.fixtures or os.path.join(os.path.dirname(os.path.abspath(args.config)), "fixtures.json")
    fixtures = json.load(open(fixtures_path, encoding="utf-8"))
    print("Plumb extended acceptance report (T3, T4)")
    print(f"portal: {cfg['portal']['base_url']}")
    print(f"fixtures: {os.path.basename(fixtures_path)}, imported as a scratch event")
    print()
    rep = Report()
    try:
        run(cfg, fixtures, args.webhook_host, rep)
    except Exception as exc:  # report, never crash without a verdict
        rep.check("--", "checker ran to the end", False, f"{type(exc).__name__}: {exc}")
    verified = rep.print(cfg.get("tiers", {}).get("claimed", []))
    return 0 if verified == ["T3", "T4"] else 1


if __name__ == "__main__":
    sys.exit(main())
