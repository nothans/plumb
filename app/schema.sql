-- Plumb schema. SQLite, one file, foreign keys on.
-- Ids are short prefixed strings (evt_, usr_, prj_ ...) so that ids from an
-- imported fixture file survive the round trip unchanged.
-- Timestamps are ISO 8601 UTC strings ("2026-03-01T18:00:00Z"), which sort
-- correctly as text.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name          TEXT NOT NULL,
    password_hash TEXT,                       -- NULL until the account is claimed
    is_admin      INTEGER NOT NULL DEFAULT 0 CHECK (is_admin IN (0, 1)),
    created_at    TEXT NOT NULL,
    created_ip    TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,              -- sha256 of the cookie value, never the value
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS events (
    id                   TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    tagline              TEXT NOT NULL DEFAULT '',
    description          TEXT NOT NULL DEFAULT '',
    submissions_open     TEXT NOT NULL,
    submissions_close    TEXT NOT NULL,
    judging_close        TEXT,
    voting_mode          TEXT NOT NULL DEFAULT 'off'
                         CHECK (voting_mode IN ('off', 'accounts', 'participants')),
    voting_open          TEXT,
    voting_close         TEXT,
    votes_per_voter      INTEGER NOT NULL DEFAULT 3 CHECK (votes_per_voter BETWEEN 1 AND 50),
    reviews_per_project  INTEGER NOT NULL DEFAULT 3 CHECK (reviews_per_project BETWEEN 1 AND 20),
    max_team_size        INTEGER NOT NULL DEFAULT 4 CHECK (max_team_size BETWEEN 1 AND 50),
    pairwise             INTEGER NOT NULL DEFAULT 0 CHECK (pairwise IN (0, 1)),
    rubric_locked_at     TEXT,
    results_published_at TEXT,
    archived_at          TEXT,                -- hidden from listings and the gallery; still reachable by link
    method_json          TEXT,                -- the judging method registered when the rubric was locked (app/method.py)
    method_hash          TEXT,                -- its sha256, published on the event page
    created_at           TEXT NOT NULL,
    CHECK (submissions_open < submissions_close),
    CHECK (voting_open IS NULL OR voting_close IS NULL OR voting_open < voting_close)
);

CREATE TABLE IF NOT EXISTS tracks (
    id       TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    name     TEXT NOT NULL,
    UNIQUE (event_id, name)
);

CREATE TABLE IF NOT EXISTS prizes (
    id          TEXT PRIMARY KEY,
    event_id    TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    track_id    TEXT REFERENCES tracks(id) ON DELETE SET NULL,  -- NULL = overall
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    position    INTEGER NOT NULL DEFAULT 0
);

-- Per-event roles. Participants are not listed here: being on a team in the
-- event is what makes someone a participant.
CREATE TABLE IF NOT EXISTS event_roles (
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    user_id  TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role     TEXT NOT NULL CHECK (role IN ('organizer', 'judge')),
    PRIMARY KEY (event_id, user_id, role)
);

-- Tracks a judge is qualified for. No rows = any track.
CREATE TABLE IF NOT EXISTS judge_tracks (
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    user_id  TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    track_id TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    PRIMARY KEY (event_id, user_id, track_id)
);

CREATE TABLE IF NOT EXISTS invitations (
    token_hash  TEXT PRIMARY KEY,
    event_id    TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    email       TEXT NOT NULL COLLATE NOCASE,
    role        TEXT NOT NULL CHECK (role IN ('organizer', 'judge')),
    created_by  TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at  TEXT NOT NULL,
    accepted_at TEXT,
    accepted_by TEXT REFERENCES users(id) ON DELETE SET NULL
);

