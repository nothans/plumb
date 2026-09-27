# Data model

Plumb stores everything in one SQLite file (`/data/plumb.db` in the container), with foreign keys on, WAL journaling, and the schema in [`app/schema.sql`](app/schema.sql).
This page explains the tables, the rules the database itself enforces, and the ways data gets in and out.

## Conventions

* **Ids are short prefixed strings** (`evt_`, `usr_`, `tm_`, `prj_`, `rev_`, `rec_` ...). Imported ids are kept when they are free, so a fixture file's `prj_07` is still `prj_07` inside Plumb and in every export.
* **Timestamps are ISO 8601 UTC strings** (`2026-03-01T18:00:00Z`). They sort correctly as text, which is what the deadline and window comparisons rely on.
* **Money-like numbers do not exist here.** Scores are integers per criterion; everything derived (weighted scores, normalized scores, intervals) is computed at read time and never stored, except inside signed records, where it is frozen on purpose.

## Tables

```
users ──< sessions            users ──< api_tokens
  │
  ├──< event_roles >── events ──< tracks ──< judge_tracks >── users
  │     (organizer, judge)  │   ──< prizes
  │                         │   ──< criteria ──< review_scores
  │                         │   ──< invitations
  ├──< team_members >── teams ──< projects ──< assignments >── users (judge)
  │                                   │  └──< reviews ──< review_scores
  │                                   ├──< votes >── users (voter)
  │                                   ├──< comments >── users
  │                                   ├──< comparisons >── users (judge)
  │                                   └──< awards >── prizes
  └── audit_log (append-only, hash-chained)     records (signed)     webhooks ──< webhook_deliveries
```

| Table | What a row is | Rules worth knowing |
|---|---|---|
| `users` | An account | Email unique, case-insensitive. `password_hash` is NULL for accounts created by an import or invitation that nobody has claimed yet. `is_admin` is global. |
| `sessions` | A login | Stores the SHA-256 of the cookie, never the cookie. Carries the per-session CSRF token. |
| `api_tokens` | A bearer token | Stored hashed; shown to the user once. |
| `events` | A hackathon | Schedule (`submissions_open`, `submissions_close`, optional `judging_close`), voting window and mode, votes per voter, review target, team size cap, `results_published_at`. CHECK constraints keep windows ordered. |
| `tracks`, `prizes` | Event structure | Prizes can belong to a track or be overall. |
| `event_roles` | Organizer or judge of one event | Participants are not listed here: being on a team is what makes someone a participant. Admins act as organizers everywhere. |
| `judge_tracks` | Which tracks a judge covers | No rows means any track. Drives auto-assignment. |
| `invitations` | A single-use link to become judge or organizer | Token stored hashed. Bound to one email. |
| `teams`, `team_members` | A team and its people | `team_members` repeats `event_id` so that `UNIQUE (event_id, user_id)` can enforce one team per person per event in the database. Team names are not unique (the fixtures have three different teams called StillTrail); new teams made in Plumb are refused a taken name by the application. |
| `projects` | A submission | `status` is `draft` or `submitted` (CHECK: submitted rows have `submitted_at`). `duplicate_of` points at the entry this one duplicates. A partial unique index allows only one live (non-duplicate) project per team. `disqualified_reason` removes it from gallery, ballot and ranking without deleting anything. |
| `criteria` | The rubric | Per event: key, name, weight, min and max. Weights change freely before publication; criteria can only be added before the first review. |
| `assignments` | Judge J should review project P | Primary key `(judge, project)`. `source` records whether it came from an import, the auto-assigner or an organizer. |
| `reviews`, `review_scores` | A judge's review and its per-criterion values | One review per `(judge, project)`, and a composite foreign key to `assignments`, so the database refuses a review nobody assigned. Values live in their own table so the rubric can differ between events without schema changes. |
| `comparisons` | A judge's pick between two assigned projects (pairwise mode) | Primary key `(judge, project_a, project_b)` with `project_a < project_b`: one answer per pair per judge. The winner must be one of the two (CHECK). |
| `awards` | Who won which prize | Written once, at publication, in the same transaction that signs the results; the signed record carries the same list. |
| `votes` | A community vote | Primary key `(voter, project)`: one vote per project per voter. The per-voter limit is enforced in a transaction. |
| `comments` | A comment on a project | Hidden, never deleted, by organizers, with a reason. |
| `audit_log` | One state change | See below. |
| `records` | A signed statement | The exact signed canonical JSON, its signature and key id. Never updated. |
| `webhooks`, `webhook_deliveries` | Outbound event stream | `cursor` is the last audit sequence number delivered. |

