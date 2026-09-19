# Pool operations runbook: onboard, share, recalibrate, revoke

This is the human process around the `sk-`-only pool. Pool members only — no
display-only entries. No cookies, no chat-pasted sessions.

The `pool-*` subcommands below are implemented in `bin/agentrouter-proxy`.
`pool-add` reads the key from stdin only (never argv); `pool-recalibrate`
reads the live usage counter through the proxy's own code path, so it needs the
repo `.venv` (created by `bash start.sh`).

## Onboard a pool member
1. Friend, in their own browser, creates a NEW `sk-` key in the AgentRouter
   dashboard (dedicated to pooling, with spend cap if supported). They keep
   their login to themselves.
2. Friend shares ONLY that key via a one-time secret share (pick one):
   - 1Password Send (1 view) / PrivateBin burn-after-reading / `age`-encrypted file.
   - In person: QR / copy from their screen. NOT WhatsApp / Discord history,
     NOT email, NOT screenshots.
3. Operator imports stdin-only (never as argv, never `echo 'sk-...'`):
   `pool-add <name>` → paste → file `AGENTROUTER_FRIEND_<name>` (`600`).
   Delete the share link / file immediately after import.
4. Friend pastes numbers-only balance text (from their local
   `agentrouter-proxy quota`): e.g. `Quota remaining: 100% ($30.00)`.
   Operator: `pool-recalibrate <name> 30.00`. This is what makes the key
   spendable — an un-anchored key is excluded from routing.
5. Test: one small non-stream call forced to that key, confirm
   `/pool/status` decrements and no key material in `/tmp/agentrouter-proxy.log`.

Run `pool-status` (or `pool-status --raw`) any time to see combined remaining.
It uses the running proxy when pooling is on, and falls back to the local files
(reading usage live) when the proxy is stopped.

## Include your own account (`you`)
Your own key joins the pool automatically once a friend is pooled: its balance
is read live from your dashboard cookie and shown as the virtual `you` member.
`/api/user/self` is intermittently behind an Aliyun captcha challenge, so when
the cookie path is unavailable `pool-status` prints a note and `you` shows
`usage unknown`. Anchor it once for reliable inclusion:

```bash
agentrouter-proxy pool-recalibrate you 179.62
```

This also tightens `AGENT_ROUTER_API_KEY` to mode `600`. Once anchored, `you`
tracks like any other key (recalibrate after top-ups).

## Availability
If no key currently has a balance figure (cold start, or a usage refresh just
failed), the proxy refreshes usage once and then falls back to the primary key
instead of failing the request. A key whose latest refresh failed keeps its last
known value and is marked `stale` in `/pool/status`; it remains routable.

## Weekly recalibration
- Each pool member re-pastes numbers-only `quota` output.
- `pool-recalibrate <name> <dollars>` re-reads `total_usage` at that moment,
  stores it as the new anchor, and stamps `recalibrated`. It also sets
  `enabled=true` and, because dead marks only apply while older than
  `recalibrated`, clears any 401 dead mark for that key.
- Never auto-adjust upward from estimates alone — human numbers win.
- If a key's anchor age is old, re-run recalibration rather than trusting the
  derived remaining.

## Revoke / forget (both halves)
1. Friend deletes the pooled `sk-` in their dashboard (record exact URL here
   once known: _______________).
2. Operator: `pool-forget <name>` → deletes the key file and sets
   `enabled=false` in `pool.json`; the proxy evicts the cached client on its
   next config reload. Confirm with `pool-status` + `ls KEY_DIR`.
3. If any leak suspected (share link opened twice, chat paste, log hit):
   treat as compromised — do 1+2 immediately, rotate any sibling keys created
   in the same session.

## Configuration
- Routing auto-enables once `pool.json` has one enabled, anchored key, so
  `pool-add` + `pool-recalibrate` is all an operator needs.
- `POOL_ENABLED=0` forces routing off (kill switch); `POOL_ENABLED=1` forces it
  on even before any key is calibrated.
- Optional: `POOL_REFRESH_S` (usage-cache TTL, default 120) and
  `POOL_RESERVE_USD` (in-flight reservation, default 0.05).

## What NOT to do
- Don't ask for, accept, or store `New-API-User` id + cookie jar strings.
- Don't paste secrets in WhatsApp (history, backups, notification previews).
- Don't commit key files or `pool.json` with real balances (schema example only).
- Don't "fix" a 401 by retrying the same dead key in a loop — disable it.

## If banned / clawed back
Accepted risk for this pool (referral-provenance accounts). Response:
1. Stop pool routing (`POOL_ENABLED=0`), keep proxy single-key for diagnosis.
2. Record status codes + bodies (redacted) + which key triggered first.
3. Don't create replacement accounts to dodge — that escalates a billing dispute
   into platform abuse. Friends re-decide consent before any restart.
