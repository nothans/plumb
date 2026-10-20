# After DOGFOOD 2026

DOGFOOD 2026 judged Plumb at commit [`5c36b75`](https://github.com/nothans/plumb/tree/5c36b7538816150e2cba038f1cc61382513cf7d7), frozen on 2026-09-29 at 18:00 UTC.
It placed 19th of 194 entries, with a score of 4.215, in the top 10%.

Everything after that commit is v1.1, built after the results came out.
None of it was judged.
This page says what we learned and what changed because of it.

## How we learned it

After the winners were published, we read the top ten entries and the public judge comments on them, then compared them with Plumb at its frozen commit.
We were not looking for a feature checklist.
We wanted to know what the strongest entries did that a judge could trust in ten minutes of reading.

Thank you to the teams behind Verdict, Quorum, LippInc, Raptor Desk, ballotbench, Rubric, Banana Hat, dogfood-judge, and BroadSheet.
Reading your work taught us more than our own reviews did.

## What the field did better

- **Rules lived in the database, not only in the app.**
  Most of the top entries enforced score ranges, deadlines and immutable results with database triggers.
  Plumb did that only for its audit log.
- **No route could ship without an access rule.**
  The best entries refused to start, or failed the build, when a route had no declared access.
  Plumb's isolation was correct, but it relied on each route remembering to check.
- **A production switch that protects itself.**
  Every top-ten entry could start empty and safe for a real event.
  Plumb had no such switch, and its demo seed could touch real data.
- **Uncertainty that leads to an action.**
  Strong entries aimed extra reviews at the projects a prize hung on, and blocked publishing until close calls were decided.
  Plumb reported its uncertainty but did not act on it.
- **Claims held in check.**
  Some entries generated their README tier tables from the committed test reports, and others simply claimed less.
  Judges noticed both.

## What we learned about our own entry

- **The first thirty minutes of a hands-on review set the score.**
  A careful judge could hit two traps: logging out of a demo session broke the checker, and saving unchanged settings returned an error.
- **A claim must not outrun its data.**
  The README said "proven on the DOGFOOD fixture data", and the proof table showed 50% in every row.
  The accurate wording is "shown in simulation", and a column that says nothing should not ship.
- **Evidence has to be visible without running anything.**
  Test counts, a proof for every claim, and a clear path through the docs matter as much as the code behind them.

## What changed in v1.1

v1.1 landed in five commits.

- **Demo mode stays away from real data.**
  It refuses to seed a real database, and there is a `retire-demo` command, a production compose file, and configurable rate limits.
- **A disqualified entry is gone everywhere.**
  A live entry is defined in one place, so disqualified entries leave the gallery, the API, the widget and the counts.
  Settings forms save without moving any times.
- **The community vote is unlinkable and checkable.**
  No voter appears in the log, a public vote log opens after the close, and the tally is in the signed record.
- **Results on small events claim only what the data supports.**
  Plumb uses Student's t intervals, states no probability below 20 residual degrees of freedom, and requires a signed reason for each close call before publishing.
  Imported people get claim links to take over their accounts, organizers can revoke invitations, and an import is validated in full before anything is written.
- **Rules are in the database, and access is declared.**
  Triggers enforce scores, freezes, the rubric, records, awards, votes and deadlines, and are checked against the code at every boot.
  Every route declares its access, and an undeclared route stops the boot.
- **Ideas borrowed from the field:**
  - review drafts saved on the server, and numbered, signed receipts for reviews and entries
  - per-team deadline extensions with a reason, enforced by the database
  - a judging method that is hashed and published when the rubric locks
  - extra reviews aimed at the projects a close call hangs on
  - a page where each team can see why it ranked where it did
  - a tested backup and restore, a `doctor` command, and an operations runbook ([OPERATIONS.md](OPERATIONS.md))
  - a README tier table generated from the reports and checked in CI, and a CI job that runs the portal and both checkers with the network cut off

## Tried and removed

We built a "does the winner survive losing any one judge" check, as several top entries had.
We measured it, and then took it out.
It fired on most healthy wins and predicted nothing the probability did not already say ([JUDGING.md section 9](JUDGING.md#9-winners)).

## What we kept

- **Intervals that cover what they claim.**
  In Plumb's own simulation, its 90% intervals contain the true value about 90% of the time in every scenario.
- **An extended checker built to distrust the portal.**
  It verifies signatures with its own Ed25519, receives webhooks on its own listener, and recomputes the vote seals.
- **One container on SQLite,** with CI against a real `docker compose up`.

## Where it stands

v1.1 passes 233 tests.
The DOGFOOD checker passes 7 of 7, and the extended checker passes 41 of 41, both run in a container with no network.
The committed reports are [acceptance-report.txt](acceptance-report.txt) and [acceptance-report-extended.txt](acceptance-report-extended.txt).
