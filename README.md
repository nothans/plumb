# Plumb

A hackathon submission and judging portal whose results you can check.

Plumb runs the whole event: teams, submissions, a deadline that holds, judge assignment, scoring, a community vote, winners and certificates.
It is built around one idea: when a ranking decides who gets the prize, everyone involved should be able to see how much to trust it.
So judge bias is corrected with a model tested against a planted truth on the DOGFOOD fixtures' own judging design, every ranking comes with honest uncertainty (and says nothing it cannot back), every action lands in a tamper-evident log, and published results, judge records, certificates and submission receipts are signed so anyone can verify them offline.

Built for [DOGFOOD 2026](https://dogfoodhack.com) ("build the platform that will judge you").
MIT licensed.

**After the contest.** DOGFOOD 2026 judged commit [`5c36b75`](https://github.com/nothans/plumb/tree/5c36b7538816150e2cba038f1cc61382513cf7d7), which placed 19th of 194 (top 10%).
Everything since is v1.1, built after the results from what we learned studying the top entries.
[AFTER-DOGFOOD.md](AFTER-DOGFOOD.md) says what we learned and what changed.

**[Watch the 3-minute demo](https://nothans.github.io/plumb/)**: a finished event, an organizer mid-judging, the integrity checks, a judge, and a participant.

## Verified, not just claimed

<!-- tiers:start -->
| Tiers | Checked by | Result |
|---|---|---|
| T1, T2 | `tools/acceptance_run.py`, the published `run.py` byte for byte | [7 of 7 pass](acceptance-report.txt) |
| T3, T4 | [`tools/acceptance_extended.py`](tools/acceptance_extended.py): the same kind of checker, for the tiers `run.py` has no checks for | [41 of 41 pass](acceptance-report-extended.txt); every verdict lists the HTTP requests it rests on |
| All of it | 233 tests, and CI running both checkers against a real `docker compose up` on every push, and again with the network cut off | [`tests/`](tests), [`ci.yml`](.github/workflows/ci.yml) |
<!-- tiers:end -->

The extended checker was written by us, so it is built to be checked rather than trusted: one standard-library file you can read in ten minutes, talking to the portal only over HTTP, and relying on nothing the server says about itself (it verifies signatures with its own Ed25519, receives webhooks on its own listener, and recomputes vote seals).
The [table below](#every-t3-and-t4-bullet-and-the-checks-that-cover-it) maps every T3 and T4 bullet from the spec to its checks.

## Run it

```
docker compose up
```

Then open http://localhost:8080.
It needs no network at runtime: no CDN, no external API, no hosted database (CI runs it, and both checkers, in a container with no network at all). The first build downloads the Python base image and packages once, like any Docker build.
On boot it loads the DOGFOOD `fixtures.json` and prints ready-made logins:

```
seeded. test logins:
  organizer    Cookie: plumb_session=demo-organizer-3c1f9a7e   (organizer@example.org)
  judge_a      Cookie: plumb_session=demo-judge-a-8b27d4c0   (diego.herrera@example.org)
  judge_b      Cookie: plumb_session=demo-judge-b-51e0f6a3   (jonas.vogel@example.org)
  participant  Cookie: plumb_session=demo-participant-e94a2b18   (priya1@example.org)
  password for every demo account above, and admin@example.org: plumb-demo-2026
```

Log in with any of those emails and the password `plumb-demo-2026`.
Three events are seeded, so every stage of an event can be seen on first boot:

* **Sample Hack 2026**: the fixture event, mid-judging. Submissions closed on 2026-03-01, the 126 fixture reviews are loaded (the duplicate entry is not ranked, but its 5 reviews still show how its judges lean, so all 126 are in the fit), 8 projects are below the review target, a community vote is open for 14 days from boot, and pairwise judging is on. This is the event the DOGFOOD checker tests.
* **Sample Hack 2026 (published replay)**: the same fixture data run to the end. Prizes awarded, a closed community vote with a planted cluster of sock-puppet accounts (two already voided, three waiting for a decision), a hidden spam comment, and signed results, judge records and certificates.
* **Plumb Demo Jam**: empty, with submissions open for 7 days, to try the participant side from scratch.

**Offline install.** The running portal needs no network. The first build downloads the Python base image and packages once; for a machine that has never had them, build or download the image elsewhere and carry it over:

```
docker compose build && docker save plumb:local | gzip > plumb-image.tar.gz   # on a connected machine
docker load -i plumb-image.tar.gz && docker compose up --no-build          # on the offline one
```

The [latest release](https://github.com/nothans/plumb/releases/latest) attaches that image file, built from the tagged commit, so the first step can be a download.

Without Docker: `pip install -r requirements.txt`, then `PLUMB_DEMO=1 python -m app.cli bootstrap` and `uvicorn app.main:get_app --factory --port 8080`.

## A five-minute tour

1. **The end first.** Open [Sample Hack 2026 (published replay)](http://localhost:8080/events/evt_replay/results): winners, how sure the model is about each, the ranking with 90% intervals, and why each project moved ("its judges scored 1.2 points high"). On this data the honest answer is that every prize is a close call, so each winner carries the organizers' signed reason, and the page shows it. The method was registered before the ranking existed; the page says the results came from exactly that method. Open a certificate from *All records*, download its envelope, and check it at [/verify](http://localhost:8080/verify), or offline with `tools/verify_record.py`. Change one character and it fails.
2. **As the organizer** (organizer@example.org), open *Sample Hack 2026*, then *Manage*:
   * *Progress* says what needs you now: 8 projects below three reviews (one button fixes it) and the judge who gave every project the same score.
   * *Results* shows the live ranking, track leaders with how sure each lead is, and a Bradley-Terry cross-check. On the fixture data the honest answer is that neighbouring ranks are close to coin flips, and the page says so. *Publish* is a deliberate second step, and it is refused while the community vote is open.
   * *Integrity* verifies the audit chain and resolves the duplicate submission (Dry Harbour, twice). On the replay event it also shows the sock puppets.
   * *Audit log* shows every change with who, when, and before and after values. *Integrations* has webhooks and the embed snippet.
3. **As a judge** (diego.herrera@example.org), open *Your review queue*: only your own reviews exist for you. Save a review as a draft and come back to it from another browser; submit it and keep the numbered, signed receipt. *Compare pairs* asks which of two projects is better. Try `GET /api/judge/scores?judge=jdg_26` with judge_a's cookie: 403.
4. **As a participant**, open *Plumb Demo Jam*: create a team, copy the invite link, save a draft, submit (and get a signed receipt), edit. After the deadline nothing changes, unless the organizers give your team an extension, with a reason they sign into the results. On the replay event, a team member can open *Why it ranked where it did*: every review of their entry, judges anonymous, with each judge's lean removed.

## What is claimed, and what is verified

`.dogfood.toml` claims **T1, T2, T3 and T4**, and every tier is checked by a program, not by this README:

* **T1 and T2**: the DOGFOOD checker, `tools/acceptance_run.py` (the published `run.py`, byte for byte). The committed [`acceptance-report.txt`](acceptance-report.txt) is the run behind the table above. It has no checks for T3 and T4, so it lists them as claimed but not verified.
* **T3 and T4**: [`tools/acceptance_extended.py`](tools/acceptance_extended.py), written the same way: one standard-library Python file that makes plain HTTP requests to the running portal. It imports its own scratch copy of the fixtures through the bulk import API, runs a real community vote with throwaway voters from open to closed, receives the webhooks on a listener of its own, recomputes every vote seal, and verifies every signed record with a pure-Python Ed25519, so it relies on nothing the server says about itself. The committed [`acceptance-report-extended.txt`](acceptance-report-extended.txt) is the run behind the table above, each verdict with its requests. Run it yourself: `python3 tools/acceptance_extended.py .dogfood.toml --evidence` (about 45 seconds).

CI runs both checkers against a real `docker compose up` on every push, and again in a container with the network cut off.

**T1 core** (verified by the checker).
Login and signup, roles (visitor, participant, judge, organizer, admin), events with configurable dates, tracks and prizes, teams by invite link, draft-and-edit submissions, a deadline enforced in the backend and again by the database itself (with per-team extensions that need a reason), a public gallery with search and filter.

**Winners.** The fixture scores carry no winners, so Plumb decides them: an overall ranking and a ranking within each track, each with calibrated confidence, and at publication the organizer confirms a winner per prize (overall prizes down the overall ranking, track prizes to the track leader), all signed into the results record. A prize the reviews cannot settle (under 60% sure, or too few reviews to say) is a close call: it needs the organizers' written reason, signed beside the award, and one button gives the projects it hangs on another review ([JUDGING.md section 9](JUDGING.md#9-winners)).

**T2 judging** (verified by the checker).
Judge invitation links, and claim links for judges imported from last year; track-aware, conflict-aware, coverage-first assignment, with idle judges' work released and reassigned; an organizer-weighted rubric that is locked, with the whole method hashed and published, before the ranking exists; server-side review drafts and signed receipts; backend role isolation, declared route by route and checked at boot; a live progress dashboard; documented cross-judge normalization with a proof ([JUDGING.md](JUDGING.md)); CSV export.

**T3 public** (verified by the extended checker).
Community voting (organizers choose: any account, or participants only); comments with moderation; results hidden during voting in a way that holds against organizers too: counts are hidden from everyone, votes enter the audit log only as seals until the vote closes (then the nonces are revealed so anyone can check the tally), an open vote cannot be shortened, and judged results cannot be published until it closes; a ballot shuffled per voter; and an answer to cheating: one vote per project, a fixed number per voter, no voting for your own team, rate limits, abuse signals (shared addresses, accounts created mid-vote, projects drawing their votes), void-with-reason, and the audit trail ([THREAT-MODEL.md](THREAT-MODEL.md)).

**T4 stretch** (verified by the extended checker; see it in the replay event).
A JSON API for every UI action with an OpenAPI document ([`/api`](http://localhost:8080/api), [`docs/openapi.json`](docs/openapi.json)); webhooks that stream the audit log with HMAC signatures, managed from the *Integrations* tab; certificates; signed, publicly verifiable judge participation records; an embeddable gallery widget with a copyable snippet; bulk import and export in the fixture format.

### Every T3 and T4 bullet, and the checks that cover it

Check names as they appear in [`acceptance-report-extended.txt`](acceptance-report-extended.txt).

| Spec bullet | Checks |
|---|---|
| T3: community voting (email gated, link based or authenticated) | organizer opens a community vote; voters can sign up; a vote counts once per project; votes per voter are capped; no voting for your own team; judges cannot vote; anonymous visitors cannot vote |
| T3: project comments | comments are posted and public; organizers can hide a comment |
| T3: results hidden during the voting window | vote counts hidden from organizers; audit log does not reveal votes; abuse signals do not name projects; an open vote cannot be cut short; results unpublishable during voting; counts appear when voting closes; revealed votes match the sealed log; a closed vote cannot reopen |
| T3: randomized project ordering on ballots | ballot order is random per voter; ballot order is stable for a voter |
| T3: anti-abuse (rate limits, duplicate detection, audit trail) | vote flooding is rate limited; duplicate submissions are detected; new accounts voting are flagged; organizers void votes with a reason; audit trail records the moderation |
| T4: REST API and webhooks | OpenAPI covers the UI's actions; API tokens work and can be revoked; webhook registered; webhooks deliver every action; webhook deliveries are signed; webhook deliveries arrive in order; webhook entries verify and chain within the event |
| T4: certificate and record generation | teams get certificates; results publish once voting closed |
| T4: signed, publicly verifiable judge participation records | every record's Ed25519 signature verifies; a tampered record fails verification; judge record matches the judge's own scores |
| T4: embeddable gallery widget | gallery widget can be framed; other pages refuse framing |
| T4: bulk import and export | bulk import creates an event; export then import round-trips; scratch events archive out of the listings |

**Bonus challenges.**
Normalization proof ([JUDGING.md section 5](JUDGING.md#5-the-normalization-proof), [`docs/normalization-proof.md`](docs/normalization-proof.md), `tools/normalization_proof.py`, checked by `tests/test_proof.py`).
Pairwise mode (Bradley-Terry with active pair selection and a skip for pairs too close to call, [JUDGING.md section 8](JUDGING.md#8-pairwise-mode-bradley-terry)).
Threat model ([THREAT-MODEL.md](THREAT-MODEL.md)).
API first ([`docs/openapi.json`](docs/openapi.json), kept current by `tests/test_openapi.py`).

## The API in one minute

Make a token on your page (`/me`), then:

```
T=plb_...                                   # your token
curl -H "Authorization: Bearer $T" localhost:8080/api/v1/events
curl -H "Authorization: Bearer $T" localhost:8080/api/v1/events/evt_01/queue            # as a judge
curl -X PUT -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
     -d '{"values": {"functionality": 4, "quality": 3, "innovation": 5}, "comment": "Solid."}' \
     localhost:8080/api/v1/projects/prj_06/review
curl -H "Authorization: Bearer $T" localhost:8080/api/v1/events/evt_01/results            # as an organizer
curl localhost:8080/records/<record id>.json > envelope.json
python tools/verify_record.py envelope.json --key "$(curl -s localhost:8080/.well-known/plumb-key.json | python -c 'import json,sys; print(json.load(sys.stdin)["keys"][0]["x"])')"
```

## Run a real event

Demo mode publishes fixed logins (they are in `.dogfood.toml`), and every page says so in a banner.
A real event runs from its own compose file and volume, so nothing about the demo can reach it.
[OPERATIONS.md](OPERATIONS.md) is the day-by-day runbook; the short version:

1. Copy `.env.example` to `.env` and fill it in: the public address (`PLUMB_BASE_URL`, signed into every record), the first admin (a password of 15 or more characters), and your reverse proxy's address or network in `FORWARDED_ALLOW_IPS` (like `172.16.0.0/12` for a proxy container).
2. `docker compose -f compose.prod.yml up -d`, behind a TLS reverse proxy (Caddy, nginx, Traefik) that passes the `Host` header. Plumb refuses to start with settings that contradict each other (https without secure cookies, a wildcard or malformed proxy address), and demo mode refuses to seed any database that holds data. A volume that ran a real event under demo mode keeps its data with `python -m app.cli retire-demo`, which ends every public login.
3. `docker compose -f compose.prod.yml exec plumb python -m app.cli doctor` says what to fix before the event: settings, the database, the rule triggers, every signed record, the key.
4. Back up on a schedule with `python -m app.cli backup /data/backups/plumb.db` (the copy is verified as it is made), then copy it off the host together with `/data/signing-key.pem` (and `/data/retired-keys.json`, once the key has been rotated): records verify only with the key that signed them. A restore puts a copy back with the portal stopped (`docker compose -f compose.prod.yml run --rm plumb python -m app.cli restore PATH`; [OPERATIONS.md](OPERATIONS.md#restore) has the four lines); it checks the copy first and keeps the database it replaces.
5. Upgrades back the database up on their own before migrating, and refuse a database from a newer Plumb.
6. If a venue puts everyone behind one address, raise the per-address limits with `PLUMB_RATE_LIMITS` (in `.env`).
7. Import last year's event from *Import an event* on the home page; its judges get claim links from the organizer, so their accounts carry over.
8. When judging opens, lock the rubric (*Manage*, *Rubric*). That also registers the judging method and publishes its hash, so nobody can reweight or retune after seeing the live ranking.

## Honest limitations

* Plumb sends no email. Invitations and claim links are links the organizer copies and sends. People change their own password on their page; a forgotten one is reset by an operator with `python -m app.cli set-password <email>`.
* "Anyone with an account" voting cannot stop determined sock puppets, because accounts are not tied to verified addresses. Use "participants only" for votes with prizes, and review the signals on the Integrity page.
* The fixture data's project summaries are the contest's placeholder text ("One line of what it does."); Plumb shows the data as given.
* The normalization model is additive; it does not model judges who stretch or squash their scale (JUDGING.md explains why, and the proof shows it still ranks best when they do).
* On small events the probabilities would run high, so below 20 reviews beyond one per project Plumb states none; on events where projects are close together, the rare 75 to 90% "beats next" still runs high ([JUDGING.md section 12](JUDGING.md#12-limitations)).
* One instance per database file. SQLite is plenty for a hackathon, not for a multi-tenant service.
* Rate limits are in memory and reset on restart; `PLUMB_RATE_LIMITS` sets them.

## Documents

* [ARCHITECTURE.md](ARCHITECTURE.md): how it is put together, and why.
* [DATA-MODEL.md](DATA-MODEL.md): the schema, the audit chain, migrations, import and export.
* [JUDGING.md](JUDGING.md): assignment, scoring math, normalization, the proof, pairwise mode, winners.
* [THREAT-MODEL.md](THREAT-MODEL.md): who might cheat, how, and what stops them.
* [OPERATIONS.md](OPERATIONS.md): running a real event, from a month out to after the results.
* [AFTER-DOGFOOD.md](AFTER-DOGFOOD.md): what the contest taught us, and what v1.1 changed because of it.

## Tests

```
pip install -r requirements-dev.txt
python -m pytest -q                                    # the whole suite (the count is in the table at the top)
ruff check app tests tools                             # lint
python tools/readme_tiers.py --check                   # the table at the top matches the reports and the suite
python tools/acceptance_run.py .dogfood.toml           # the DOGFOOD checker, against a running portal
python tools/normalization_proof.py --draws 1000       # the proof, about 30 seconds
```
