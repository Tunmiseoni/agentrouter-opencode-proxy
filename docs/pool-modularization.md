# Deferred: extract the pool into `pool.py`

Status: intentional deferral (2026-09-16). The first pool implementation landed
inside `proxy.py` (see `docs/pool-implementation-plan.md`) because the routing
work touches the streaming pump and the sync-client registry directly, and was
riskier to split while unverified. Modularize once the behavior is proven live.

## Why it was inlined

- The pool shares `_HTTP_CLIENT` (the WAF allowlists the sync TLS handshake),
  `TARGET`, the timeout knobs, and `_client()`. Extracting them first would have
  meant a wide, behavior-neutral refactor ahead of the risky change.
- `tests/test_proxy.py` imports `proxy` and calls module functions directly, so
  keeping the pool as module-level functions kept the test story identical.

## Target layout

Move the whole `# ── Key pool` section (currently between `_client()` and
`# ── Model list`) plus the pool route/lifespan glue into `pool.py`:

- `pool.py`: config knobs, ledger/registry, usage cache, `_pick`/`_release`,
  `_mark_dead`, `_pool_status`, `_create_with_failover`, `_stream_attempt`.
- `proxy.py` keeps: `_HTTP_CLIENT`/`_build_http_client`, `_client`, routes,
  streaming pump entry points.
- Injection points to avoid a circular import: pass `http_client`, `target`,
  `max_retries`, `models_timeout`, `key_dir`, and a `primary_client` callable
  into a small `Pool` object rather than importing `proxy` from `pool`.

## Suggested shape

```python
class Pool:
    def __init__(self, *, http_client, target, key_dir, primary_client,
                 max_retries, timeout, enabled, refresh_s, reserve_usd): ...
    def active(self) -> bool: ...
    def pick(self, exclude: set[str]) -> tuple[str | None, float]: ...
    def release(self, name: str | None, amount: float) -> None: ...
    def mark_dead(self, name: str, status: int) -> None: ...
    def status(self, refresh: bool = False) -> dict: ...
    def create_with_failover(self, kw: dict, rid: str): ...
```

`proxy.py` would build one `Pool` at import and keep thin module-level shims
(`_pick`, `_release`, ...) so existing tests and call sites keep working.

## Guardrails for the move

- Pure move first; no behavior change, tests stay green untouched.
- Keep `_HTTP_CLIENT` ownership in `proxy.py`; the pool must never `.close()`
  an `anthropic.Anthropic` built on it (the SDK's `close()` closes the shared
  httpx client).
- Keep single-writer file semantics (`pool.json` CLI, `pool-state.json` proxy).
- Re-run `tests/test_pool.py` and `tests/test_proxy.py` after the move, then the
  two-key live failover check in `docs/pool-implementation-plan.md` §Verification.
