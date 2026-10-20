# Architecture

Plumb is one Python process and one SQLite file.
That is a deliberate choice for the job it has: a portal an organizer can run for a decade on a small machine, back up by copying a file, and bring up with the network off.

```
browser / curl / agent
        │  HTTPS (terminated by your proxy) or HTTP on :8080
        ▼
┌─────────────────────────────── one container ───────────────────────────────┐
│ uvicorn ── FastAPI app (app/main.py)                                         │
│   middleware: per-request SQLite connection, CSP + security headers          │
│   access.py: every route's declared level, checked before the route runs     │
│   routes/  public · account · teams · judging · organizer · api (JSON)       │
│        │ parse, authenticate, call the domain                                 │
│        ▼                                                                      │
│   domain/    the rules and permission checks, one transaction per change,    │
│              each writing its own audit entry                                │
│   normalize.py, pairwise.py, assign.py (pure)   method.py (the registered    │
│   judging method)   records.py + signing.py (records, receipts, keys)        │
│   audit.py (hash chain)  transfer.py (import/export)  ratelimit.py          │
│   backup.py (backup, restore, doctor)                                        │
│   webhooks.py  one delivery thread per hook, streaming the audit log         │
│        │                                                                      │
│        ▼                                                                      │
│   /data/plumb.db (SQLite, WAL; the rules again, as triggers)                 │
│   /data/signing-key.pem (Ed25519, 0600)   /data/retired-keys.json            │
└───────────────────────────────────────────────────────────────────────────────┘
```

## Layers

