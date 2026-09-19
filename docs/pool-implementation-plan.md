# Implementation plan: `sk-`-only key pool with key-scoped usage accounting

Status: settled 2026-09-16. Supersedes the accounting section of
`pool-build-plan.md` (the token × price estimate + price-table approach) with a
discovered, cookie-free, authoritative usage endpoint. Storage, routing, CLI,
and ops decisions carry over from `design-pooled-credentials.md` and
`pool-operations.md`.

## Implementation status (2026-09-16)

Landed in `proxy.py` (routing auto-enables; `POOL_ENABLED=0` is the kill switch)
with `tests/test_pool.py`:

- Registry with lazy per-key `anthropic.Anthropic` clients on the shared
  `_HTTP_CLIENT`; eviction never calls `.close()` (the SDK's `close()` would
  tear down the shared httpx transport for every key).
- Usage cache + FastAPI `lifespan` background refresh + refresh on
  `/pool/status`; config reloads on `pool.json` mtime change, so CLI edits need
  no restart.
- Least-spent-first pick with in-flight reservation; non-streaming failover on
  `402/429/503` + `401` dead-marking; streaming failover only before the first
  forwarded byte.
- Cold/stale cache recovery: one synchronous usage refresh, then fall back to
  the primary key; last known usage retained (`stale`) instead of dropped.
- Automatic `you` member (cookie-sourced virtual, or anchor-based via CLI).
- `GET /pool/status`.
- CLI in `bin/agentrouter-proxy`: `pool-add` (stdin-only import, 600),
  `pool-recalibrate` (reads live usage via the proxy's own
  `_fetch_usage_cents`; tightens the primary key to 600 for `you`),
  `pool-forget`, and `pool-status [--raw]` (falls back to the local files when
  the proxy is stopped).
- Tray (`tray.swift`): combined pool total as the title (`AR $200.00`, with a
  `!` when routing is off or usage is unknown) and a **Pool** submenu with
  per-key remaining/spent/state; falls back to the own-account percentage when
  no pool keys are configured.

Deviations from the text below:

- **Routing auto-enables** when `pool.json` has an enabled, anchored key;
  `POOL_ENABLED=0` is the kill switch and `POOL_ENABLED=1` forces it on. The
  plan's "default 0" is replaced by this auto behavior.
- **Availability fallback:** a cold/failed usage cache no longer errors. The
  proxy refreshes once, then falls back to the primary key. Last known usage is
  retained (marked `stale`) instead of being discarded on a failed refresh.
- **`you` is included automatically** once a friend is pooled, sourced from the
  dashboard cookie (`/api/user/self`) as a virtual member. Because that endpoint
  is intermittently captcha-gated, `pool-recalibrate you <remaining>` anchors it
  reliably (then tracked like any key); the cookie value is used opportunistically.
- Dead state lives in a **separate proxy-owned `pool-state.json`** rather than
  `pool.json` (single writer per file; see §1).
