# Plumb

A hackathon submission and judging portal whose results you can check.

Plumb runs the whole event: teams, submissions, a deadline that holds, judge assignment, scoring, a community vote, winners and certificates.
It is built around one idea: when a ranking decides who gets the prize, everyone involved should be able to see how much to trust it.
So judge bias is corrected with a model that is proven on the DOGFOOD fixture data, every ranking comes with honest uncertainty, every action lands in a tamper-evident log, and published results, judge records and certificates are signed so anyone can verify them offline.

Built for [DOGFOOD 2026](https://dogfoodhack.com) ("build the platform that will judge you").
MIT licensed.

## Run it

```
docker compose up
```

Then open http://localhost:8080.
It needs no network at runtime: no CDN, no external API, no hosted database.
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

* **Sample Hack 2026**: the fixture event, mid-judging. Submissions closed on 2026-03-01, the 126 fixture reviews are loaded, 8 projects are below the review target, a community vote is open for 14 days from boot, and pairwise judging is on. This is the event the DOGFOOD checker tests.
* **Sample Hack 2026 (published replay)**: the same fixture data run to the end. Prizes awarded, a closed community vote with a planted cluster of sock-puppet accounts (two already voided, three waiting for a decision), a hidden spam comment, and signed results, judge records and certificates.
* **Plumb Demo Jam**: empty, with submissions open for 7 days, to try the participant side from scratch.

Without Docker: `pip install -r requirements.txt`, then `PLUMB_DEMO=1 python -m app.cli bootstrap` and `uvicorn app.main:get_app --factory --port 8080`.

## A five-minute tour

1. **The end first.** Open [Sample Hack 2026 (published replay)](http://localhost:8080/events/evt_replay/results): winners, how sure the model is about each, the ranking with 90% intervals, and why each project moved ("its judges scored 1.2 points high"). On this data the honest answer is that every prize is a statistical tie, and the page says so next to each winner. Open a certificate from *All records*, download its envelope, and check it at [/verify](http://localhost:8080/verify), or offline with `tools/verify_record.py`. Change one character and it fails.
2. **As the organizer** (organizer@example.org), open *Sample Hack 2026*, then *Manage*:
   * *Progress* says what needs you now: 8 projects below three reviews (one button fixes it) and the judge who gave every project the same score.
   * *Results* shows the live ranking, track leaders with how sure each lead is, and a Bradley-Terry cross-check. On the fixture data the honest answer is that neighbouring ranks are close to coin flips, and the page says so. *Publish* is a deliberate second step, and it is refused while the community vote is open.
   * *Integrity* verifies the audit chain and resolves the duplicate submission (Dry Harbour, twice). On the replay event it also shows the sock puppets.
   * *Audit log* shows every change with who, when, and before and after values. *Integrations* has webhooks and the embed snippet.
3. **As a judge** (diego.herrera@example.org), open *Your review queue*: only your own reviews exist for you. *Compare pairs* asks which of two projects is better. Try `GET /api/judge/scores?judge=jdg_26` with judge_a's cookie: 403.
4. **As a participant**, open *Plumb Demo Jam*: create a team, copy the invite link, save a draft, submit, edit. After the deadline nothing changes.

## What is claimed, and what is verified

`.dogfood.toml` claims **T1, T2, T3 and T4**, and every tier is checked by a program, not by this README:

* **T1 and T2**: the DOGFOOD checker, `tools/acceptance_run.py` (the published `run.py`, byte for byte). The committed [`acceptance-report.txt`](acceptance-report.txt) shows 7 of 7 passing. It has no checks for T3 and T4, so it lists them as claimed but not verified.
* **T3 and T4**: [`tools/acceptance_extended.py`](tools/acceptance_extended.py), written the same way: one standard-library Python file that makes plain HTTP requests to the running portal. It imports its own scratch copy of the fixtures through the bulk import API, runs a real community vote with throwaway voters from open to closed, receives the webhooks on a listener of its own, recomputes every vote seal, and verifies every signed record with a pure-Python Ed25519, so it relies on nothing the server says about itself. The committed [`acceptance-report-extended.txt`](acceptance-report-extended.txt) shows 38 of 38 passing. Run it yourself: `python3 tools/acceptance_extended.py .dogfood.toml` (about 45 seconds).

CI runs both checkers against a real `docker compose up` on every push.

**T1 core** (verified by the checker).
Login and signup, roles (visitor, participant, judge, organizer, admin), events with configurable dates, tracks and prizes, teams by invite link, draft-and-edit submissions, a deadline enforced in the backend, a public gallery with search and filter.

**T2 judging** (verified by the checker).
Judge invitation links; track-aware, conflict-aware, coverage-first assignment; an organizer-weighted rubric; backend role isolation; a live progress dashboard; documented cross-judge normalization with a proof ([JUDGING.md](JUDGING.md)); CSV export.

**T3 public** (verified by the extended checker).
Community voting (organizers choose: any account, or participants only); comments with moderation; results hidden during voting in a way that holds against organizers too: counts are hidden from everyone, votes enter the audit log only as seals until the vote closes (then the nonces are revealed so anyone can check the tally), an open vote cannot be shortened, and judged results cannot be published until it closes; a ballot shuffled per voter; and an answer to cheating: one vote per project, a fixed number per voter, no voting for your own team, rate limits, abuse signals (shared addresses, accounts created mid-vote, projects drawing their votes), void-with-reason, and the audit trail ([THREAT-MODEL.md](THREAT-MODEL.md)).

**T4 stretch** (verified by the extended checker; see it in the replay event).
A JSON API for every UI action with an OpenAPI document ([`/api`](http://localhost:8080/api), [`docs/openapi.json`](docs/openapi.json)); webhooks that stream the audit log with HMAC signatures, managed from the *Integrations* tab; certificates; signed, publicly verifiable judge participation records; an embeddable gallery widget with a copyable snippet; bulk import and export in the fixture format.

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
For a real event:

1. Start from an empty volume (`docker compose down -v`): Plumb refuses to boot a demo-seeded database with demo mode off, because its logins are public. Then set `PLUMB_DEMO: "0"` in `docker-compose.yml`, and add `PLUMB_ADMIN_EMAIL` and `PLUMB_ADMIN_PASSWORD` for the first admin (or run `docker compose exec plumb python -m app.cli create-admin you@example.org "Your Name"`).
2. Put it behind a TLS reverse proxy (Caddy, nginx, Traefik). Set `PLUMB_SECURE_COOKIES: "1"`, `PLUMB_BASE_URL` to the public address (it is signed into every record, and publishing is refused without it), and `FORWARDED_ALLOW_IPS` to the proxy's address (never `*`: that lets any client choose its own address and walk around the rate limits).
3. Back up `/data` on a schedule: `docker compose exec plumb python -m app.cli backup /data/backup.db`, plus `/data/signing-key.pem`. Records stay verifiable only with the same key; pin its public key (`/.well-known/plumb-key.json`) somewhere outside the portal.
4. Upgrade by backing up, pulling the new version and starting it; database migrations run on boot.
5. Import last year's event, or any fixture-shaped file, from *Import an event* on the home page.
6. When judging opens, lock the rubric (*Manage*, *Rubric*), so nobody can reweight after seeing the live ranking.

## Honest limitations

* Plumb sends no email. Invitations are links the organizer copies and sends, and there is no self-service password reset: an operator resets one with `docker compose exec plumb python -m app.cli set-password <email>`.
* "Anyone with an account" voting cannot stop determined sock puppets, because accounts are not tied to verified addresses. Use "participants only" for votes with prizes, and review the signals on the Integrity page.
* The fixture data's project summaries are the contest's placeholder text ("One line of what it does."); Plumb shows the data as given.
* The normalization model is additive; it does not model judges who stretch or squash their scale (JUDGING.md explains why, and the proof shows it still ranks best when they do).
* One instance per database file. SQLite is plenty for a hackathon, not for a multi-tenant service.
* Rate limits are in memory and reset on restart.

## Documents

* [ARCHITECTURE.md](ARCHITECTURE.md): how it is put together, and why.
* [DATA-MODEL.md](DATA-MODEL.md): the schema, the audit chain, migrations, import and export.
* [JUDGING.md](JUDGING.md): assignment, scoring math, normalization, the proof, pairwise mode, winners.
* [THREAT-MODEL.md](THREAT-MODEL.md): who might cheat, how, and what stops them.

## Tests

```
pip install -r requirements-dev.txt
python -m pytest -q                                    # 75 tests
ruff check app tests tools                             # lint
python tools/acceptance_run.py .dogfood.toml           # the DOGFOOD checker, against a running portal
python tools/normalization_proof.py --draws 1000       # the proof, about 30 seconds
```
