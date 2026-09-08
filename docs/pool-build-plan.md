# Pool build plan: `sk-`-only, least-spent-first, spend-down tracking

## 0. Discovery (do first, read-only)
1. Capture one redacted sample each (values replaced, keys kept):
   - non-streaming `msg.usage` (`msg.model_dump_json()` in `proxy.py:535`)
   - streaming `message_start` / `message_delta` usage blocks
   - one `billing_summary` SSE block (temporarily log raw lines before filter,
     redacted, then revert). Question to answer: does it contain cost/usage?
2. Record AgentRouter out-of-credits / rate-limit status codes (expect 402/429,
   confirm 503 vs 401 semantics for dead key).
3. Snapshot per-model price table for the 5 stub models
   (`claude-opus-5`, `opus-4-8`, `gpt-5.6-sol`, `deepseek-v4-flash`, `glm-5.3`).
   Unknown price → `enabled=false` for that model-key combo.
4. Record dashboard key create/delete URLs + whether spend caps exist.

Decision gate: if `billing_summary` has authoritative cost, accounting uses it
(parse-then-drop, still filtered downstream). Else use usage × price table.

## 1. Storage
- `~/.config/opencode/api_keys/AGENTROUTER_FRIEND_<name>` — one `sk-` per file,
  `chmod 600`, enforced at startup (loud fail if group/other readable).
- `~/.config/opencode/api_keys/pool.json` (new, no secrets):
  ```json
  {
    "friend-a": {"initial_remaining": 30.0, "estimated_spent": 1.23,
                 "last_recalibrated": "2026-09-08", "enabled": true},
    "you": {"initial_remaining": 4.5, "estimated_spent": 0.0,
            "last_recalibrated": "2026-09-08", "enabled": true}
  }
  ```
- Atomic writes (`tmp + rename`), single-process lock (file lock or in-process
  mutex; document assumption: one proxy instance).
- `.gitignore`: `AGENTROUTER_FRIEND_*`, `pool.json` stays local-only (decide: track
  schema example as `pool.example.json`, never real balances).

## 2. Client pool (`proxy.py`)
- Replace `_client()` singleton with `dict[name -> anthropic.Anthropic]`, each built
  with existing `_build_http_client()` (sync httpx, keepalive, timeouts — WAF invariant).
- Lazy-load + cache; `forget <name>` evicts + closes underlying httpx client.
- Startup: load key files, skip missing/unreadable with warning, never log key material.

## 3. Routing: least-spent-first + failover
- Score = `initial_remaining - estimated_spent` (higher = pick first). Ties → fixed
  config order. Skip `enabled=false` or dead-marked keys.
- Reserve at pick time (in-memory `in_flight[name] += estimate_floor`) to avoid two
  concurrent grills piling onto the same least-spent key; reconcile on completion.
- Non-streaming (`asyncio.to_thread` path): loop keys in score order; on
  `402/429/503` try next; on `401` mark dead (disable until operator re-enables),
  try next; else return with `x-pool-key: <name>` debug header.
- Streaming (`_stream_worker` / `_stream_gen` path): pick key once before first byte;
  try-next only on upfront failure. Once bytes flow, stick. Errors surface as
  `event: error` with `rid` + key name, as today.
- Accounting hook (both paths): on completion extract `usage`, compute cost,
  `estimated_spent[name] += cost`, persist `pool.json`.

## 4. Cost computation
- Preferred: `billing_summary` cost if discovery confirms (parse-then-drop).
- Fallback: `(input_tokens × p_in + output_tokens × p_out)` from price snapshot.
  Cache-hit / reasoning tokens: price as documented, else conservative (max) + note.
- Fail-closed: if usage missing and no billing cost, count `0` + `usage_unknown`
  counter surfaced in `/pool/status` (never silently inflate spend).

## 5. Observability (local-only)
- `GET /pool/status` → `{per_key: {remaining, spent, enabled, last_recalibrated},
  total_remaining, usage_unknown_count}`. No secrets in output.
- Keep `rid` tracing; add `pool_key` to existing request start/end logs (name only).
- Redaction test: `rg -i 'sk-'` over logs must be empty.

## 6. CLI (local, in `~/bin/agentrouter-proxy` or new `pool` subcommand — decide owner)
- `pool-add <name>` (stdin-only key import, `600`), `pool-forget <name>`,
  `pool-recalibrate <name> <dollars>`, `pool-status` (reads `/pool/status` or file).
- Existing `quota` / `set-cookie` paths untouched (operator's own cookie only).

## 7. Verify
- Unit: score order, tie-break, dead-key skip, `forget`, cost math per model,
  atomic persist, redaction.
- E2E (2 test keys): force 503/402 on primary → fallback serves, `x-pool-key`
  flips, `/pool/status` decrements, stream still heartbeats (no `CHUNK_TIMEOUT`
  regression).
- Soak: concurrent long grill streams don't double-pick without reservation.

## 8. Rollout
1. Discovery samples only, no behavior change.
2. Pool behind flag (`POOL_ENABLED=0/1`), single-key default = current behavior.
3. Add first pool-member key → check sum math + live failover test.
4. Add second pool-member key → least-spent-first order + weekly recalibration reminder.
