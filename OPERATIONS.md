# Running a real event

The runbook for an operator, from a month before the event to after the results.
Every `python -m app.cli` command runs inside the container: prefix it with `docker compose -f compose.prod.yml exec plumb`, except a restore, which needs the portal stopped (see "Restore" below).
All times Plumb shows and stores are UTC.

## Restore

A restore replaces the live database, so the portal must not be running:

```
docker compose -f compose.prod.yml cp plumb-backup.db plumb:/data/restore-me.db     # put the copy in the volume
docker compose -f compose.prod.yml stop plumb
docker compose -f compose.prod.yml run --rm plumb python -m app.cli restore /data/restore-me.db
docker compose -f compose.prod.yml start plumb
```

The restore checks the copy before it replaces anything (SQLite's integrity check, the whole audit chain, the schema version, the rule triggers), keeps the database it replaces beside it as `plumb.db.before-restore-<time>.bak`, and warns if the copy holds records signed by a key this portal does not have: put that `signing-key.pem` back too.

## A month out

1. Install from `compose.prod.yml` with a filled-in `.env` (README, "Run a real event"), behind a TLS reverse proxy that passes the `Host` header.
2. Run `python -m app.cli doctor`.
   Every line should read `ok`; each `FIX` line says what to change.
3. Log in as the admin, create the event (or import last year's from *Import an event*) and invite its organizers from *Judges*.
4. Copy `/data/signing-key.pem` somewhere safe and off the host, and write down its public key from `/.well-known/plumb-key.json` somewhere outside the portal.
   Records verify only with the key that signed them; someone checking a certificate in a year needs that public key, not the portal.
5. Schedule backups: `python -m app.cli backup /data/backups/plumb-$(date +%F).db`, then copy the file off the host.
   The backup verifies itself as it is made, and warns while the copy is still inside the data volume.
6. Do one restore drill now, on a scratch copy, not the live instance: a second checkout of the compose files with its own project name (`docker compose -p plumb-drill -f compose.prod.yml ...`) gets its own volume; restore a backup into it as in "Restore" above, start it, and open the results.

## Two weeks out

1. Set the schedule, tracks and prizes in *Settings*, the rubric in *Rubric*, and the target reviews per project.
   Plumb's probabilities need at least 20 reviews beyond one per project (about 3 per project on a 10-project event, fewer per project on a large one); below that it shows the ranking and its intervals but no probabilities, and every prize becomes a close call that needs a reason.
2. Invite judges from *Judges*.
   Judges imported with last year's event have accounts but no password: make each a claim link from *No account yet*.
   Plumb sends no email: you send the links.
3. Pick the voting mode.
   "Participants" (members of a team with an entry) is the one to use when the vote carries a prize; "accounts" lets anyone with an account vote, and sock puppets with it.
4. If the venue puts everyone behind one public address, raise the per-address limits: `PLUMB_RATE_LIMITS=signup=1000/3600,login-ip=2000/300` in `.env`, then restart.
5. Run a rehearsal event end to end with a few colleagues, then archive it from its *Settings*.

## The day before

1. `python -m app.cli doctor`, then a backup.
2. Check the deadline on the event page reads the time you mean, in UTC.
3. Agree, among the organizers, when you would give a team an extension: a power cut, a venue failure.
   Every extension carries its reason into the audit log and the signed results.

## While submissions are open

* *Progress* shows teams and entries as they arrive; *Integrity* shows likely duplicate submissions.
* A team that lost time to something outside its control gets an extension in *Settings*, *Deadline extensions*: one team, a later deadline, a reason.
  Its members stay as they were at the close; only its entry can change, until the extension ends or a judge reviews it.

## When submissions close

1. Lock the rubric in *Rubric*.
   That registers the judging method and publishes its hash on the event page, before anyone has seen a ranking.
2. In *Assignments*, assign automatically, then check *By project* for anything short.
3. Watch *Progress*.
   A judge who stopped reviewing keeps their projects until you release them (*Assignments*, *Open work by judge*); assign automatically again to give them to others.
   Judges flagged `flat`, `harsh` or `generous` are worth a word, not a removal: the model already corrects for lean.
4. Once results take shape, *Assignments*, *Reviews where they decide a prize* gives one more review to each project a close-call prize hangs on.

## The community vote

* The vote opens no earlier than submissions close, and an open vote can be extended but never shortened.
* *Integrity* shows the abuse signals while it runs (shared addresses, accounts made mid-vote) without saying which project they favour.
  Void a voter's votes with a reason before publication; voiding is refused after it.
* After it closes, the public vote log lets anyone check the count.

## Publishing

1. Open *Results*.
   For every close call, write the reason you award it as you do (a judging-room discussion, a demo, a shared prize); it is signed beside the award.
   If the judges fell into groups that share no project, *Results* says so and asks why ranking across them is fair; bridging assignments are the better fix.
2. Publish.
   It cannot be undone: reviews, the rubric, the schedule and the awards freeze, and the database refuses to change them.
3. Open the public results and one certificate, download its envelope, and check it at `/verify`.
4. `python -m app.cli doctor` again, then a backup, copied off the host.

## After

* Archive the event when it should leave the home page; its links keep working.
* Keep the last backup and the signing key for as long as anyone may want to check a certificate.
* Teams can open *Why it ranked where it did* on their team page.

## When something goes wrong

| What you see | What to do |
|---|---|
| Someone forgot their password | `python -m app.cli set-password <email>` (it ends their sessions and tokens); people who know theirs change it on their page |
| Everyone at the venue hits "too many attempts" | The proxy's address is missing from `FORWARDED_ALLOW_IPS`, so every visitor looks like one address; fix it in `.env`, or raise `PLUMB_RATE_LIMITS` |
| The portal will not start | Read the first lines of `docker compose -f compose.prod.yml logs plumb`: a refused boot names each setting to fix and changes nothing |
| The database is damaged or a bad change went in | Restore the last good backup ("Restore" above); the replaced database is kept beside it |
| An upgrade went wrong | Every upgrade that migrates leaves `plumb.db.before-schema-<new>-from-<old>.bak` in `/data`: go back to the old version's image and restore that file the same way |
| The signing key may have leaked | `python -m app.cli rotate-key`, restart, and tell people: records signed before keep verifying under the retired key, published with its retirement date; anything signed by the old key after that date is not genuine |
| The audit chain fails on *Integrity* | Stop changes, take a backup of the database as it is, and compare it with your last good backup; the chain names the first entry that does not verify |
| `doctor` reports a rule trigger that differs | Someone changed the database by hand. Plumb refuses to start until it is restored from a backup or the trigger is recreated from `app/schema.sql` |