- Un-anchored keys are **excluded + warned** (the open item's proposed choice).
- `POOL_RESERVE_USD` default is `0.05`.
- `AGENTROUTER_TARGET` and `AGENTROUTER_PROXY_URL` override the upstream target
  and the local proxy URL for the CLI (useful for testing); defaults unchanged.
- `/pool/status` adds a `configured` count alongside `enabled` (resolved routing
  state), plus per-key `virtual` and `stale` flags.

Deferred: the `pool.py` extraction (`docs/pool-modularization.md`).

## Goal

Let one local proxy spend across N consenting friends' AgentRouter `sk-` keys,
show a combined remaining balance, and survive a key running dry — without ever
collecting browser cookies / dashboard sessions.

## Discovery findings (2026-09-16, read-only probes)

Probed against a live AgentRouter account with the existing key:

| Probe | Result |
|---|---|
| `GET /v1/dashboard/billing/usage` with `Authorization: Bearer sk-…` | `200 {"object":"list","total_usage":57965.2012}` |
| `GET /dashboard/billing/usage` (no `/v1`) | same |
| `GET /v1/dashboard/billing/subscription` | `200`, `hard_limit_usd: 100000000` (sentinel, unusable) |
| `GET /v1/dashboard/billing/credit_grants` | `404 Invalid URL` |
| `GET /api/user/self` with Bearer key only | `200` but `quota`/`used_quota`/`username` all `null` |
| Cookie baseline `/api/user/self` | `used_quota=289826006` → `/500000 = $579.65` |
| Cross-check | `total_usage/100 = $579.652` == `used_quota/500000` → **exact match** |

Confirmed properties of `/v1/dashboard/billing/usage`:

- **Authoritative**: equals the cookie-based `used_quota`, in **cents**.
- **Monotonic**: two reads straddling one small request went
  `58017.7322 → 58017.7574` (+0.0252¢).
- **Key-scoped**: authenticated by the `sk-` alone; no `New-API-User`, no cookie.
- **WAF-safe**: plain `httpx` via the proxy's existing `_HTTP_CLIENT` works
  (unlike `/v1/messages`).
- **Date params ignored**: any `start_date`/`end_date` combination returns the
  same account-cumulative number (even a 2025 window). Treat as a single
  monotonic counter.
- **Account-wide, not per-key**: the number aggregates the whole account that
  owns the token.

Also confirmed: `billing_summary` never appeared in a raw streaming response, so
there is no per-request cost event to parse. Dropped from the plan.

Consequence: a key yields **spend**, not **remaining**. Remaining is derived
from a one-time human anchor plus usage deltas (below).

## Tracking model: anchor + live usage deltas

```
remaining_usd(key) = anchor_remaining_usd
                   - (usage_now_cents - anchor_usage_cents) / 100
```

- Friend pastes **one** numbers-only balance (their own `agentrouter-proxy quota`
  output) at onboarding.
- Proxy reads that key's `total_usage` at the same moment and stores it as
  `anchor_usage_cents` alongside `anchor_remaining_usd`.
- Thereafter `remaining` updates automatically from the live endpoint. No price
  table, no token estimation, no drift.
- `pool-recalibrate` re-anchors; needed only after a top-up, a reset, or if a
  friend uses the same account elsewhere (usage is account-wide).
- Fail-closed: if the endpoint is unreachable, mark usage unknown and surface
  `usage_unknown_count` in `/pool/status`; do not silently report stale numbers
  as live.

## Components

### 1. Storage (`~/.config/opencode/api_keys/`)

- `AGENTROUTER_FRIEND_<name>` — one `sk-` per file, `chmod 600`, stdin-only
  import, never logged, never committed.
- `pool.json` (no secrets):

  ```json
  {
    "you":      {"enabled": true,  "anchor_remaining_usd": null, "anchor_usage_cents": null, "recalibrated": null},
    "friend-a": {"enabled": true,  "anchor_remaining_usd": 30.0, "anchor_usage_cents": 1234.5, "recalibrated": "2026-09-16"}
  }
  ```

- `pool-state.json` (no secrets) — **implemented deviation from the original
  single-file design.** `dead` is the only pool field the running proxy must
  persist, and the CLI and proxy would otherwise both write `pool.json`. Splitting
  keeps one writer per file, so each side can use atomic `tmp` + `rename` with no
  cross-process lock:

  ```json
  {"friend-a": {"dead": true, "dead_at": "2026-09-16T00:00:00+00:00", "last_status": 401}}
  ```

  A dead mark is honoured only while `dead_at` (date) is later than the key's
  `recalibrated` (date), so `pool-recalibrate` re-enables a key without editing
  the proxy-owned file.

- Atomic writes (`tmp` + `rename`); single-process in-process lock (documented
  assumption: one proxy instance).
- `.gitignore`: add `AGENTROUTER_FRIEND_*` and `pool.json`; track a
  `pool.example.json` schema only.

### 2. Client / key registry (`proxy.py`)

- Replace the `_client()` singleton with a name → `anthropic.Anthropic` registry.
- Each entry is built with the **shared** `_HTTP_CLIENT` (sync httpx, keepalive,
  timeouts — the WAF invariant) and its own `api_key`.
- Lazy build + cache; `pool-forget <name>` evicts the entry and marks disabled.
- Startup: load files, skip missing/unreadable with a warning; enforce `600`;
  never log key material.

### 3. Usage cache (`proxy.py`)

- Per-key cached `total_usage` (cents) + `fetched_at`.
- Refreshed by a background task every `POOL_REFRESH_S` (default 120) and on
  every `/pool/status` request.
- Always call with an explicit wide range
  (`?start_date=1970-01-01&end_date=<today>`) for a consistent fresh reading;
  verify monotonicity in tests (the no-params form may be cached).

### 4. Routing: least-spent-first + failover

- Score = `remaining_usd` (higher = pick first); ties → fixed config order;
  skip `enabled=false` or dead-marked keys.
- Reserve at pick time (`in_flight[name] += POOL_RESERVE_USD`) to stop two
  concurrent grill streams double-picking; reconcile on completion.
- Non-streaming path: iterate keys in score order; on `402`/`429`/`503` try the
  next; on `401` mark dead (disable until operator re-enables); on success return
  with debug header `x-pool-key: <name>`.
- Streaming path (`_stream_worker` / `_stream_gen`): pick once before the first
  byte; try-next only on upfront failure. Once bytes flow, stick. Errors surface
  as `event: error` with `rid` + key name, as today.

### 5. Accounting

- No per-request cost computation. `remaining` is derived from the cumulative
  usage counter (section "Tracking model").
- On completion, release the reservation; optionally nudge a usage refresh if
  the cached value is stale.
- Emit `usage_unknown` when a refresh fails.

### 6. Observability (local-only)

- `GET /pool/status`:

  ```json
  {
    "enabled": true,
    "per_key": {"friend-a": {"remaining": 28.7, "spent": 1.3, "enabled": true, "dead": false, "recalibrated": "2026-09-16", "usage_unknown": false}},
    "total_remaining": 28.7,
    "usage_unknown_count": 0
  }
  ```

- Keep `rid` tracing; add `pool_key=<name>` to request start/end logs (name only).
- Redaction check: `rg -i 'sk-'` over `/tmp/agentrouter-opencode-proxy.log` must
  be empty.

### 7. CLI (`bin/agentrouter-proxy`, repo)

- `pool-add <name>` — stdin-only key import, `600`.
- `pool-forget <name>` — delete key file, `enabled=false`, evict cached client.
- `pool-recalibrate <name> <remaining_or_total>` — read live usage, store anchor.
- `pool-status [--raw]` — read `/pool/status` (fallback: `pool.json`).
- Existing `quota` / `set-cookie` untouched — the operator's own cookie remains
  the exact source for `you`.

### 8. Tray (`tray.swift`)

- Poll `pool-status --raw`; title shows the combined dollar total
  (e.g. `AR $28.70`) with a `!` when routing is off or usage is unknown.
- Menu gains a "Pool" submenu listing routing state, combined total, and per-key
  remaining + enabled/dead/unanchored/unknown state.
- Falls back to the own-account `quota` percentage when no pool keys exist.

### 9. Config knobs

| Env | Default | Meaning |
|---|---|---|
| `POOL_ENABLED` | unset (auto) | unset = route when an enabled, anchored key exists; `1` force on; `0` kill switch |
| `POOL_REFRESH_S` | `120` | Usage-cache TTL / background refresh interval |
| `POOL_RESERVE_USD` | `0.05` | In-flight reservation to avoid concurrent double-pick |

## Tests (`tests/`)

- Remaining math: anchor + usage delta, top-up re-anchor, unknown-usage path.
- Least-spent ordering and tie-break.
- Dead-key skip / `401` disable; failover on `402`/`429`/`503`.
- `pool-forget` eviction.
- Atomic `pool.json` persist.
- Reservation under concurrency (two picks do not target the same least-spent key).
- Redaction: no `sk-` in log output.

## Verification

1. Unit suite green (`.venv/bin/python -m unittest discover -v`).
2. Two test keys: force `402` on primary → fallback serves; `x-pool-key` flips;
   `/pool/status` decrements.
3. Long streaming request still heartbeats (no `CHUNK_TIMEOUT` regression).
4. `POOL_ENABLED=0` reproduces today's single-key behavior byte-for-byte.

## Rollout

1. Merge with routing auto-enabling only when a key is anchored (`POOL_ENABLED=0`
   remains the kill switch); a proxy with no `pool.json` is unchanged.
2. Add first friend key, `pool-recalibrate`, validate sum math + live failover.
3. Add second key; exercise least-spent-first order + weekly recalibration.
4. Tray shows the combined total and per-key state automatically.

## Onboarding workflow (from `pool-operations.md`)

1. Friend creates a dedicated `sk-` key (spend cap if supported) in their own
   dashboard; keeps their login private.
2. Friend shares only that key via a one-time secret share.
3. Operator: `pool-add <name>` (stdin-only).
4. Friend pastes numbers-only `agentrouter-proxy quota` output; operator:
   `pool-recalibrate <name> <dollars>`.
5. Test one small call; confirm `/pool/status` decrements and no key material in
   logs.
6. Weekly: friend re-pastes numbers; re-run `pool-recalibrate`. Re-anchor after
   any top-up.

## Risks / open items

- **Account-wide usage**: a friend using the same account for anything else
  consumes the pool view. Acceptable for unused dedicated keys; visible as faster
  than expected decrement + a >20% divergence at recalibration.
- **Ban/clawback risk is not removed by `sk-`-only**: pooling
  referral-provenance accounts can still be clawed back. Keep `POOL_ENABLED=0` as
  the kill switch; never create replacement accounts to dodge a clawback.
- **Endpoint caching**: the no-params usage form appeared cached once; standardize
  on the explicit wide-date form and assert monotonicity in tests.
- **Single-instance assumption**: `pool.json` locking assumes one proxy process;
  document if that changes.
- **Unknown key balance until anchored**: a key with no `anchor_remaining_usd`
  is excluded from routing and warned about at startup (and listed as
  `unanchored` in `/pool/status`); run `pool-recalibrate` to enroll it.
