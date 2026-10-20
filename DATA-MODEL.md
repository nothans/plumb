# Data model

Plumb stores everything in one SQLite file (`/data/plumb.db` in the container), with foreign keys on, WAL journaling, and the schema in [`app/schema.sql`](app/schema.sql).
This page explains the tables, the rules the database itself enforces, and the ways data gets in and out.
The rules that must never break are SQLite triggers as well as code (the `rule_` triggers at the end of `schema.sql`), and every boot checks that the database still holds them as written ("Rules in the database", below).

## Conventions

* **Ids are short prefixed strings** (`evt_`, `usr_`, `tm_`, `prj_`, `rev_`, `rec_` ...). Imported ids are kept when they are free, so a fixture file's `prj_07` is still `prj_07` inside Plumb and in every export.
* **Timestamps are ISO 8601 UTC strings** (`2026-03-01T18:00:00Z`). They sort correctly as text, which is what the deadline and window comparisons rely on.
* **Money-like numbers do not exist here.** Scores are integers per criterion; everything derived (weighted scores, normalized scores, intervals) is computed at read time and never stored, except inside signed records, where it is frozen on purpose.

## Tables

```
users ──< sessions            users ──< api_tokens            users ──< claim_links
  │
  ├──< event_roles >── events ──< tracks ──< judge_tracks >── users
  │     (organizer, judge)  │   ──< prizes
  │                         │   ──< criteria ──< review_scores
  │                         │   ──< invitations
  ├──< team_members >── teams ──< projects ──< assignments >── users (judge)
  │                       │           │  ├──< reviews ──< review_scores
  │                       │           │  └──< review_drafts
  │                       │           ├──< votes >── users (voter)
  │                       │           ├──< comments >── users
  │                       │           ├──< comparisons >── users (judge)
  │                       │           └──< awards >── prizes
  │                       └── team_extensions
  └── audit_log (append-only, hash-chained)   records (signed)   receipts (signed, numbered)
      webhooks ──< webhook_deliveries          review_salts       meta
```

