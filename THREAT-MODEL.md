# Threat model

A judging portal is a machine for deciding who gets money and recognition, so people will try to bend it.
This page lists who might try, what they would try, what Plumb does about it, and what it does not stop.
Each control names the code that implements it; the test suite exercises most of them over HTTP.

## What is worth protecting

1. **The ranking**: judges' scores, the rubric, the normalization, and the published results.
2. **Judge independence**: a judge must not see other judges' scores or the running ranking before publication.
3. **The deadline**: nothing about a submission changes after the close.
4. **The community vote**: one person, a fixed number of votes, no peeking at counts.
5. **The record**: what happened, when, and by whom, in a form nobody can quietly rewrite.
6. **Accounts and personal data**: emails, sessions, API tokens.

## Who might try

| Actor | Can | Wants |
|---|---|---|
| Visitor | Browse public pages | Vote many times; spam comments |
| Participant | Everything a visitor can, plus their team and project | Edit after the deadline; see scores early; vote for themselves |
| Judge | Their queue and reviews | See other judges' scores to anchor on them; favour a friend's team |
| Organizer | Everything in their event | Move a deadline for a favourite; bury a review; quietly change results after publishing |
| Operator | Shell and database access | Rewrite history without leaving a trace |
| Outsider | The network | Hijack sessions, forge requests, forge certificates |

## Threats and controls

### Judging

| Threat | Control | Where |
|---|---|---|
| Judge reads another judge's scores via a URL | Refused in the domain layer (403), not hidden in a template. Asking for another judge's scores needs organizer rights on every event those scores belong to; the request is refused whole, so an empty answer and a forbidden one cannot be told apart. The same rule covers the page, the CSV and the API. | `domain.judge_scores`; `tests/test_acceptance.py`, `test_judge_isolation_everywhere` |
| Judge or participant sees the ranking before publication | Results pages and APIs are organizer-only until `results_published_at` is set. | `domain.results_for_viewer` |
| Judge reviews a project nobody assigned them | Refused in code, and the database has a composite foreign key from reviews to assignments. | `domain.save_review`; `schema.sql` |
| Judge reviews their own team or a colleague's | Judges and organizers cannot be on a team in the same event, in either order. The assigner and manual assignment refuse judges on the team or sharing a work email domain. | `domain._can_join`, `domain.accept_invitation`, `assign.conflict` |
| Judge quietly changes a review | Allowed until publication (judges refine), but every edit is audited with before and after values. After publication reviews are locked. | `domain.save_review`, audit `review.edited` |
| Colluding judge inflates a friend's project | Leniency correction removes a judge's general generosity, not favouritism toward one project. The mitigation is coverage (three or more independent reviews) and visibility: the organizer sees each review next to the others for the same project, and the adjusted score's interval shows when one review is carrying a project. | `normalize.fit`, organizer Reviews tab |
| Organizer changes the rubric to favour a project | Allowed before publication (rubrics do get fixed mid-event), audited with old and new weights, and frozen into the signed results record, so a reader can see the weights that produced the ranking. | `domain.update_rubric` |
| Organizer awards a prize past the top-ranked candidate | Allowed (organizers decide), but the signed record marks the award as an override and states how sure the model was about the choice. | `records.publish_results` |
| Flat or careless judge | Flagged on the dashboard; their reviews still count only through shared projects. | `normalize` flags |

### Submissions

| Threat | Control | Where |
|---|---|---|
| Edit after the deadline | The deadline check is the first thing `save_project` does after authentication, against the server clock, in the same transaction as the write. Team changes freeze too. | `domain.save_project`, `domain._can_join` |
| Organizer moves a deadline for one team | Deadlines are per event, not per team, and every change is audited with old and new values. | `domain.update_event` |
| Organizer reopens submissions after judges have scored | Refused: once any review exists, the close cannot move back into the future. | `domain._check_schedule_change` |
| Duplicate or resubmitted entries | Detected (same team, repository or title), excluded from gallery and ranking once marked, never deleted. | `domain.duplicate_candidates`, `resolve_duplicate` |
| Drafts leaking | A draft is visible only to its team and organizers; everyone else gets 404. | `domain.can_view_project` |

### Community vote