-- A single-use link that lets the owner of an address set the password of
-- an account an import created without one (a judge or team member from
-- last year's event). Issued by an organizer who runs every event the
-- account belongs to, so nobody can take over someone else's judge.
CREATE TABLE IF NOT EXISTS claim_links (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_by TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    used_at    TEXT
);

CREATE TABLE IF NOT EXISTS teams (
    id          TEXT PRIMARY KEY,
    event_id    TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    invite_code TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL
    -- Names are not unique: imported events can have two different teams
    -- called the same thing (the fixtures do). New teams made in Plumb are
    -- still refused a taken name, in domain.create_team.
);
CREATE INDEX IF NOT EXISTS teams_event ON teams(event_id);

-- event_id is repeated here so the database itself can enforce
-- "one team per person per event".
CREATE TABLE IF NOT EXISTS team_members (
    team_id   TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    event_id  TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    user_id   TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    joined_at TEXT NOT NULL,
    PRIMARY KEY (team_id, user_id),
    UNIQUE (event_id, user_id)
);

CREATE TABLE IF NOT EXISTS projects (
    id           TEXT PRIMARY KEY,
    event_id     TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    team_id      TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    track_id     TEXT REFERENCES tracks(id) ON DELETE SET NULL,
    title        TEXT NOT NULL,
    summary      TEXT NOT NULL DEFAULT '',
    description  TEXT NOT NULL DEFAULT '',
    repo_url     TEXT NOT NULL DEFAULT '',
    demo_url     TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'submitted')),
    duplicate_of TEXT REFERENCES projects(id) ON DELETE SET NULL,
    disqualified_reason TEXT,
    submitted_at TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    CHECK (status = 'draft' OR submitted_at IS NOT NULL),
    CHECK (duplicate_of IS NULL OR duplicate_of <> id)
);
CREATE INDEX IF NOT EXISTS projects_event ON projects(event_id, status);
-- A team has one live entry per event. Imported duplicates are kept for the
-- record but must point at the entry they duplicate.
CREATE UNIQUE INDEX IF NOT EXISTS projects_one_per_team
    ON projects(team_id) WHERE duplicate_of IS NULL;

CREATE TABLE IF NOT EXISTS criteria (
    id          TEXT PRIMARY KEY,
    event_id    TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    key         TEXT NOT NULL,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    weight      REAL NOT NULL DEFAULT 1 CHECK (weight >= 0),
    min_value   INTEGER NOT NULL DEFAULT 1,
    max_value   INTEGER NOT NULL DEFAULT 5,
    position    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (event_id, key),
    CHECK (min_value < max_value)
);

CREATE TABLE IF NOT EXISTS assignments (
    event_id   TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    judge_id   TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source     TEXT NOT NULL CHECK (source IN ('import', 'auto', 'manual')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (judge_id, project_id)
);
CREATE INDEX IF NOT EXISTS assignments_project ON assignments(project_id);

-- One review per judge per project; the per-criterion values live in
-- review_scores so that the rubric can change shape without a migration.
CREATE TABLE IF NOT EXISTS reviews (
    id           TEXT PRIMARY KEY,
    event_id     TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    judge_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    comment      TEXT NOT NULL DEFAULT '',
    submitted_at TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE (judge_id, project_id),
    FOREIGN KEY (judge_id, project_id) REFERENCES assignments(judge_id, project_id)
);

CREATE TABLE IF NOT EXISTS review_scores (
    review_id    TEXT NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    criterion_id TEXT NOT NULL REFERENCES criteria(id) ON DELETE CASCADE,
    value        INTEGER NOT NULL,
    PRIMARY KEY (review_id, criterion_id)
);

-- Pairwise judging: a judge picked the better of two assigned projects.
-- project_a < project_b so each pair has one row per judge.
CREATE TABLE IF NOT EXISTS comparisons (
    event_id   TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    judge_id   TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_a  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    project_b  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    winner     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (judge_id, project_a, project_b),
    CHECK (project_a < project_b),
    CHECK (winner IN (project_a, project_b))
);
CREATE INDEX IF NOT EXISTS comparisons_event ON comparisons(event_id);

CREATE TABLE IF NOT EXISTS votes (
    event_id   TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    voter_id   TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    ip         TEXT,
    nonce      TEXT,        -- random; the audit log holds sha256(nonce:project) until voting closes
    PRIMARY KEY (voter_id, project_id)
);
CREATE INDEX IF NOT EXISTS votes_event ON votes(event_id);

CREATE TABLE IF NOT EXISTS comments (
    id         TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    body       TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 2000),
    created_at TEXT NOT NULL,
    hidden_at  TEXT,
    hidden_by  TEXT REFERENCES users(id) ON DELETE SET NULL,
    hidden_reason TEXT
);
CREATE INDEX IF NOT EXISTS comments_project ON comments(project_id, created_at);

-- Append-only and hash-chained: each row's hash covers the previous row's
-- hash, so editing or deleting any past row breaks every hash after it.
-- Triggers refuse UPDATE and DELETE at the database level.
CREATE TABLE IF NOT EXISTS audit_log (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL,
    event_id  TEXT,
    actor_id  TEXT,
    action    TEXT NOT NULL,
    subject   TEXT NOT NULL DEFAULT '',
    detail    TEXT NOT NULL DEFAULT '{}',
    ip        TEXT,
    prev_hash TEXT NOT NULL,
    hash      TEXT NOT NULL UNIQUE,
    v          INTEGER NOT NULL DEFAULT 1,   -- hash version (audit.py); 2 since v1.1
    ip_tag     TEXT,                         -- v2: keyed tag of ip, which the hash covers instead of ip
    event_prev TEXT                          -- v2: hash of the previous row of the same event
);
CREATE INDEX IF NOT EXISTS audit_event ON audit_log(event_id, seq);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