## The audit log

Every change an organizer might later have to explain appends a row to `audit_log` in the same transaction as the change: event and deadline edits (with old and new values), team formation, submissions and edits, invitations, assignments, every review and review edit (with before and after values), rubric changes, duplicates and disqualifications, votes and voided votes, hidden comments, publication, webhook changes, logins.

Each row stores `hash = sha256(canonical_json(seq, at, event_id, actor_id, action, subject, detail, prev_hash))`, where `prev_hash` is the previous row's hash.
Editing or deleting any past row therefore breaks every hash after it.
Two triggers make the table append-only at the database level (`UPDATE` and `DELETE` abort).
`python -m app.cli verify-audit` and the organizer Integrity page recompute the whole chain.
Published results records carry the chain head, which pins history up to publication under the portal's signature.

## Getting data in

* **Fixture-shaped import.** `POST /api/v1/events/import` (or Organizer, Import an event) accepts the DOGFOOD `fixtures.json` shape: `event`, `tracks`, `judges`, `teams`, `projects`, `scores`. The demo seed is this importer fed `fixtures.json`, twice: once as the live fixture event, and once as the published replay.
  * The whole file is validated before anything is written (unknown teams, tracks, judges or projects, repeated judge-project pairs, scores without criteria) and imported in one transaction: all or nothing.
  * Ids that are already taken in this database (importing a copy, or two files that both say `trk_01`) get fresh ids, and every reference in the file follows them.
  * Judges and team members become accounts without passwords; judges claim theirs through an invitation link.
  * Each score becomes an assignment plus a review. Criteria come from the score keys (weight 1, 1 to 5, widened if the data goes outside it).
  * A second project by the same team becomes a duplicate of the first (unless a Plumb export says which one the organizer kept); teams sharing a name are reported.
  * Every date is validated before anything is written.
* **Accounts, teams and projects** arrive through the UI or the API like anything else.

## Getting data out

* `GET /events/{id}/export/event.json` (organizer): the same fixture shape, plus a `plumb` block with everything Plumb-specific (schedule, voting settings, pairwise setting, rubric with weights, prizes, assignments, comparisons, duplicate decisions, audit head). Projects are exported in full, drafts and disqualifications included, so a reviewed entry that went back to draft still round-trips with its reviews. `tests/test_transfer.py` exports, re-imports as a copy and compares every field.
* `GET /events/{id}/export/scores.csv` (organizer): one row per review with every criterion, the weighted 0-100 score, eligibility, comment and timestamps.
* `GET /events/{id}/export/results.csv` (organizer, or anyone after publication): rank, raw mean, adjusted score, 90% interval, `P(beats next)`.
* `GET /records/{id}.json`: any signed record as a portable envelope.
* The SQLite file itself, consistently, while the portal runs: `docker compose exec plumb python -m app.cli backup /data/backup.db`, then `docker compose cp plumb:/data/backup.db .`. Keep `/data/signing-key.pem` with it: records stay verifiable only with the same key.

## Migrations

`schema.sql` always describes the current shape and is idempotent (`CREATE ... IF NOT EXISTS`), so a new database needs nothing else.
An existing database stores its version in `meta.schema_version`; on boot, `db.init_schema` runs every numbered migration in `db.MIGRATIONS` above that version, each in its own transaction, then applies `schema.sql` for anything new (whole new tables need no migration).
The current version is 2 (version 2 added the `pairwise` setting to events).
Upgrading is: back up, pull the new image, start it.
