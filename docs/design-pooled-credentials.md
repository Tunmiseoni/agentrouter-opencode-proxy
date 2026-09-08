# Design: pooled friend credentials / combined balance

## Status
Grill session closed 2026-09-08. Core decisions settled. No code changed yet.

## Goal
Let one local proxy spend across N consenting friends' AgentRouter `sk-` keys,
show a combined remaining balance, and survive one key running dry — without
ever collecting browser cookies / dashboard sessions.

## Decisions
- `sk-`-only pool. No cookie exfiltration, no `New-API-User` + cookie reuse for
  friends, no WhatsApp cookie paste. Reason: cookies are ambient login authority
  (full dashboard session, unpredictable expiry), while `sk-` keys are scoped,
  revocable, and already the proxy's auth model (`AGENTROUTER_API_KEY` / key file).
- Display-sum + failover on non-stream; pick-once for streaming. Reason: one SSE
  stream = one billing identity; mid-stream key rotation is incoherent.
- Least-spent-first routing among enabled keys (user choice). Reason: spread spend
  instead of draining primary first. Requires local spend accounting (see below).
- Spend tracking = initial-numbers + local decrement + weekly recalibration.
  Friend pastes numbers-only `quota` text once (`Quota remaining: 42.1% ($12.34...)`),
  stored as `initial_remaining`. Proxy subtracts estimated cost per request.
  Recalibrate weekly (or on dispute) by re-pasting numbers. Reason: AgentRouter
  pricing page is authoritative but cookie-gated, so exact live pricing per key is
  out of reach; estimates + recalibration is the honest variant.
- `billing_summary` SSE stays filtered downstream; optionally parse-then-drop for
  accounting if discovery shows it carries cost. No new upstream shapes — still only
  `messages.create` via Python sync `anthropic.Anthropic` (WAF invariant).
- Pool members only: every entry holds a revocable `sk-` shared via one-time
  link and is spendable. No display-only / numbers-only entries — if you don't
  hold the key, it isn't in the pool.
- Revocation = friend deletes key in AgentRouter dashboard + operator runs
  `forget <name>` (deletes key file, disables in pool). Both halves required.

## Non-goals
- No N-cookie dashboard scraper (`GET /api/user/self` with friends' cookies).
- No round-robin blind spend without attribution; no mid-stream key switching.
- No evasion (IP rotation, fingerprint spoofing, staggering) to hide pooling.

## Open Questions (for build)
- Per-model price table source snapshot (AgentRouter pricing page, operator copy).
  Unknown-price models are excluded from pool until priced.
- Does `billing_summary` carry authoritative cost? Needs one redacted sample.
- Failover trigger set: `402/429/503` + `401` (dead key → disable + continue)?
  Confirm AgentRouter's out-of-credits status code.
- `pool.json` location + atomic-write + concurrency (single process now; lock file?).
- Dashboard revoke URL + per-key spend-cap support (record in runbook).

## Risks & Tradeoffs
- Referral provenance: accounts created solely for referral bonus, even with
  consent, match Sybil/farming patterns providers claw back. `sk-` pooling lowers
  session-hijack risk but NOT ToS/ban risk. Operator accepted ban as possible outcome.
- Estimates drift (promos, retries, cached-input pricing, failed-then-billed edge).
  Mitigation: weekly numbers-only recalibration, never auto-overwrite upward without
  friend confirmation.
- Least-spent-first oscillation: two concurrent grill sessions can both pick the
  same "least spent" key. Mitigation: in-memory reservation at pick time, reconcile
  on completion (see build plan).
- Secret sprawl: N keys on one Mac + `/tmp/*.log` + shell history. Mitigation:
  `600` files, stdin-only import, redacted logs, `forget` path, keys gitignored.
- Undocumented surface: `~/bin/agentrouter-proxy` CLI + `.tray/agentrouter-tray`
  binary live outside this repo. Pool status must not silently depend on them.