-- Who won what. Written once, at publication, inside the same transaction
-- that signs the results; the signed record carries the same list.
CREATE TABLE IF NOT EXISTS awards (
    prize_id   TEXT PRIMARY KEY REFERENCES prizes(id) ON DELETE CASCADE,
    event_id   TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    awarded_at TEXT NOT NULL
);

-- Signed statements the portal has issued (judge participation, team
-- participation and placement, results snapshots). payload is the exact
-- canonical JSON that was signed.
CREATE TABLE IF NOT EXISTS records (
    id        TEXT PRIMARY KEY,
    event_id  TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    kind      TEXT NOT NULL CHECK (kind IN ('judge', 'team', 'results')),
    subject   TEXT NOT NULL,                  -- user id, team id, or event id
    payload   TEXT NOT NULL,
    signature TEXT NOT NULL,
    key_id    TEXT NOT NULL,
    issued_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS records_event ON records(event_id, kind);

-- A random salt per judge per event, mixed into the review digest of their
-- signed record. Without it, a judge with one review and no comment could
-- have their scores recovered from the public digest by trying every
-- combination. Only the judge (and the event's organizers) can read it.
CREATE TABLE IF NOT EXISTS review_salts (
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    judge_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    salt     TEXT NOT NULL,
    PRIMARY KEY (event_id, judge_id)
);

CREATE TABLE IF NOT EXISTS api_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    expires_at TEXT                         -- a token past this is refused; NULL is treated as expired
);

-- Webhooks stream the audit log: every audited action of the event is
-- POSTed, in order, signed with HMAC-SHA256 using the hook's secret.
-- cursor is the last audit seq delivered.
CREATE TABLE IF NOT EXISTS webhooks (
    id         TEXT PRIMARY KEY,
    event_id   TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    url        TEXT NOT NULL,
    secret     TEXT NOT NULL,
    cursor     INTEGER NOT NULL DEFAULT 0,
    created_by TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    webhook_id  TEXT NOT NULL REFERENCES webhooks(id) ON DELETE CASCADE,
    audit_seq   INTEGER NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('pending', 'delivered', 'failed')),
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    updated_at  TEXT NOT NULL,
    UNIQUE (webhook_id, audit_seq)
);

-- A deadline extension for one team, with the organizers' reason. The
-- submission rule below reads it, so a late entry is refused by the
-- database itself, not only by the form.
CREATE TABLE IF NOT EXISTS team_extensions (
    team_id    TEXT PRIMARY KEY REFERENCES teams(id) ON DELETE CASCADE,
    event_id   TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    until      TEXT NOT NULL,
    reason     TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 300),
    granted_by TEXT REFERENCES users(id) ON DELETE SET NULL,
    granted_at TEXT NOT NULL
);

-- The rules, kept by the database as well as by the code. Each refusal
-- starts with "plumb:" so the app can answer it as a 409 with the message;
-- nothing else from SQLite is shown to anyone. db.check_rules compares
-- every rule_ trigger here with what the database holds, at every boot.

-- A score stays inside its criterion's range.
CREATE TRIGGER IF NOT EXISTS rule_score_in_range BEFORE INSERT ON review_scores
WHEN NEW.value < (SELECT min_value FROM criteria WHERE id = NEW.criterion_id)
  OR NEW.value > (SELECT max_value FROM criteria WHERE id = NEW.criterion_id)
BEGIN SELECT RAISE(ABORT, 'plumb: a score is outside its criterion''s range'); END;
CREATE TRIGGER IF NOT EXISTS rule_score_in_range_update BEFORE UPDATE ON review_scores
WHEN NEW.value < (SELECT min_value FROM criteria WHERE id = NEW.criterion_id)
  OR NEW.value > (SELECT max_value FROM criteria WHERE id = NEW.criterion_id)
BEGIN SELECT RAISE(ABORT, 'plumb: a score is outside its criterion''s range'); END;