| Table | What a row is | Rules worth knowing |
|---|---|---|
| `users` | An account | Email unique, case-insensitive. `password_hash` is NULL for accounts created by an import or invitation that nobody has claimed yet. `is_admin` is global. |
| `sessions` | A login | Stores the SHA-256 of the cookie, never the cookie. Carries the per-session CSRF token. |
| `api_tokens` | A bearer token | Stored hashed; shown to the user once; expires after 30, 90 or 365 days. Changing or resetting a password deletes the account's tokens and sessions. |
| `claim_links` | A single-use link to set the password of an imported account | Token stored hashed; 14 days; made only by someone who organizes every event the account belongs to; a new one deletes the account's earlier unused ones. |
| `events` | A hackathon | Schedule (`submissions_open`, `submissions_close`, optional `judging_close`), voting window and mode, votes per voter, review target, team size cap, `results_published_at`, `rubric_locked_at` with the method registered then (`method_json`, `method_hash`), and `archived_at`. CHECK constraints keep windows ordered; publication, the rubric lock and the registered method can each be set once and never changed (rule triggers). |
| `tracks`, `prizes` | Event structure | Prizes can belong to a track or be overall. |
| `event_roles` | Organizer or judge of one event | Participants are not listed here: being on a team is what makes someone a participant. Admins act as organizers everywhere. |
| `judge_tracks` | Which tracks a judge covers | No rows means any track. Drives auto-assignment. |
| `invitations` | A single-use link to become judge or organizer | Token stored hashed. Bound to one email. |
| `teams`, `team_members` | A team and its people | `team_members` repeats `event_id` so that `UNIQUE (event_id, user_id)` can enforce one team per person per event in the database. Team names are not unique (the fixtures have three different teams called StillTrail); new teams made in Plumb are refused a taken name by the application. |
| `projects` | A submission | `status` is `draft` or `submitted` (CHECK: submitted rows have `submitted_at`, and a rule refuses a `submitted_at` past the deadline or the team's extension). `duplicate_of` points at the entry this one duplicates. A partial unique index allows only one non-duplicate project per team. `disqualified_reason` removes it from gallery, ballot and ranking without deleting anything. A *live entry* is submitted, not a duplicate and not disqualified (`domain.live_entry`, the one definition every query uses); only live entries are ranked, voted on and given certificates. |
| `team_extensions` | One team's later deadline | With the organizers' reason; ends before `judging_close`; read by the submission rule. |
| `criteria` | The rubric | Per event: key, name, weight, min and max. Weights change until the rubric is locked or results are published; criteria can be added or removed only before the first review. Both are rules in the database too. |
| `assignments` | Judge J should review project P | Primary key `(judge, project)`. `source` records whether it came from an import, the auto-assigner or an organizer. |
| `reviews`, `review_scores` | A judge's review and its per-criterion values | One review per `(judge, project)`, and a composite foreign key to `assignments`, so the database refuses a review nobody assigned. Values live in their own table so the rubric can differ between events without schema changes; a rule refuses a value outside its criterion's range, and another freezes both tables once results are published. Reviews of a duplicate, a disqualified entry or a withdrawn draft stay in the fit (they measure their judges' lean) but are not ranked. |
| `review_drafts` | A judge's unfinished review | Never read by the results. Deleted when the review is submitted; cannot be saved over a submitted review. |
| `comparisons` | A judge's pick between two assigned projects (pairwise mode) | Primary key `(judge, project_a, project_b)` with `project_a < project_b`: one answer per pair per judge. The winner must be one of the two (CHECK). |
| `awards` | Who won which prize | Written once, at publication, in the same transaction that signs the results; the signed record carries the same list, with each close call's reason. Rules refuse any later insert, update or delete. |
| `votes` | A community vote | Primary key `(voter, project)`: one vote per project per voter. The per-voter limit is enforced in a transaction; a rule refuses a vote dated outside the voting window, and any change after publication. `nonce` is random; the audit log records only `sha256(nonce:project)`, with no voter, and the public vote log lists seals, nonces and projects after the close. The voter is kept here for abuse review and voiding. |
| `comments` | A comment on a project | Hidden, never deleted, by organizers, with a reason. |
| `audit_log` | One state change | See below. |
| `records` | A signed statement | The exact signed canonical JSON, its signature and key id. Rules refuse any update or delete. |
| `receipts` | A numbered, signed receipt for a submitted review or entry | One per distinct content: opening a receipt again returns the same one, a change gets the next number. Numbered in order and never changed (rules). |
| `review_salts` | A per-judge salt | Keeps a judge's record digest from revealing a single review's scores by brute force. |
| `meta` | Settings the database carries | The schema version, the keyed tag secret for audit addresses, the audit checkpoint, demo markers. |
| `webhooks`, `webhook_deliveries` | Outbound event stream | `cursor` is the last audit sequence number delivered. |

## The audit log

Every change an organizer might later have to explain appends a row to `audit_log` in the same transaction as the change: event and deadline edits (with old and new values), team formation, submissions and edits, invitations, assignments, every review and review edit (with before and after values), rubric changes, duplicates and disqualifications, votes and voided votes, hidden comments, publication, webhook changes, logins.

Each row's hash is SHA-256 of the canonical JSON (sorted keys, no spaces, UTF-8) of these fields, where `detail` is the canonical JSON string stored in the row, `prev_hash` is the previous row's hash, and the first row's `prev_hash` is 64 zeros:

```
v1 (rows written before v1.1): {seq, at, event_id, actor_id, action, subject, detail, ip, prev_hash}
v2 (since v1.1, column v = 2): {seq, at, event_id, actor_id, action, subject, detail, ip_tag, prev_hash, event_prev, v}
```

`event_prev` is the hash of the previous row of the same event, and `ip_tag` is an HMAC of the client address under a key kept in `meta`, so a receiver can recompute every hash without ever being sent an address.
That makes one event's slice of the log, which is what a webhook receiver gets, checkable on its own: each delivered entry carries every field its hash covers, and names the one before it.
Editing or deleting any past row breaks every hash after it.
Two triggers make the table append-only (`UPDATE` and `DELETE` abort), and a rule refuses a new row that does not link to the last one, in the log and in its event.
`python -m app.cli verify-audit` recomputes the whole chain; the organizer pages verify from a stored checkpoint, so they stay fast as the log grows.
Published results records carry the chain head, which pins history up to publication under the portal's signature.

## Getting data in

* **Fixture-shaped import.** `POST /api/v1/events/import` (or Organizer, Import an event) accepts the DOGFOOD `fixtures.json` shape: `event`, `tracks`, `judges`, `teams`, `projects`, `scores`. The demo seed is this importer fed `fixtures.json`, twice: once as the live fixture event, and once as the published replay.
  * The whole file is validated before anything is written, field by field with the same cleaners as the forms (`app/fixture_schema.py`: lengths, control characters, http or https links, well-formed emails and times), then for references (unknown teams, tracks, judges or projects, repeated judge-project pairs, scores without criteria or outside their range), and imported in one transaction: all or nothing.
  * Ids that are already taken in this database (importing a copy, or two files that both say `trk_01`) get fresh ids, and every reference in the file follows them.
  * Judges and team members become accounts without passwords; an organizer of every event they belong to gives them a claim link (or an invitation) to set one.
  * Each score becomes an assignment plus a review. Criteria come from the score keys (weight 1, 1 to 5, widened if the data goes outside it).
  * A second project by the same team becomes a duplicate of the first (unless a Plumb export says which one the organizer kept); teams sharing a name are reported.
  * Every date is validated before anything is written.
* **Accounts, teams and projects** arrive through the UI or the API like anything else.

## Getting data out

* `GET /events/{id}/export/event.json` (organizer): the same fixture shape, plus a `plumb` block with everything Plumb-specific (schedule, voting settings, pairwise setting, rubric with weights, prizes, assignments, comparisons, duplicate decisions, audit head). Projects are exported in full, drafts and disqualifications included, so a reviewed entry that went back to draft still round-trips with its reviews. `tests/test_transfer.py` exports, re-imports as a copy and compares every field.
* `GET /events/{id}/export/scores.csv` (organizer): one row per review with every criterion, the weighted 0-100 score, eligibility, comment and timestamps.
* `GET /events/{id}/export/results.csv` (organizer, or anyone after publication): rank, raw mean, adjusted score, 90% interval, `P(beats next)`.
* `GET /records/{id}.json`: any signed record as a portable envelope; `GET /receipts/{number}.json` a receipt, for its owner.
* `GET /events/{id}/method.json`: the judging method registered when the rubric was locked, byte for byte as hashed.
* The SQLite file itself, consistently, while the portal runs: `python -m app.cli backup PATH` writes a copy and verifies it (integrity check and the whole audit chain). Keep `/data/signing-key.pem` (and `retired-keys.json`, once the key has been rotated) with it: records verify only with the key that signed them. `python -m app.cli restore PATH` puts a copy back; [OPERATIONS.md](OPERATIONS.md#restore) has the steps.

## Migrations

`schema.sql` always describes the current shape and is idempotent (`CREATE ... IF NOT EXISTS`), so a new database needs nothing else.
An existing database stores its version in `meta.schema_version`; on boot, `db.init_schema` runs every numbered migration in `db.MIGRATIONS` above that version, each in its own transaction, then applies `schema.sql` for anything new (whole new tables need no migration).
The current version is 7: version 2 added the `pairwise` setting to events, 3 the vote `nonce`, 4 `events.rubric_locked_at`, 5 token expiry and the v2 audit columns (tokens made before it get 90 days), 6 `events.archived_at`, 7 the registered method.
Before the first migration touches a database, boot copies it to `plumb.db.before-schema-<new>-from-<old>.bak`; a database whose version is newer than the code is refused instead of guessed at.
`tests/test_upgrade.py` upgrades a real v1.0 database and checks that its audit chain and every signed record still verify.

## Rules in the database

Each rule is a `rule_` trigger in `schema.sql` whose refusal starts with `plumb:`; the app answers it as a 409 with the message.
At every boot `db.check_rules` compares each trigger the database holds with the text `schema.sql` defines: a missing one is recreated, a changed one stops the portal from starting (and `doctor` names it), because a weakened rule is exactly what someone with database access would leave behind.
`tests/test_rules.py` breaks every rule in raw SQL, with no application code in the way.