**Access is declared, and closed by default.**
`app/access.py` names the level of every route (public, a secret link, any signed-in account, a judge or an organizer of the route's event, an admin), and a dependency of every route checks it before the route runs and before the request body is read.
A route missing from the table, or a declaration for a route that no longer exists, stops the app from starting.
`tests/test_access.py` calls every route as an anonymous visitor and as a participant and checks each refusal.
This is the outer wall: the domain keeps its own, finer checks.

**Routes call the domain.**
A route reads the request, resolves the actor (session cookie or bearer token), enforces CSRF on forms, and calls the `domain` package for anything that changes state.
The HTML routes and the JSON API call the same domain functions, so a rule enforced once is enforced for every door.
The DOGFOOD checker's "judge cannot read peer scores" check is one instance of this: the refusal lives in `domain.judge_scores`, and the page, the CSV and the API all inherit it.
A few organizer pages assemble their view with a query of their own; none of those write.

**The domain layer owns every rule.**
`domain/` is plain functions over a connection (`save_project`, `save_review`, `cast_vote` and so on), split into layered modules that only import from the layers below them: `core` (errors, the actor, validation, users), `events`, `teams`, `projects`, `judging`, `results`, `voting`, `comments`, `pairwise_judging`, `hooks`, `exports`. The package re-exports every function, so callers write `domain.save_project`.
Each mutating function takes the actor, looks up only what it needs to know which event the request is about, checks permission, checks the event phase (so a closed deadline refuses a request before its content is looked at), validates input, then writes inside one `BEGIN IMMEDIATE` transaction together with its audit entry.
Errors are typed (`Unauthorized` 401, `Forbidden` 403, `NotFound` 404, `Conflict` 409 for "not now", `Invalid` 422), and one exception handler turns them into an HTML page or `{"error": ...}` JSON depending on who asked.
A draft project that you may not see returns 404, not 403, so its existence does not leak.

**Pure modules for the parts that need proof.**
`normalize.py` (the judging model), `pairwise.py` (Bradley-Terry) and `assign.py` (judge assignment) take plain data and return plain data: no database, no web.
They are tested directly, and `tools/normalization_proof.py` runs the exact production code in simulation.

**The rules live in the database too.**
The rules that must never break are SQLite triggers as well as code (`app/schema.sql`, the `rule_` triggers): scores inside their range, reviews, scores, votes and awards frozen after publication, the rubric frozen once scored and once locked, signed records and receipts never changed, a vote inside its window, an entry submitted by its deadline or its team's extension, and every audit entry linked to the one before.
Each refusal starts with `plumb:` and becomes a 409 with that message.
At every boot, `db.check_rules` compares each trigger in the database with the one the code defines and refuses to start if one was changed by hand.

**Phases are computed, never stored.**
Whether submissions, judging or voting are open is derived from the event's timestamps and the server clock on every request (`domain.phase`).
There is no scheduler that could fail to flip a flag, and moving a deadline takes effect immediately (and is audited with old and new values).

## Choices, and why

| Choice | Why |
|---|---|
| SQLite, one file | Zero operations, offline by construction, trivially backed up (`python -m app.cli backup`), and far more than enough: a large hackathon is thousands of rows, not millions. WAL mode lets reads continue during writes. |
| stdlib `sqlite3` and hand-written SQL, no ORM | The schema is the design (see DATA-MODEL.md), and the queries that matter (permission checks, partial unique indexes, composite foreign keys, append-only triggers) read best as SQL. |
| Constraints and rule triggers in the database, not just the code | One team per person per event, one live project per team, reviews only for assigned pairs, windows ordered, audit rows immutable, and the rule triggers above. If the application has a bug, the database still refuses the impossible state. |
| Server-rendered HTML, no JavaScript | Every page works with scripting off, the CSP can forbid inline script entirely, and there is no build step. Forms post and redirect. |
| System fonts, no CDN | "It must run with the network off." Swagger UI is disabled for the same reason; `/api` renders the OpenAPI document as a plain page. |
| One process, in-process rate limits and webhook threads | Nothing to orchestrate. Limits reset on restart, acceptable for their purpose (slowing guessing and floods), and `PLUMB_RATE_LIMITS` sets them. Each webhook delivers on its own thread, so a slow receiver delays only its own stream. |
| Ed25519 signatures, canonical JSON | Small keys, fast, in the standard `cryptography` package. Canonical JSON (sorted keys, no whitespace) means anyone can re-serialize and check. |
| Single-threaded BLAS | The model solves hundreds of small linear systems; multithreaded BLAS spent 60x longer waking threads than computing. Pinned in `app/__init__.py`. |

## Request lifecycle, concretely

A judge saves a review:

1. `POST /projects/prj_06/review` with the form and CSRF token.
2. `web.actor` hashes the session cookie, finds the session, builds an `Actor`; `access.enforce` checks that the actor judges this project's event; `web.form_body` checks the CSRF token against the session's.
3. `domain.save_review` opens `BEGIN IMMEDIATE`: is the actor a judge of this event, is this project assigned to them, is judging open (submissions closed, results not published, before `judging_close`), are all criteria present and in range.
4. Insert or update the review and its criterion values (the rule triggers check the range and the publication freeze again); drop the judge's draft; append `review.submitted` or `review.edited` (with before and after values) to the audit chain; commit.
5. Redirect to the receipt page, which signs a numbered receipt for exactly this review the first time it is opened. The hook's delivery thread picks the new audit entry up within two seconds.

## Configuration

All environment variables, all optional:

| Variable | Default | Meaning |
|---|---|---|
| `PLUMB_DEMO` | `0` (`1` in compose) | Seed the fixture event, the empty demo event and the demo logins, and print them on boot |
| `PLUMB_DATA_DIR` | `./data` (`/data` in the image) | Where the database and signing key live |
| `PLUMB_BASE_URL` | unset | The public address, signed into every record and receipt as their issuer; until it is set, links use the address the visitor came in on, receipts name no issuer, and publishing is refused |
| `PLUMB_ADMIN_EMAIL`, `PLUMB_ADMIN_PASSWORD` | unset | Create the first admin on a non-demo install |
| `PLUMB_SECURE_COOKIES` | `0` (`1` in `compose.prod.yml`) | Set to `1` behind HTTPS; a real instance refuses to start with an https base URL and this off, or the reverse |
| `PLUMB_RATE_LIMITS` | the defaults in `ratelimit.py` | Per bucket, `attempts/seconds`, like `signup=1000/3600,login-ip=2000/300`, for a venue behind one address |
| `PLUMB_WEBHOOKS` | `1` | Run the webhook delivery thread |
| `PLUMB_WEBHOOKS_ALLOW_PRIVATE` | `0` | Allow webhook receivers on private networks |
| `PLUMB_WEBHOOKS_ALLOW_HOSTS` | none | Demo mode only: hostnames exempt from the private-address rule (the demo sets `host.docker.internal` so the extended checker can receive) |
| `PLUMB_FIXTURES` | `fixtures.json` | The file the demo seed imports |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | uvicorn's setting: whose `X-Forwarded-For` to believe, as addresses or networks (`172.16.0.0/12`). A real instance refuses `*` and any entry uvicorn would silently ignore |

## Running it for real

[OPERATIONS.md](OPERATIONS.md) is the runbook.
One instance per database file: Plumb is not built to run several replicas against one SQLite file; for a hackathon portal, one process handles thousands of concurrent readers.

## Tests

`python -m pytest -q` runs the suite (the count is in the README's table, which CI checks against it). CI runs it on every push, with `ruff`, the official checker and the extended checker against a real `docker compose up`, and both checkers again in a container with no network.
It covers the model and Bradley-Terry on planted data and the probability model's properties (`tests/test_statistics.py`), a seeded run of the normalization proof including small events, the assigner, the seven DOGFOOD checks with the reasons behind each answer, the committed OpenAPI document, end-to-end flows over HTTP for every role, the access policy route by route (`tests/test_access.py`), every rule trigger broken in raw SQL (`tests/test_rules.py`), accounts, claim links, extensions and organizer tools (`tests/test_accounts.py`), drafts and receipts, backup and restore, an upgrade of a real v1.0 database (`tests/test_upgrade.py`), no external resources on any page, one regression test per finding of the review rounds, page copy that must tell the truth (`tests/test_pages.py`), and checks that every figure the documents quote is what the code computes (`tests/test_docs.py`).
`python tools/acceptance_run.py .dogfood.toml` is the official checker, byte-for-byte the published `run.py` (sha256 `aa98963841bc8e18e8e5d76f0499697c093dd3c0055f9d73a459f592f4dcf09d`).