-- After publication, reviews and their scores are frozen.
CREATE TRIGGER IF NOT EXISTS rule_reviews_frozen_insert BEFORE INSERT ON reviews
WHEN (SELECT results_published_at FROM events WHERE id = NEW.event_id) IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'plumb: results are published; reviews are frozen'); END;
CREATE TRIGGER IF NOT EXISTS rule_reviews_frozen_update BEFORE UPDATE ON reviews
WHEN (SELECT results_published_at FROM events WHERE id = OLD.event_id) IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'plumb: results are published; reviews are frozen'); END;
CREATE TRIGGER IF NOT EXISTS rule_reviews_frozen_delete BEFORE DELETE ON reviews
WHEN (SELECT results_published_at FROM events WHERE id = OLD.event_id) IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'plumb: results are published; reviews are frozen'); END;
CREATE TRIGGER IF NOT EXISTS rule_scores_frozen_insert BEFORE INSERT ON review_scores
WHEN (SELECT e.results_published_at FROM reviews r JOIN events e ON e.id = r.event_id WHERE r.id = NEW.review_id) IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'plumb: results are published; reviews are frozen'); END;
CREATE TRIGGER IF NOT EXISTS rule_scores_frozen_update BEFORE UPDATE ON review_scores
WHEN (SELECT e.results_published_at FROM reviews r JOIN events e ON e.id = r.event_id WHERE r.id = OLD.review_id) IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'plumb: results are published; reviews are frozen'); END;
CREATE TRIGGER IF NOT EXISTS rule_scores_frozen_delete BEFORE DELETE ON review_scores
WHEN (SELECT e.results_published_at FROM reviews r JOIN events e ON e.id = r.event_id WHERE r.id = OLD.review_id) IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'plumb: results are published; reviews are frozen'); END;

-- The rubric: no new or removed criterion once anything is scored, and no
-- change at all once it is locked or the results are published.
CREATE TRIGGER IF NOT EXISTS rule_rubric_no_new_criterion BEFORE INSERT ON criteria
WHEN EXISTS (SELECT 1 FROM reviews WHERE event_id = NEW.event_id)
BEGIN SELECT RAISE(ABORT, 'plumb: reviews exist; criteria can be reweighted but not added'); END;
CREATE TRIGGER IF NOT EXISTS rule_rubric_no_removed_criterion BEFORE DELETE ON criteria
WHEN EXISTS (SELECT 1 FROM reviews WHERE event_id = OLD.event_id)
BEGIN SELECT RAISE(ABORT, 'plumb: reviews exist; criteria cannot be removed'); END;
CREATE TRIGGER IF NOT EXISTS rule_rubric_frozen BEFORE UPDATE ON criteria
WHEN (SELECT rubric_locked_at IS NOT NULL OR results_published_at IS NOT NULL FROM events WHERE id = OLD.event_id)
BEGIN SELECT RAISE(ABORT, 'plumb: the rubric is locked'); END;

-- Publication and the rubric lock happen once and stay.
CREATE TRIGGER IF NOT EXISTS rule_published_once BEFORE UPDATE OF results_published_at ON events
WHEN OLD.results_published_at IS NOT NULL AND NEW.results_published_at IS NOT OLD.results_published_at
BEGIN SELECT RAISE(ABORT, 'plumb: results were published; that cannot be undone'); END;
CREATE TRIGGER IF NOT EXISTS rule_method_registered_once BEFORE UPDATE OF method_json, method_hash ON events
WHEN OLD.method_hash IS NOT NULL AND (NEW.method_hash IS NOT OLD.method_hash OR NEW.method_json IS NOT OLD.method_json)
BEGIN SELECT RAISE(ABORT, 'plumb: the judging method was registered; it cannot change'); END;
CREATE TRIGGER IF NOT EXISTS rule_rubric_locked_once BEFORE UPDATE OF rubric_locked_at ON events
WHEN OLD.rubric_locked_at IS NOT NULL AND NEW.rubric_locked_at IS NOT OLD.rubric_locked_at
BEGIN SELECT RAISE(ABORT, 'plumb: the rubric was locked; that cannot be undone'); END;

-- Signed records and awards are written once.
CREATE TRIGGER IF NOT EXISTS rule_records_no_update BEFORE UPDATE ON records
BEGIN SELECT RAISE(ABORT, 'plumb: signed records cannot change'); END;
CREATE TRIGGER IF NOT EXISTS rule_records_no_delete BEFORE DELETE ON records
BEGIN SELECT RAISE(ABORT, 'plumb: signed records cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS rule_awards_once BEFORE INSERT ON awards
WHEN (SELECT results_published_at FROM events WHERE id = NEW.event_id) IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'plumb: results are published; awards are final'); END;
CREATE TRIGGER IF NOT EXISTS rule_awards_no_update BEFORE UPDATE ON awards
BEGIN SELECT RAISE(ABORT, 'plumb: awards are final'); END;
CREATE TRIGGER IF NOT EXISTS rule_awards_no_delete BEFORE DELETE ON awards
BEGIN SELECT RAISE(ABORT, 'plumb: awards are final'); END;

