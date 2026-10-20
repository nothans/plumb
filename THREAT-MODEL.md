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
| Judge or participant sees the ranking before publication | Results pages and APIs are organizer-only until `results_published_at` is set. After it, a team can see its own reviews, judges anonymous, without their comments. | `domain.results_for_viewer`, `domain.explain_ranking` |
| Judge reviews a project nobody assigned them | Refused in code, and the database has a composite foreign key from reviews to assignments. | `domain.save_review`; `schema.sql` |
| Judge reviews their own team or a colleague's | Judges and organizers cannot be on a team in the same event, in either order. The assigner and manual assignment refuse judges on the team or sharing a work email domain. | `domain._can_join`, `domain.accept_invitation`, `assign.conflict` |
| Judge quietly changes a review | Allowed until publication (judges refine), but every edit is audited with before and after values and gets a new numbered receipt. After publication reviews are locked, in the code and by the database's own rules. | `domain.save_review`, audit `review.edited`, `rule_reviews_frozen_*` |
| Colluding judge inflates a friend's project | Leniency correction removes a judge's general generosity, not favouritism toward one project. The mitigation is coverage (three or more independent reviews) and visibility: the organizer sees each review next to the others for the same project, and the adjusted score's interval shows when one review is carrying a project. | `normalize.fit`, organizer Reviews tab |
| Organizer changes the rubric or the method to favour a project | Reweighting is allowed until the rubric is locked (rubrics do get fixed mid-event) and audited with old and new weights. Locking registers the whole judging method (weights, model, thresholds, tie rule, a hash of the model's code) and publishes its hash before anyone has seen a ranking; the database then refuses changes, and the signed results say whether the method that produced them is the registered one. | `domain.lock_rubric`, `method.py`, `rule_rubric_frozen`, `rule_method_registered_once` |
| Organizer awards a prize past the top-ranked candidate, or on a coin flip | Allowed (organizers decide), but the signed record marks an override and states how sure the model was. A close call (under 60%, or too few reviews to say) cannot be published without the organizers' written reason, signed beside the award and shown with it. | `records.publish_results`, `domain.award_check` |
| Organizer ranks across judge groups that never shared a project | Judge lean cannot be compared across such groups, so publishing needs a signed reason for ranking across them, shown on the public results. | `records.publish_results` |
| Results claim more certainty than the reviews support | Intervals use Student's t; probabilities average over what the reviews leave uncertain, and below 20 reviews beyond one per project none are stated at all, because simulation showed them running high there. | `normalize.MIN_DOF`, JUDGING.md section 3 |
| Flat or careless judge | Flagged on the dashboard; their reviews still count only through shared projects. | `normalize` flags |

### Submissions

| Threat | Control | Where |
|---|---|---|
| Edit after the deadline | The deadline check comes straight after authentication in `save_project`, against the server clock, in the same transaction as the write, and the database refuses a submission time past the deadline on its own. Team changes freeze too. | `domain.save_project`, `domain._can_join`, `rule_submitted_in_time` |
| Organizer gives a favourite team more time | Only through an extension: one team, a later deadline that must end before judging closes, and a reason, all audited and signed into the results. Only that team's entry can change, and only until a judge reviews it; its members stay as they were at the close. | `domain.grant_extension`, `domain.save_project` |
| A team says it submitted something else | Every submitted entry, and every later change, gets a numbered receipt signed by the portal: the title, links and hashes of the text, and the times. | `records.entry_receipt` |
| Organizer reopens submissions after judges have scored | Refused: once any review exists, the close cannot move back into the future. | `domain._check_schedule_change` |
| Duplicate or resubmitted entries | Detected (same team, repository or title), excluded from gallery and ranking once marked, never deleted. | `domain.duplicate_candidates`, `resolve_duplicate` |
| Drafts leaking | A draft is visible only to its team and organizers; everyone else gets 404. | `domain.can_view_project` |

### Community vote

| Threat | Control | Where |
|---|---|---|
| Ballot stuffing by one account | One vote per project per voter (primary key), a fixed number of votes per voter (checked in the vote's transaction), no voting for your own team, judges, organizers and admins do not vote, 30 vote actions per minute per account, and the database refuses a vote dated outside the window. | `domain.cast_vote`, `ratelimit`, `rule_vote_in_window` |
| Sybil accounts | Organizers choose who may vote: anyone with an account, or only participants of the event (members of a team with a live entry). The integrity page lists accounts created after voting opened, addresses used by three or more voting accounts, and projects drawing votes from new accounts, and an organizer can void an account's votes with a reason, which is audited. | `domain.abuse_signals`, `remove_votes_of` |
| Position bias (first on the list wins) | Each voter's ballot is shuffled by a hash of (event, voter, project): stable for them, different for everyone else, not steerable. | `domain.ballot` |
| Bandwagoning and late tactical voting | Counts are hidden from everyone until voting closes; a closed vote cannot be reopened, even through switching it off and on; judged results cannot be published until it closes. | `domain.vote_tallies`, `domain._check_schedule_change`, `records.publish_results` |
| Organizer watches the race and ends the vote when a favourite leads | Votes enter the audit log (and every webhook) only as seals, `sha256(nonce:project)`; abuse signals never name the project a cluster favours while voting is open; an open vote can be extended but not shortened or switched off. After close the nonces are revealed, so anyone can check the tally against the sealed log. | `domain.cast_vote`, `domain.vote_reveal`, `domain.abuse_signals`, `domain._check_schedule_change` |
| Organizer reweights the rubric after seeing the ranking | The rubric can be locked, which also registers the method (above); the signed results state whether it was, and how many times it changed after the first review. | `domain.lock_rubric`, `domain.rubric_history` |
| Someone links a voter to their choices | The audit log, its webhooks and the public vote log carry seals, nonces and projects, never a voter or an address. The `votes` table keeps the voter for abuse review and voiding; reading it takes database access (below). | `domain.cast_vote`, `domain.vote_log` |

### History and results

| Threat | Control | Where |
|---|---|---|
| Rewriting what happened | The audit log is append-only (database triggers refuse UPDATE and DELETE) and hash-chained, through the whole log and within each event; the database refuses an entry that does not link to the one before. Any edit breaks every later hash, and the Integrity page and `python -m app.cli verify-audit` recompute the chain. A webhook receiver gets every field an entry's hash covers, so it can verify each entry and spot a gap on its own. | `audit.py`, `rule_audit_linked`, `webhooks.payload` |
| Changing results after publication | Publication issues a signed results record containing the ranking, awards and their reasons, the method and whether it is the registered one, the rubric, extensions, the community vote's tally and the audit chain head. The schedule, reviews, rubric, votes, duplicates and disqualifications then lock (the database refuses changes too), and the public results page, CSV and API are read from the signed record, never recomputed. | `records.publish_results`, `records.published_results`, `rule_*` |
| Rules weakened in the database by hand | At every boot each rule trigger is compared with the one the code defines; a changed one stops the portal from starting, and `doctor` names it. | `db.check_rules` |
| Forged certificate, judge record or receipt | All are Ed25519-signed canonical JSON. `/verify` checks against this portal's keys (never a key inside the envelope) and refuses a payload that is not in canonical form; `tools/verify_record.py` checks offline against a pinned key. A rotated key stays published with its retirement date, so older records still verify. | `records.check_envelope`, `signing.Signer.rotate`, `tools/verify_record.py` |
| A judge wants proof of what they submitted | A numbered, signed receipt for every submitted review, and their participation record carries a digest of their reviews, salted per judge so a single-review judge's scores cannot be guessed from it. | `records.review_receipt`, `records.review_digest_for` |

### Accounts and the web

| Threat | Control | Where |
|---|---|---|
| Password guessing | Passwords of 15 or more characters (NIST SP 800-63B-4 for a single factor), refusing ones built from the site's name or the account's address; scrypt hashes; 10 attempts per 5 minutes per email, and a per-address limit set high enough for a venue behind one address; unknown emails cost the same time as wrong passwords. | `security.password_problem`, `ratelimit` |
| Forging your address to dodge limits | Plumb only believes `X-Forwarded-For` from the proxies listed in `FORWARDED_ALLOW_IPS` (default: none but localhost), as addresses or networks. A real instance refuses to start with `*` (every client could choose its own address) or with an entry uvicorn would silently ignore. The test suite checks that a spoofed header does not reset the login limit. | `config.forwarded_problem`, `tests/test_flows.py` |
| Session or token theft | Random 256-bit tokens, stored hashed, HttpOnly, SameSite=Lax, Secure behind HTTPS (a real instance refuses an https address with insecure cookies); logout deletes the session; API tokens expire after 30, 90 or 365 days; changing or resetting a password ends every other session and every token. | `routes/account.py`, `domain.end_credentials` |
| CSRF | Every form carries a per-session token; cookie-authenticated JSON must come from the same origin; bearer-token requests are not cookie-based and so not forgeable. | `web.form_body`, `routes/api.json_guard` |
| A route added without a permission check | Every route's access is declared in one table and checked before the route runs; an undeclared route stops the app from starting, and a test calls every route as an anonymous visitor and as a participant. | `access.py`, `tests/test_access.py` |
| XSS | Jinja autoescaping everywhere, no inline script at all, and a CSP of `default-src 'self'`. Project links must be http or https. Control characters are refused in every text field. | templates, `main.py`, `domain.clean_url`, `domain._as_str` |
| Fake success messages | Notices come from a short-lived cookie set by Plumb's own redirects, never from the URL, so a crafted link cannot show a fake confirmation. | `web.redirect`, `web.render` |
| Spreadsheet formula injection | Every exported CSV cell starting with `=`, `+`, `-`, `@`, tab or carriage return is prefixed with `'`, so a project title or comment cannot run as a formula when an organizer opens the export. | `domain._cell` |
| Clickjacking | `frame-ancestors 'none'` and `X-Frame-Options: DENY`, except the embeddable gallery widget, which is read-only. | `main.py` |
| Claiming someone else's account | Signup cannot claim an email an import created. Such an account can be claimed only through an invitation or a claim link made by someone who organizes every event the account belongs to, so an organizer elsewhere cannot invite an imported judge's address and take the account over. | `domain.create_user`, `domain.may_claim_with`, `domain.issue_claim_link` |
| Leaked invitation or claim link | Single use, bound to one address, expires after 14 days; using it is audited. A pending invitation can be revoked, and making a new claim link for an account ends the earlier ones. | `domain.get_invitation`, `domain.get_claim_link`, `domain.revoke_invitation`, `domain.issue_claim_link` |
| Webhook secrets | Shown once; every delivery is HMAC-signed so receivers can reject forgeries. | `webhooks.py` |
| Webhooks aimed inside the network | Destinations that resolve to loopback, private, link-local or reserved addresses (including NAT64, IPv4-mapped and 6to4 disguises) are refused when the webhook is created and again at delivery, and the connection goes to the exact address that was checked, so DNS rebinding cannot redirect it; redirects are not followed; every delivery has a hard 10-second deadline, each hook delivers on its own thread and never has two requests in flight, so a slow or trickling receiver delays only itself; an event has at most 10 hooks. `PLUMB_WEBHOOKS_ALLOW_PRIVATE=1` lifts the address rule for a receiver on your own LAN, and `PLUMB_WEBHOOKS_ALLOW_HOSTS` exempts named hosts, in demo mode only (the demo compose file names `host.docker.internal`, so the extended checker can receive). | `webhooks.check_destination` |
| Running the demo in production | Demo mode publishes fixed session cookies (they are in `.dogfood.toml`) and a known password. Every page shows a banner while it is on; demo mode refuses to seed a database that holds anything it did not make; a demo-seeded database refuses to boot with demo mode off, until `retire-demo` ends every public login. A real event runs from `compose.prod.yml` with its own volume. | `cli.bootstrap`, `cli.retire_demo`, `compose.prod.yml` |
| A bad upgrade or restore | Migrations take a copy of the database first and refuse a database from a newer Plumb; a restore checks the copy (integrity, the audit chain, the schema, the rules) before it replaces anything, and keeps what it replaced. | `db.init_schema`, `backup.restore` |
| Host header written into signed records | Records carry only the configured `PLUMB_BASE_URL`; publishing is refused without it; the embed widget uses relative links. | `web.signing_base_url`, `records.publish_results` |

## What Plumb does not stop

Stated plainly so an organizer can decide what else they need:

* **Sybils in "anyone with an account" mode.** Plumb sends no email, so it cannot prove an address is real, and address signals are weak behind shared NAT or a VPN (and only meaningful at all when the proxy setting above is right). For a vote with a prize attached, use "participants only" or treat the signals page as a required review step before announcing.
* **An operator with database write access and the code.** They could rewrite the database and recompute every hash. The chain makes this detectable only against a copy of the head taken earlier. Publication puts the head into a signed record, so anything before publication is pinned once that record has left the building; publishing the head hash somewhere public (a post, a chat) at key moments closes the gap further.
* **The signing key's custody.** Whoever holds `/data/signing-key.pem` can issue records and receipts. It is created readable by its owner only; back it up, keep it off shared machines, and pin its public key somewhere outside the portal. If it may have leaked, `rotate-key` starts a new one and keeps the old one published with its retirement date, but anything the thief signed with a date before that cannot be told apart from the real thing.
* **An operator linking voters to choices.** The `votes` table keeps who voted for what, for abuse review and voiding; someone with database access can read it.
* **Judge favouritism toward one project.** The model corrects general leniency, not a targeted bump. Coverage and human review of outliers are the defence.
* **Denial of service.** Rate limits cover login, signup, votes and comments; the rest relies on the reverse proxy in front.
* **Rate limit memory.** Limits live in the process and reset on restart; `PLUMB_RATE_LIMITS` sets them.
