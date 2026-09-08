# Pool operations runbook: onboard, share, recalibrate, revoke

This is the human process around the `sk-`-only pool. Pool members only — no
display-only entries. No cookies, no chat-pasted sessions.

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
   Operator: `pool-recalibrate <name> 30.00`.
5. Test: one small non-stream call forced to that key, confirm
   `/pool/status` decrements and no key material in `/tmp/agentrouter-proxy.log`.

## Weekly recalibration
- Each pool member re-pastes numbers-only `quota` output.
- `pool-recalibrate <name> <dollars>` overwrites `initial_remaining` and zeroes
  `estimated_spent` with new `last_recalibrated` date. Never auto-adjust upward
  from estimates alone — human numbers win.
- If a key's local estimate and friend's number diverge >20%, note it in
  `pool.json` comment / ops log and check price table + `usage_unknown_count`.

## Revoke / forget (both halves)
1. Friend deletes the pooled `sk-` in their dashboard (record exact URL here
   once known: _______________).
2. Operator: `pool-forget <name>` → deletes key file, sets `enabled=false`,
   evicts cached client. Confirm with `pool-status` + `ls KEY_DIR`.
3. If any leak suspected (share link opened twice, chat paste, log hit):
   treat as compromised — do 1+2 immediately, rotate any sibling keys created
   in the same session.

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