-- A vote is cast inside the voting window, and none is added or removed
-- after publication.
CREATE TRIGGER IF NOT EXISTS rule_vote_in_window BEFORE INSERT ON votes
WHEN (SELECT voting_open IS NULL OR voting_close IS NULL OR NEW.created_at < voting_open
             OR NEW.created_at >= voting_close OR results_published_at IS NOT NULL
      FROM events WHERE id = NEW.event_id)
BEGIN SELECT RAISE(ABORT, 'plumb: voting is not open'); END;
CREATE TRIGGER IF NOT EXISTS rule_votes_frozen BEFORE DELETE ON votes
WHEN (SELECT results_published_at FROM events WHERE id = OLD.event_id) IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'plumb: results are published; votes are final'); END;

-- An entry is submitted by the deadline, or by its team's extension.
CREATE TRIGGER IF NOT EXISTS rule_submitted_in_time BEFORE INSERT ON projects
WHEN NEW.status = 'submitted' AND NEW.submitted_at > (
    SELECT coalesce((SELECT until FROM team_extensions WHERE team_id = NEW.team_id), submissions_close)
    FROM events WHERE id = NEW.event_id)
BEGIN SELECT RAISE(ABORT, 'plumb: submissions are closed'); END;
CREATE TRIGGER IF NOT EXISTS rule_submitted_in_time_update BEFORE UPDATE OF status, submitted_at ON projects
WHEN NEW.status = 'submitted' AND NEW.submitted_at IS NOT OLD.submitted_at AND NEW.submitted_at > (
    SELECT coalesce((SELECT until FROM team_extensions WHERE team_id = NEW.team_id), submissions_close)
    FROM events WHERE id = NEW.event_id)
BEGIN SELECT RAISE(ABORT, 'plumb: submissions are closed'); END;

-- Each audit entry links to the one before it, in the whole log and in its
-- event (the hash itself is recomputed by audit.verify_chain).
CREATE TRIGGER IF NOT EXISTS rule_audit_linked BEFORE INSERT ON audit_log
WHEN NEW.seq IS NOT coalesce((SELECT max(seq) FROM audit_log), 0) + 1
  OR NEW.prev_hash IS NOT coalesce((SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1), printf('%064d', 0))
  OR (NEW.v >= 2 AND NEW.event_id IS NOT NULL AND NEW.event_prev IS NOT coalesce(
        (SELECT hash FROM audit_log WHERE event_id = NEW.event_id ORDER BY seq DESC LIMIT 1), printf('%064d', 0)))
BEGIN SELECT RAISE(ABORT, 'plumb: an audit entry must link to the one before it'); END;

-- A judge's unfinished review, kept on the server so it survives a closed
-- tab or another device. Drafts never reach the results: those read only
-- reviews. A draft cannot be saved over a submitted review.
CREATE TABLE IF NOT EXISTS review_drafts (
    judge_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    event_id    TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    values_json TEXT NOT NULL DEFAULT '{}',
    comment     TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (judge_id, project_id)
);

-- Numbered, signed receipts: one for each submitted review and each
-- submitted entry, for the exact content submitted (a later edit gets a new
-- receipt with a new number). payload is the canonical JSON that was signed.
CREATE TABLE IF NOT EXISTS receipts (
    number         INTEGER PRIMARY KEY,
    event_id       TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL CHECK (kind IN ('review', 'entry')),
    subject        TEXT NOT NULL,             -- the review id, or the project id
    owner_id       TEXT,                      -- the judge, for a review
    team_id        TEXT,                      -- the team, for an entry
    content_sha256 TEXT NOT NULL,
    payload        TEXT NOT NULL,
    signature      TEXT NOT NULL,
    key_id         TEXT NOT NULL,
    issued_at      TEXT NOT NULL,
    UNIQUE (kind, subject, content_sha256)
);
CREATE TRIGGER IF NOT EXISTS rule_receipts_no_update BEFORE UPDATE ON receipts
BEGIN SELECT RAISE(ABORT, 'plumb: receipts cannot change'); END;
CREATE TRIGGER IF NOT EXISTS rule_receipts_no_delete BEFORE DELETE ON receipts
BEGIN SELECT RAISE(ABORT, 'plumb: receipts cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS rule_receipts_numbered BEFORE INSERT ON receipts
WHEN NEW.number IS NOT coalesce((SELECT max(number) FROM receipts), 0) + 1
BEGIN SELECT RAISE(ABORT, 'plumb: receipts are numbered in order'); END;
