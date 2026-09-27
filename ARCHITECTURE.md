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
│   routes/  public · account · teams · judging · organizer · api (JSON)       │
│        │ every route is thin: parse, authenticate, call one domain function  │
│        ▼                                                                      │
│   domain/    all rules and permission checks, one transaction per change,    │
│              each writing its own audit entry                                │
│   normalize.py, pairwise.py, assign.py (pure)   records.py + signing.py     │
│   audit.py (hash chain)  transfer.py (import/export)  ratelimit.py          │
│   webhooks.py  background thread streaming the audit log                     │
│        │                                                                      │
│        ▼                                                                      │
│   /data/plumb.db (SQLite, WAL)       /data/signing-key.pem (Ed25519)          │
└───────────────────────────────────────────────────────────────────────────────┘
```

## Layers

**Routes are thin.**
A route reads the request, resolves the actor (session cookie or bearer token), enforces CSRF, and calls exactly one function in the `domain` package.
The HTML routes and the JSON API call the same domain functions, so a rule enforced once is enforced for every door.
The DOGFOOD checker's "judge cannot read peer scores" check is one instance of this: the refusal lives in `domain.judge_scores`, and the page, the CSV and the API all inherit it.

**The domain layer owns every rule.**
`domain/` is plain functions over a connection (`save_project`, `save_review`, `cast_vote` and so on), split into layered modules that only import from the layers below them: `core` (errors, the actor, validation, users), `events`, `teams`, `projects`, `judging`, `results`, `voting`, `comments`, `pairwise_judging`, `hooks`, `exports`. The package re-exports every function, so callers write `domain.save_project`.
Each mutating function takes the actor, checks permission first, checks the event phase second (so a closed deadline refuses a request before anything else is looked at), validates input, then writes inside one `BEGIN IMMEDIATE` transaction together with its audit entry.
Errors are typed (`Unauthorized` 401, `Forbidden` 403, `NotFound` 404, `Conflict` 409 for "not now", `Invalid` 422), and one exception handler turns them into an HTML page or `{"error": ...}` JSON depending on who asked.
A draft project that you may not see returns 404, not 403, so its existence does not leak.

**Pure modules for the parts that need proof.**
`normalize.py` (the judging model), `pairwise.py` (Bradley-Terry) and `assign.py` (judge assignment) take plain data and return plain data: no database, no web.
They are tested directly, and `tools/normalization_proof.py` runs the exact production code in simulation.

**Phases are computed, never stored.**
Whether submissions, judging or voting are open is derived from the event's timestamps and the server clock on every request (`domain.phase`).
There is no scheduler that could fail to flip a flag, and moving a deadline takes effect immediately (and is audited with old and new values).

## Choices, and why

| Choice | Why |
|---|---|
| SQLite, one file | Zero operations, offline by construction, trivially backed up (`python -m app.cli backup`), and far more than enough: a large hackathon is thousands of rows, not millions. WAL mode lets reads continue during writes. |
| stdlib `sqlite3` and hand-written SQL, no ORM | The schema is the design (see DATA-MODEL.md), and the queries that matter (permission checks, partial unique indexes, composite foreign keys, append-only triggers) read best as SQL. |
| Constraints in the database, not just the code | One team per person per event, one live project per team, reviews only for assigned pairs, windows ordered, audit rows immutable. If the application has a bug, the database still refuses the impossible state. |
| Server-rendered HTML, no JavaScript | Every page works with scripting off, the CSP can forbid inline script entirely, and there is no build step. Forms post and redirect. |
| System fonts, no CDN | "It must run with the network off." Swagger UI is disabled for the same reason; `/api` renders the OpenAPI document as a plain page. |
| One process, in-process rate limits and webhook thread | Nothing to orchestrate. Limits reset on restart, acceptable for their purpose (slowing guessing and floods). |
| Ed25519 signatures, canonical JSON | Small keys, fast, in the standard `cryptography` package. Canonical JSON (sorted keys, no whitespace) means anyone can re-serialize and check. |
| Single-threaded BLAS | The model solves hundreds of small linear systems; multithreaded BLAS spent 60x longer waking threads than computing. Pinned in `app/__init__.py`. |

## Request lifecycle, concretely

A judge saves a review:

1. `POST /projects/prj_06/review` with the form and CSRF token.
2. `web.actor` hashes the session cookie, finds the session, builds an `Actor`. `web.body` checks the CSRF token against the session's.
3. `domain.save_review` opens `BEGIN IMMEDIATE`: is the actor a judge of this event, is this project assigned to them, is judging open (submissions closed, results not published, before `judging_close`), are all criteria present and in range.
4. Insert or update the review and its criterion values; append `review.submitted` or `review.edited` (with before and after values) to the audit chain; commit.
5. Redirect to the queue with a notice. The webhook thread picks the new audit entry up within two seconds.

## Configuration

All environment variables, all optional:

| Variable | Default | Meaning |
|---|---|---|
| `PLUMB_DEMO` | `0` (`1` in compose) | Seed the fixture event, the empty demo event and the demo logins, and print them on boot |
| `PLUMB_DATA_DIR` | `./data` (`/data` in the image) | Where the database and signing key live |
| `PLUMB_BASE_URL` | the address the visitor used | Used in invitation links, records and the widget; set it in production |
| `PLUMB_ADMIN_EMAIL`, `PLUMB_ADMIN_PASSWORD` | unset | Create the first admin on a non-demo install |
| `PLUMB_SECURE_COOKIES` | `0` | Set to `1` behind HTTPS |
| `PLUMB_WEBHOOKS` | `1` | Run the webhook delivery thread |
| `PLUMB_WEBHOOKS_ALLOW_PRIVATE` | `0` | Allow webhook receivers on private networks |
| `PLUMB_WEBHOOKS_ALLOW_HOSTS` | none | Comma-separated hostnames exempt from the private-address rule (the demo sets `host.docker.internal`) |
| `PLUMB_FIXTURES` | `fixtures.json` | The file the demo seed imports |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | uvicorn's setting: whose `X-Forwarded-For` to believe. Set it to your reverse proxy's address; never `*` |

## Running it for real

* Put it behind a reverse proxy that terminates TLS (Caddy, nginx, Traefik) and set `PLUMB_SECURE_COOKIES=1`, `PLUMB_BASE_URL`, and `FORWARDED_ALLOW_IPS` to the proxy's address.
* Set `PLUMB_DEMO=0` and create the admin with `PLUMB_ADMIN_EMAIL` and `PLUMB_ADMIN_PASSWORD` (or `python -m app.cli create-admin`).
* Back up `/data` (database and signing key) on a schedule. Losing the key does not lose data, but new records would be signed by a new key.
* One instance per database file. Plumb is not built to run several replicas against one SQLite file; for a hackathon portal, one process handles thousands of concurrent readers.

## Tests

`python -m pytest -q` runs 75 tests (CI runs them on every push, with `ruff`, the official checker and the extended checker against a real `docker compose up`): the model and Bradley-Terry on planted data, a seeded run of the normalization proof, the assigner, the seven DOGFOOD checks with the reasons behind each answer, the committed OpenAPI document, and end-to-end flows over HTTP for every role (deadline, isolation, invitation scope, voting, moderation, publication locks and verification, tamper detection, hostile input, spoofed forwarding headers, import/export round trip, webhooks), plus one regression test per finding of the second review round (`tests/test_round2.py`), and checks that every figure the documents quote is what the code computes (`tests/test_docs.py`).
`python tools/acceptance_run.py .dogfood.toml` is the official checker, byte-for-byte the published `run.py` (sha256 `aa98963841bc8e18e8e5d76f0499697c093dd3c0055f9d73a459f592f4dcf09d`).