| Threat | Control | Where |
|---|---|---|
| Ballot stuffing by one account | One vote per project per voter (primary key), a fixed number of votes per voter (checked in the vote's transaction), no voting for your own team, judges do not vote, 30 vote actions per minute per account. | `domain.cast_vote`, `ratelimit` |
| Sybil accounts | Organizers choose who may vote: anyone with an account, or only participants of the event (which requires being on a team, which requires the event to be open). The integrity page lists accounts created after voting opened, addresses used by three or more voting accounts, and projects drawing votes from new accounts, and an organizer can void an account's votes with a reason, which is audited. | `domain.abuse_signals`, `remove_votes_of` |
| Position bias (first on the list wins) | Each voter's ballot is shuffled by a hash of (event, voter, project): stable for them, different for everyone else, not steerable. | `domain.ballot` |
| Bandwagoning and late tactical voting | Counts are hidden from everyone, organizers included, until voting closes; a closed vote cannot be reopened; judged results cannot be published while the vote is open. | `domain.vote_tallies`, `domain._check_schedule_change`, `records.publish_results` |

### History and results

| Threat | Control | Where |
|---|---|---|
| Rewriting what happened | The audit log is append-only (database triggers refuse UPDATE and DELETE) and hash-chained; any edit breaks every later hash, and the Integrity page and `python -m app.cli verify-audit` recompute the chain. | `audit.py`, `schema.sql` |
| Changing results after publication | Publication issues a signed results record containing the ranking, awards, method parameters, rubric and the audit chain head. The schedule, reviews, rubric, duplicates and disqualifications then lock, and the public results page is rendered from the signed record, not recomputed. | `records.publish_results`, `records.published_results` |
| Forged certificate or judge record | Records are Ed25519-signed canonical JSON. `/verify` checks against this portal's key (never a key inside the envelope), and `tools/verify_record.py` checks offline against a pinned key. | `records.check_envelope`, `tools/verify_record.py` |
| A judge wants proof of what they submitted | Their record contains a SHA-256 digest of their reviews, which they can recompute from their own export. | `records.review_digest_for` |

### Accounts and the web

| Threat | Control | Where |
|---|---|---|
| Password guessing | scrypt hashes; 10 attempts per 5 minutes per email and 60 per address; unknown emails cost the same time as wrong passwords. | `security.py`, `ratelimit` |
| Forging your address to dodge limits | Plumb only believes `X-Forwarded-For` from the proxies listed in `FORWARDED_ALLOW_IPS` (default: none but localhost). Behind a reverse proxy, set it to the proxy's address; never to `*`, which would let every client choose its own address. The test suite checks that a spoofed header does not reset the login limit. | `Dockerfile`, `tests/test_flows.py` |
| Session theft | Random 256-bit tokens, stored hashed, HttpOnly, SameSite=Lax, Secure behind HTTPS; logout deletes the session. | `routes/account.py` |
| CSRF | Every form carries a per-session token; cookie-authenticated JSON must come from the same origin; bearer-token requests are not cookie-based and so not forgeable. | `web.body`, `routes/api.json_guard` |
| XSS | Jinja autoescaping everywhere, no inline script at all, and a CSP of `default-src 'self'`. Project links must be http or https. Control characters are refused in every text field. | templates, `main.py`, `domain.clean_url`, `domain._as_str` |
| Fake success messages | Notices come from a short-lived cookie set by Plumb's own redirects, never from the URL, so a crafted link cannot show a fake confirmation. | `web.redirect`, `web.render` |
| Spreadsheet formula injection | Every exported CSV cell starting with `=`, `+`, `-`, `@`, tab or carriage return is prefixed with `'`, so a project title or comment cannot run as a formula when an organizer opens the export. | `domain._cell` |
| Clickjacking | `frame-ancestors 'none'` and `X-Frame-Options: DENY`, except the embeddable gallery widget, which is read-only. | `main.py` |
| Claiming someone else's account | Signup cannot claim an email an import created. Such an account can only be claimed through an invitation from the event it belongs to: an organizer of another event cannot invite an imported judge's address and take the account over. | `domain.create_user`, `domain.may_claim_with` |
| Leaked invitation link | Single use, bound to one address, expires after 14 days; accepting it is audited. | `domain.get_invitation`, `domain.accept_invitation` |
| Webhook secrets | Shown once; every delivery is HMAC-signed so receivers can reject forgeries. | `webhooks.py` |
| Webhooks aimed inside the network | Destinations that resolve to loopback, private, link-local or reserved addresses are refused when the webhook is created and again at delivery; redirects are not followed; each hook gets a bounded time per delivery pass, so one slow receiver cannot stall the others. `PLUMB_WEBHOOKS_ALLOW_PRIVATE=1` lifts the address rule for a receiver on your own LAN. | `webhooks.check_destination` |
| Running the demo in production | Demo mode publishes fixed session cookies (they are in `.dogfood.toml`). Every page shows a banner while it is on, and the README's "Run a real event" section turns it off. | `docker-compose.yml`, `base.html` |

## What Plumb does not stop

Stated plainly so an organizer can decide what else they need:

* **Sybils in "anyone with an account" mode.** Plumb sends no email, so it cannot prove an address is real, and address signals are weak behind shared NAT or a VPN (and only meaningful at all when the proxy setting above is right). For a vote with a prize attached, use "participants only" or treat the signals page as a required review step before announcing.
* **An operator with database write access and the code.** They could rewrite the database and recompute every hash. The chain makes this detectable only against a copy of the head taken earlier. Publication puts the head into a signed record, so anything before publication is pinned once that record has left the building; publishing the head hash somewhere public (a post, a chat) at key moments closes the gap further.
* **The signing key's custody.** Whoever holds `/data/signing-key.pem` can issue records. Back it up, restrict it, and pin its public key somewhere outside the portal.
* **Judge favouritism toward one project.** The model corrects general leniency, not a targeted bump. Coverage and human review of outliers are the defence.
* **Denial of service.** Rate limits cover login, signup, votes and comments; the rest relies on the reverse proxy in front.
* **Rate limit memory.** Limits live in the process and reset on restart.
