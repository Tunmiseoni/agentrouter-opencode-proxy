#!/usr/bin/env python3
"""
agentrouter-proxy: thin reverse-proxy for agentrouter.org.

AgentRouter's Aliyun WAF fingerprints TLS handshakes AND inspects
Anthropic SDK-specific headers (x-stainless-*, user-agent). Raw httpx
is blocked, and the AsyncAnthropic client is also rejected because its
asyncio SSL implementation produces a different TLS fingerprint.

Only requests made through the Python sync `anthropic` SDK pass the
check.

This proxy keeps a single sync `anthropic.Anthropic` client (shared
connection pool, short keepalive_expiry to prevent zombie connections
when the upstream load balancer silently closes idle sockets).

Both the non-streaming and streaming paths offload to a thread via
asyncio.to_thread / a thread+queue so the FastAPI event loop stays free.

Long-request hardening (grill sessions can stream for many minutes):
- The custom httpx transport keeps the Anthropic SDK's TCP keepalive
  socket options (SO_KEEPALIVE + TCP keepalive timers) so NATs and load
  balancers don't silently drop idle upstream sockets.
- The streaming pump emits SSE comment heartbeats (``: keepalive``) on
  a timer while the upstream is silent, so downstream clients and any
  intermediate proxies see an alive connection. Per the SSE spec,
  comment lines are ignored by event parsers.
- Queue waits tick frequently so idle time is observable; only a real
  stall longer than CHUNK_TIMEOUT aborts the request.
- Downstream disconnects propagate: the worker is signalled and the
  upstream response is closed instead of leaking a thread.

Thinking history:
- AgentRouter runs reasoning models in "thinking mode": every assistant
  message containing a `tool_use` block must also carry a `thinking`
  block, or upstream rejects the request with "The `content[].thinking`
  in the thinking mode must be passed back to the API."
- @ai-sdk/anthropic drops reasoning parts lacking a provider signature,
  so unsigned tool-use turns arrive without their thinking block and trip
  that check. AgentRouter also cannot deserialize `redacted_thinking`.
- The proxy therefore drops `redacted_thinking` and injects a minimal
  empty `thinking` block into tool-use turns that lack one (THINKING_HISTORY,
  default `ensure`).

Usage:
    python proxy.py           # reads key from ~/.config/opencode/api_keys/AGENT_ROUTER_API_KEY
    AGENTROUTER_API_KEY=sk-... python proxy.py

The live model list is fetched from the AgentRouter dashboard API using
the session cookie + New-API-User id written by
`agentrouter-proxy set-cookie` (~/.config/opencode/api_keys/AGENT_ROUTER_COOKIE
and AGENT_ROUTER_USER_ID), overridable via AGENTROUTER_COOKIE and
AGENTROUTER_USER_ID. On failure the last good list, then a stub, is served.

Env knobs (all optional):
    PORT                 local listen port (default 7187)
    LOG_LEVEL            DEBUG|INFO|WARNING|ERROR (default INFO)
    CHUNK_TIMEOUT        max seconds of upstream silence before aborting (default 600)
    QUEUE_TICK           internal pump tick seconds (default 5)
    SSE_HEARTBEAT_S      downstream heartbeat interval seconds, 0 disables (default 20)
    CONNECT_TIMEOUT      httpx connect timeout seconds (default 15)
    WRITE_TIMEOUT        httpx write timeout seconds (default 60; large
                         grill-session bodies need this through the WAF)
    POOL_TIMEOUT         httpx pool timeout seconds (default 5)
    KEEPALIVE_EXPIRY     httpx keepalive expiry seconds (default 20)
    UPSTREAM_MAX_RETRIES Anthropic SDK max retries for initial send (default 2)
    MODELS_CACHE_TTL     seconds to cache the live model list (default 300)
    MODELS_TIMEOUT       dashboard model API timeout seconds (default 30;
                         cold WAF handshakes are slow, ~11s observed)
    THINKING_HISTORY     how to handle prior thinking blocks in request
                         history: ensure | strip | off (default ensure).
                         See "Thinking history" below.
    POOL_ENABLED         routing override: unset = auto (on when pool.json has
                         an enabled, anchored key), 1 = force on, 0 = kill switch
    POOL_REFRESH_S       usage-cache TTL / background refresh (default 120)
    POOL_RESERVE_USD     in-flight reservation per key (default 0.05)

The key pool spends across consenting friends' sk- keys using
the key-scoped, cookie-free `GET /v1/dashboard/billing/usage` counter. Remaining
is `anchor_remaining_usd - (usage_now_cents - anchor_usage_cents) / 100`. See
docs/pool-implementation-plan.md and docs/pool-operations.md.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import queue
import socket
import sys
import threading
import time
import traceback
import uuid
import warnings
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

# Python 3.14's asyncio still calls the deprecated
# asyncio.iscoroutinefunction alias internally (via run_in_executor);
# silence that stdlib-originated warning so logs stay useful.
warnings.filterwarnings("ignore", message=".*iscoroutinefunction.*")

# ── Config ────────────────────────────────────────────────────────────────────

TARGET = os.environ.get("AGENTROUTER_TARGET", "https://agentrouter.org")
PORT = int(os.environ.get("PORT", "7187"))
KEY_DIR = Path.home() / ".config/opencode/api_keys"
KEY_FILE = KEY_DIR / "AGENT_ROUTER_API_KEY"
# Dashboard credentials for the /api/user/* endpoints (model list, quota).
# Written by `agentrouter-proxy set-cookie`; the same files the CLI's
# quota command uses. Kept distinct from the sk- API key.
COOKIE_FILE = KEY_DIR / "AGENT_ROUTER_COOKIE"
USER_ID_FILE = KEY_DIR / "AGENT_ROUTER_USER_ID"
# Pool state (see docs/pool-implementation-plan.md):
#   pool.json       — CLI-owned: enabled + balance anchors. No secrets.
#   pool-state.json — proxy-owned: persisted dead marks. No secrets.
#   AGENTROUTER_FRIEND_<name> — one sk- key per friend, mode 600.
# One writer per file, so atomic tmp+rename needs no cross-process lock.
POOL_FILE = KEY_DIR / "pool.json"
POOL_STATE_FILE = KEY_DIR / "pool-state.json"
POOL_KEY_PREFIX = "AGENTROUTER_FRIEND_"
# Seconds to cache the upstream model list before re-fetching.
MODELS_CACHE_TTL = float(os.environ.get("MODELS_CACHE_TTL", "300"))
# Dashboard cold handshakes are slow through the WAF (~11s observed);
# keep this generous. The models route runs in a thread, so a slow
# first fetch never blocks the event loop.
MODELS_TIMEOUT = float(os.environ.get("MODELS_TIMEOUT", "30"))

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# Seconds of upstream silence before aborting a streaming request.
CHUNK_TIMEOUT = float(os.environ.get("CHUNK_TIMEOUT", "600"))
# Internal pump tick: how often the async side wakes up to check the
# queue / idle time / downstream disconnect. Small so idle time is
# observable; does NOT abort anything by itself.
QUEUE_TICK = float(os.environ.get("QUEUE_TICK", "5"))
# Downstream SSE heartbeat interval while upstream is silent.
# 0 disables. Heartbeats are SSE comments, ignored by event parsers.
SSE_HEARTBEAT_S = float(os.environ.get("SSE_HEARTBEAT_S", "20"))

CONNECT_TIMEOUT = float(os.environ.get("CONNECT_TIMEOUT", "15"))
# Write timeout must be generous: design/grill sessions re-upload the whole
# conversation history every turn (hundreds of KB+), and agentrouter's
# WAF/LB intermittently stalls large uploads for tens of seconds.
# Observed: a succeeding large upload took ~50s to drain+respond; three
# ~10s attempts all stalled -> user-visible APITimeoutError.
WRITE_TIMEOUT = float(os.environ.get("WRITE_TIMEOUT", "60"))
POOL_TIMEOUT = float(os.environ.get("POOL_TIMEOUT", "5"))
KEEPALIVE_EXPIRY = float(os.environ.get("KEEPALIVE_EXPIRY", "20"))
UPSTREAM_MAX_RETRIES = int(os.environ.get("UPSTREAM_MAX_RETRIES", "2"))

# ── Pool config ───────────────────────────────────────────────────────────────
#
# An sk-only key pool: one local proxy spends across N consenting friends'
# AgentRouter sk- keys, least-spent-first, and survives a key running dry.
# See docs/pool-implementation-plan.md.
#
# Routing is auto-enabled when pool.json holds at least one enabled, anchored
# key. Set POOL_ENABLED=1 to force it on (even with no keys yet) or
# POOL_ENABLED=0 to force it off (kill switch) regardless of pool.json.
POOL_ENABLED_ENV = os.environ.get("POOL_ENABLED")  # None = auto
# Usage-cache TTL and background refresh interval (seconds).
POOL_REFRESH_S = float(os.environ.get("POOL_REFRESH_S", "120"))
# In-flight reservation (USD) added to a key's score while a request is
# running, so two concurrent streams cannot double-pick the least-spent key.
POOL_RESERVE_USD = float(os.environ.get("POOL_RESERVE_USD", "0.05"))

# How to treat thinking/redacted_thinking content blocks in the request
# history before forwarding upstream.
#
# AgentRouter runs reasoning models (e.g. deepseek-v4-flash) in "thinking
# mode", where it requires every assistant message that contains a `tool_use`
# block to also carry a `thinking` block — otherwise it returns:
#   "The `content[].thinking` in the thinking mode must be passed back to the API."
# (Verified empirically: a tool_use turn without thinking 400s; text-only
# assistant turns do not need thinking. See README "Thinking history".)
#
# @ai-sdk/anthropic silently drops reasoning parts that have no provider
# `signature`, so unsigned tool-use turns arrive without their thinking block
# and trip that check. AgentRouter also cannot deserialize `redacted_thinking`
# (unknown variant), so those must never be forwarded.
#
#   ensure (default) drop redacted_thinking; inject an empty thinking block
#                    into tool_use turns that lack one; keep existing thinking
#   strip            drop all thinking + redacted_thinking blocks
#   off              pass history through untouched (legacy/debug)
THINKING_HISTORY = os.environ.get("THINKING_HISTORY", "ensure").strip().lower()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [proxy] %(message)s",
)
log = logging.getLogger("agentrouter-proxy")


def _socket_options() -> list:
    """TCP keepalive options mirroring the Anthropic SDK's default client.

    A custom httpx.Client otherwise loses these (httpx defaults set no
    socket options), letting NATs / load balancers silently drop idle
    upstream sockets during long reasoning pauses.
    """
    opts: list = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, True)]

    TCP_KEEPINTVL = getattr(socket, "TCP_KEEPINTVL", None)
    if TCP_KEEPINTVL is not None:
        opts.append((socket.IPPROTO_TCP, TCP_KEEPINTVL, 60))
    elif sys.platform == "darwin":
        TCP_KEEPALIVE = getattr(socket, "TCP_KEEPALIVE", 0x10)
        opts.append((socket.IPPROTO_TCP, TCP_KEEPALIVE, 60))

    TCP_KEEPCNT = getattr(socket, "TCP_KEEPCNT", None)
    if TCP_KEEPCNT is not None:
        opts.append((socket.IPPROTO_TCP, TCP_KEEPCNT, 5))

    TCP_KEEPIDLE = getattr(socket, "TCP_KEEPIDLE", None)
    if TCP_KEEPIDLE is not None:
        opts.append((socket.IPPROTO_TCP, TCP_KEEPIDLE, 60))

    return opts


def _build_http_client() -> httpx.Client:
    """httpx client with SDK-style keepalive transport + our timeouts.

    Must stay a *sync* httpx.Client: the WAF allowlists the sync TLS
    handshake; AsyncAnthropic / raw async transports are rejected.
    """
    transport_kwargs: dict = {"socket_options": _socket_options()}
    try:
        # Keep env-proxy support working even with a custom transport
        # (httpx won't auto-configure proxies once transport is set).
        from httpx._utils import get_environment_proxies  # type: ignore

        proxy_map = get_environment_proxies()
        if proxy_map:
            from httpx import HTTPTransport, Proxy

            mounts = {
                key: None if url is None else HTTPTransport(proxy=Proxy(url=url), **transport_kwargs)
                for key, url in proxy_map.items()
            }
            default_transport = __import__("httpx").HTTPTransport(**transport_kwargs)
            return httpx.Client(
                transport=default_transport,
                mounts=mounts,  # type: ignore[arg-type]
                limits=httpx.Limits(
                    max_connections=50,
                    max_keepalive_connections=5,
                    keepalive_expiry=KEEPALIVE_EXPIRY,
                ),
                timeout=httpx.Timeout(
                    connect=CONNECT_TIMEOUT,
                    read=CHUNK_TIMEOUT,
                    write=WRITE_TIMEOUT,
                    pool=POOL_TIMEOUT,
                ),
                follow_redirects=True,
            )
    except Exception as exc:
        log.warning("proxy-mount setup skipped (%s); using plain keepalive transport", exc)

    return httpx.Client(
        transport=httpx.HTTPTransport(**transport_kwargs),
        limits=httpx.Limits(
            max_connections=50,
            max_keepalive_connections=5,
            keepalive_expiry=KEEPALIVE_EXPIRY,
        ),
        timeout=httpx.Timeout(
            connect=CONNECT_TIMEOUT,
            read=CHUNK_TIMEOUT,
            write=WRITE_TIMEOUT,
            pool=POOL_TIMEOUT,
        ),
        follow_redirects=True,
    )


_HTTP_CLIENT = _build_http_client()


def _api_key() -> str:
    k = os.environ.get("AGENTROUTER_API_KEY", "").strip()
    if not k and KEY_FILE.exists():
        k = KEY_FILE.read_text().strip()
    if not k:
        raise RuntimeError(
            "No AGENTROUTER_API_KEY. "
            f"Set env var or create {KEY_FILE}"
        )
    return k


_anthropic_client: anthropic.Anthropic | None = None


def _client() -> anthropic.Anthropic:
    """Return the module-level sync client singleton (connection pool reused across requests)."""
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(
            api_key=_api_key(),
            base_url=TARGET,
            http_client=_HTTP_CLIENT,
            max_retries=UPSTREAM_MAX_RETRIES,
        )
    return _anthropic_client


# ── Key pool (sk--only, least-spent-first) ─────────────────────────────────────
#
# A key yields *spend*, not remaining: `GET /v1/dashboard/billing/usage` is an
# authoritative, monotonic, key-scoped cumulative counter in cents (discovered
# cookie-free; see docs/pool-implementation-plan.md). Remaining is derived from
# a one-time human anchor plus usage deltas:
#
#   remaining_usd(key) = anchor_remaining_usd
#                      - (usage_now_cents - anchor_usage_cents) / 100
#
# Routing picks the highest remaining (least spent) usable key, reserves a
# small in-flight budget so concurrent requests don't double-pick, and fails
# over on 402/429/503 (and 401, which disables the key).
#
# State lives in two single-writer files so atomic tmp+rename needs no
# cross-process lock: pool.json (CLI: anchors/enabled) and pool-state.json
# (proxy: dead marks). A dead mark is honoured only while its date is later
# than `recalibrated`, so re-anchoring re-enables a key automatically.
#
# Concurrency: one proxy instance (documented assumption). Registry mutations
# are guarded by `_pool_lock`; the in-flight reservation is added synchronously
# at pick time (in the event loop) before the request is dispatched.

_POOL_FAILOVER_STATUS = {402, 429, 503}


class _PoolExhausted(RuntimeError):
    """No pool key is usable (all disabled, dead, unanchored, or missing)."""


class _PoolUpfrontFailure(Exception):
    """Upstream failed before any byte was forwarded; safe to try the next key."""

    def __init__(self, exc: BaseException):
        super().__init__(str(exc))
        self.exc = exc


_pool_lock = threading.Lock()
# name -> (key fingerprint, anthropic.Anthropic); never .close()d (shared _HTTP_CLIENT).
_pool_clients: dict[str, tuple[str, anthropic.Anthropic]] = {}
# name -> {"cents": float | None, "at": monotonic, "unknown": bool}
_pool_usage: dict[str, dict] = {}
# name -> reserved USD while its request is in flight
_pool_in_flight: dict[str, float] = {}
# Config / dead state, rebuilt from disk by _pool_reload.
_pool_order: list[str] = []
_pool_meta: dict[str, dict] = {}
_pool_dead: dict[str, str] = {}
_pool_mtime: float = -1.0
# Members injected by the proxy rather than pool.json (currently just "you",
# whose remaining comes live from the dashboard cookie). Rebuilt by _pool_reload.
_pool_virtual: set[str] = set()
# Operator's own account balance, sourced from the cookie (/api/user/self).
_you_quota: dict = {"usd": None, "at": 0.0, "ok_at": 0.0, "unknown": False, "stale": False}


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON via tmp + rename, mode 600, so readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log.warning("pool: ignoring unreadable %s (%s)", path.name, type(exc).__name__)
        return {}
    return data if isinstance(data, dict) else {}


def _key_fingerprint(key: str) -> str:
    """Short digest of key material, used only to detect rotation (never logged as the key)."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _pool_key_file(name: str) -> Path:
    return KEY_DIR / f"{POOL_KEY_PREFIX}{name}"


def _key_material(name: str) -> str | None:
    """Read a key's sk- material fresh from disk (or env/KEY_FILE for ``you``).

    Never logged. Returns None when the key is not configured.
    """
    if name == "you":
        try:
            return _api_key()
        except RuntimeError:
            return None
    path = _pool_key_file(name)
    try:
        path.stat()
    except FileNotFoundError:
        return None
    try:
        return path.read_text().strip() or None
    except OSError as exc:
        log.warning("pool: unreadable key file for %s (%s)", name, type(exc).__name__)
        return None


def _warn_key_permissions(name: str) -> None:
    """Warn (do not fail) when a key file is group/other readable."""
    path = _pool_key_file(name) if name != "you" else KEY_FILE
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & 0o077:
        log.warning(
            "pool: %s is mode %o; run: chmod 600 %s",
            path.name, mode & 0o777, path,
        )


def _parse_dt(value) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _pool_reload(force: bool = False) -> None:
    """(Re)load pool.json + pool-state.json when pool.json changed on disk.

    Cheap enough to call at pick time, so CLI edits (pool-add/forget/
    recalibrate) take effect without a proxy restart. Also injects the
    operator's own account as a virtual "you" member when friends are pooled
    and "you" is not explicitly configured.
    """
    global _pool_mtime, _pool_order, _pool_meta, _pool_dead, _pool_virtual
    try:
        mtime = POOL_FILE.stat().st_mtime
    except FileNotFoundError:
        mtime = 0.0
    if not force and mtime == _pool_mtime:
        return
    with _pool_lock:
        _pool_mtime = mtime
        _pool_meta = {
            str(name): rec for name, rec in _load_json(POOL_FILE).items()
            if isinstance(rec, dict)
        }
        state = _load_json(POOL_STATE_FILE)
        _pool_dead = {
            str(name): str(rec.get("dead_at") or "")
            for name, rec in state.items()
            if isinstance(rec, dict) and rec.get("dead")
        }
        _pool_order = list(_pool_meta.keys())
        _pool_virtual = set()
        # Auto-include the operator's own key once anything else is pooled, unless
        # an explicit "you" entry (e.g. from `pool-recalibrate you`) says otherwise.
        if _pool_meta and "you" not in _pool_meta and _key_material("you"):
            _pool_order.append("you")
            _pool_virtual.add("you")
        # Drop cached clients/usage for keys that are gone or disabled, so
        # `pool-forget` evicts without a restart. Virtual members are kept.
        for stale in [
            n for n in _pool_clients
            if n not in _pool_virtual and (n not in _pool_meta or not _is_enabled(n))
        ]:
            _pool_clients.pop(stale, None)
            _pool_usage.pop(stale, None)


def _key_record(name: str) -> dict:
    rec = _pool_meta.get(name)
    return rec if isinstance(rec, dict) else {}


def _is_enabled(name: str) -> bool:
    return bool(_key_record(name).get("enabled", True))


def _is_dead(name: str) -> bool:
    """A dead mark applies only until a later recalibration (re-anchor = re-enable)."""
    dead = _parse_dt(_pool_dead.get(name))
    if dead is None:
        return False
    recal = _parse_dt(_key_record(name).get("recalibrated"))
    if recal is None:
        return True
    return dead.date() > recal.date()


def _anchor(name: str) -> tuple[float | None, float | None]:
    """Return (anchor_remaining_usd, anchor_usage_cents) or (None, None) if unanchored."""
    rec = _key_record(name)
    try:
        rem = float(rec["anchor_remaining_usd"]) if rec.get("anchor_remaining_usd") is not None else None
        used = float(rec["anchor_usage_cents"]) if rec.get("anchor_usage_cents") is not None else None
    except (TypeError, ValueError):
        return None, None
    return rem, used


def _remaining(name: str) -> float | None:
    """Live remaining USD from anchor + usage delta, or None if unscoreable.

    Uses the last known usage reading even if the latest refresh failed, so a
    transient accounting failure cannot black out routing. Only a key that has
    never produced a reading is unscoreable. The virtual "you" member is sourced
    from the dashboard cookie quota instead.
    """
    if name in _pool_virtual:
        if _you_quota.get("usd") is None:
            return None
        return float(_you_quota["usd"])
    rem, anchor_usage = _anchor(name)
    if rem is None or anchor_usage is None:
        return None
    cents = (_pool_usage.get(name) or {}).get("cents")
    if cents is None:
        return None
    return rem - (float(cents) - anchor_usage) / 100.0


def _fetch_usage_cents(api_key: str) -> float:
    """Authoritative account-cumulative spend for one key, in cents.

    Cookie-free and key-scoped; the dashboard billing path is not behind the
    WAF's SDK fingerprint check. An explicit wide date range is used because
    the no-params form was once observed cached (the number is account-wide and
    date params are ignored).
    """
    today = datetime.now(timezone.utc).date().isoformat()
    resp = _HTTP_CLIENT.get(
        f"{TARGET}/v1/dashboard/billing/usage",
        params={"start_date": "1970-01-01", "end_date": today},
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        timeout=MODELS_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    total = payload.get("total_usage")
    if total is None:
        raise RuntimeError(f"usage API missing total_usage: {str(payload)[:200]}")
    return float(total)


def _refresh_usage(name: str) -> None:
    """Refresh one key's cached usage.

    A failed refresh keeps the last known value (marked ``stale``) so routing
    and the displayed remaining stay usable; the key only becomes ``unknown``
    when it has never produced a reading.
    """
    key = _key_material(name)
    if not key:
        return
    now = time.monotonic()
    try:
        cents: float | None = _fetch_usage_cents(key)
    except Exception as exc:
        log.warning("pool: usage refresh failed key=%s (%s)", name, type(exc).__name__)
        with _pool_lock:
            prev = _pool_usage.get(name) or {}
            had = prev.get("cents")
            _pool_usage[name] = {
                "cents": had,
                "at": now,
                "ok_at": prev.get("ok_at", 0.0),
                "unknown": had is None,
                "stale": had is not None,
            }
        return
    with _pool_lock:
        _pool_usage[name] = {
            "cents": float(cents),
            "at": now,
            "ok_at": now,
            "unknown": False,
            "stale": False,
        }


def _fetch_you_quota_usd() -> float:
    """Operator's own remaining USD from the dashboard cookie (``/api/user/self``).

    The key-scoped billing counter yields spend, not remaining, so the operator's
    own balance comes from the cookie-based quota. That dashboard path is
    intermittently behind an Aliyun captcha challenge, so a non-JSON body is
    reported as such rather than crashing the refresher.
    """
    user_id, cookie = _dashboard_credentials()
    if not user_id or not cookie:
        raise RuntimeError("no dashboard credentials; run: agentrouter-proxy set-cookie")
    resp = _HTTP_CLIENT.get(
        f"{TARGET}/api/user/self",
        headers={
            "Accept": "application/json, text/plain, */*",
            "New-API-User": user_id,
            "Cookie": cookie,
            "Referer": "https://agentrouter.org/console/token",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/152.0.0.0 Safari/537.36"
            ),
        },
        timeout=MODELS_TIMEOUT,
    )
    resp.raise_for_status()
    ctype = resp.headers.get("content-type", "")
    if "json" not in ctype.lower():
        raise RuntimeError(
            f"dashboard returned {ctype or 'non-JSON'} for /api/user/self "
            "(WAF challenge?); own balance unavailable"
        )
    try:
        payload = resp.json()
    except ValueError as exc:
        raise RuntimeError("dashboard returned invalid JSON (WAF challenge?)") from exc
    if not isinstance(payload, dict) or not payload.get("success", False):
        raise RuntimeError(f"quota API error: {str(payload)[:200]}")
    quota = (payload.get("data") or {}).get("quota")
    if quota is None:
        raise RuntimeError("quota API returned no quota (cookie expired?)")
    return float(quota) / 500_000


def _refresh_you() -> None:
    """Refresh the operator's own cookie-sourced balance; keep last-good on error."""
    now = time.monotonic()
    try:
        usd: float | None = _fetch_you_quota_usd()
    except Exception as exc:
        log.warning("pool: own-account quota refresh failed (%s)", type(exc).__name__)
        with _pool_lock:
            had = _you_quota.get("usd")
            _you_quota["usd"] = had
            _you_quota["at"] = now
            _you_quota["unknown"] = had is None
            _you_quota["stale"] = had is not None
        return
    with _pool_lock:
        _you_quota["usd"] = usd
        _you_quota["at"] = now
        _you_quota["ok_at"] = now
        _you_quota["unknown"] = False
        _you_quota["stale"] = False


def _refresh_member(name: str) -> None:
    if name in _pool_virtual:
        _refresh_you()
    else:
        _refresh_usage(name)


def _refresh_all_usage() -> None:
    _pool_reload(force=True)
    for name in list(_pool_order):
        _refresh_member(name)


def _maybe_refresh(name: str | None) -> None:
    """Nudge a background refresh when a key's cached reading is stale."""
    if not name:
        return
    if name in _pool_virtual:
        if _you_quota.get("at") and time.monotonic() - _you_quota["at"] < POOL_REFRESH_S:
            return
    else:
        usage = _pool_usage.get(name)
        if usage and time.monotonic() - usage.get("at", 0.0) < POOL_REFRESH_S:
            return
    threading.Thread(
        target=_refresh_member, args=(name,), daemon=True, name=f"ar-usage-{name}"
    ).start()


def _env_flag(value: str | None) -> bool | None:
    """Parse a tri-state env flag: None when unset/empty, else a bool."""
    if value is None or not str(value).strip():
        return None
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _has_anchor(name: str) -> bool:
    rem, anchor_usage = _anchor(name)
    return rem is not None and anchor_usage is not None


def _pool_routing_enabled() -> bool:
    """Resolved routing state.

    An explicit ``POOL_ENABLED`` always wins (0 = kill switch, 1 = force on).
    Otherwise routing auto-enables as soon as pool.json holds at least one
    enabled, anchored key (or a virtual member such as the operator's own key),
    so adding a key is enough to activate the pool.
    """
    forced = _env_flag(POOL_ENABLED_ENV)
    if forced is not None:
        return forced
    _pool_reload()
    return any(
        _is_enabled(n) and (n in _pool_virtual or _has_anchor(n))
        for n in _pool_order
    )


def _client_for(name: str | None) -> anthropic.Anthropic:
    """Pooled client for `name`, or the primary singleton when not pooling.

    Every client shares the process-wide ``_HTTP_CLIENT`` (the WAF allowlists
    the sync TLS stack), so an evicted client must never be ``.close()``d —
    that would tear down the shared transport for every key.
    """
    if name is None or not _pool_routing_enabled():
        return _client()
    key = _key_material(name)
    if not key:
        raise RuntimeError(f"pool: no key material for {name}")
    fp = _key_fingerprint(key)
    with _pool_lock:
        entry = _pool_clients.get(name)
        if entry is not None and entry[0] == fp:
            return entry[1]
    client = anthropic.Anthropic(
        api_key=key,
        base_url=TARGET,
        http_client=_HTTP_CLIENT,
        max_retries=UPSTREAM_MAX_RETRIES,
    )
    with _pool_lock:
        entry = _pool_clients.get(name)
        if entry is not None and entry[0] == fp:
            return entry[1]
        _pool_clients[name] = (fp, client)
        return client


def _pool_active() -> bool:
    if not _pool_routing_enabled():
        return False
    _pool_reload()
    return bool(_pool_order)


def _pick(exclude: set[str]) -> tuple[str | None, float]:
    """Pick the least-spent usable key, reserving in-flight budget on it.

    Returns ``(name, reservation_usd)``; ``(None, 0.0)`` when nothing is
    usable. The reservation is added before returning, so a concurrent
    request cannot select the same key.
    """
    _pool_reload()
    best: str | None = None
    best_score = float("-inf")
    with _pool_lock:
        for name in _pool_order:
            if name in exclude or not _is_enabled(name) or _is_dead(name):
                continue
            remaining = _remaining(name)
            if remaining is None:
                continue
            if _key_material(name) is None:
                log.warning("pool: key file missing key=%s; skipping", name)
                continue
            score = remaining - _pool_in_flight.get(name, 0.0)
            # Strict >: the first configured key wins ties (fixed config order).
            if score > best_score:
                best, best_score = name, score
        if best is not None:
            _pool_in_flight[best] = _pool_in_flight.get(best, 0.0) + POOL_RESERVE_USD
    return (best, POOL_RESERVE_USD) if best is not None else (None, 0.0)


def _release(name: str | None, amount: float) -> None:
    if not name:
        return
    with _pool_lock:
        left = _pool_in_flight.get(name, 0.0) - amount
        if left <= 0:
            _pool_in_flight.pop(name, None)
        else:
            _pool_in_flight[name] = left


def _mark_dead(name: str, status: int) -> None:
    """Persist a dead mark for a key the upstream rejected (401).

    Cleared by a later ``pool-recalibrate`` (or ``pool-forget``) without the
    CLI having to edit this proxy-owned file.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _pool_lock:
        if _pool_dead.get(name):
            return
        _pool_dead[name] = now
        state = _load_json(POOL_STATE_FILE)
        rec = state.get(name) if isinstance(state.get(name), dict) else {}
        state[name] = {**rec, "dead": True, "dead_at": now, "last_status": status}
        try:
            _atomic_write_json(POOL_STATE_FILE, state)
        except Exception as exc:
            log.warning("pool: could not persist dead mark key=%s (%s)", name, type(exc).__name__)
    log.warning("pool: key=%s marked dead (upstream status=%s)", name, status)


def _pool_status(refresh: bool = False) -> dict:
    """Local-only pool status: per-key balance/state, no secrets."""
    _pool_reload(force=True)
    names = list(_pool_order)
    if refresh:
        for name in names:
            _refresh_member(name)

    per_key: dict[str, dict] = {}
    total_remaining = 0.0
    usage_unknown_count = 0
    for name in names:
        virtual = name in _pool_virtual
        rem_anchor, anchor_usage = _anchor(name)
        usage = _pool_usage.get(name) or {}
        unanchored = (not virtual) and (rem_anchor is None or anchor_usage is None)
        if virtual:
            unknown = _you_quota.get("usd") is None
            stale = bool(_you_quota.get("stale"))
        else:
            unknown = (not unanchored) and usage.get("cents") is None
            stale = bool(usage.get("stale"))
        remaining = _remaining(name)
        spent = None
        if not virtual and not unanchored and usage.get("cents") is not None:
            spent = max(0.0, (float(usage["cents"]) - anchor_usage) / 100.0)
        if unknown:
            usage_unknown_count += 1
        if remaining is not None:
            total_remaining += remaining
        per_key[name] = {
            "remaining": None if remaining is None else round(remaining, 4),
            "spent": None if spent is None else round(spent, 4),
            "enabled": _is_enabled(name),
            "dead": _is_dead(name),
            "unanchored": unanchored,
            "virtual": virtual,
            "stale": stale,
            "recalibrated": _key_record(name).get("recalibrated"),
            "usage_unknown": unknown,
        }
    return {
        "enabled": _pool_routing_enabled(),
        "configured": len(names),
        "per_key": per_key,
        "total_remaining": round(total_remaining, 4),
        "usage_unknown_count": usage_unknown_count,
    }


def _pool_startup_log() -> None:
    """Log an enrollment summary at startup without leaking key material."""
    _pool_reload(force=True)
    names = list(_pool_order)
    forced = _env_flag(POOL_ENABLED_ENV)
    if not names:
        if forced:
            log.warning(
                "pool: POOL_ENABLED=1 but %s has no keys; staying on the single-key path",
                POOL_FILE.name,
            )
        else:
            log.info("pool: no keys configured; single-key path")
        return
    missing = [n for n in names if _key_material(n) is None]
    usable, unanchored, disabled, virtual = [], [], [], []
    for name in names:
        if name in missing:
            continue
        if not _is_enabled(name):
            disabled.append(name)
            continue
        if name in _pool_virtual:
            virtual.append(name)
            continue
        rem, anchor_usage = _anchor(name)
        (unanchored if rem is None or anchor_usage is None else usable).append(name)
    log.info(
        "pool: routing=%s keys=%s usable=%s self=%s disabled=%s missing=%s",
        "on" if _pool_routing_enabled() else "off",
        names, usable, virtual, disabled, missing,
    )
    if unanchored:
        log.warning(
            "pool: un-anchored keys excluded from routing: %s (run: agentrouter-proxy pool-recalibrate <name> <dollars>)",
            unanchored,
        )
    for name in names:
        if name not in missing:
            _warn_key_permissions(name)


def _is_pool_failover_exc(exc: BaseException) -> bool:
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code == 401 or exc.status_code in _POOL_FAILOVER_STATUS
    return False


async def _resolve_pool_key(excluded: set[str], refresh: bool) -> tuple[str | None, float]:
    """Pick a usable key, refreshing the usage cache once if it comes up empty.

    A cold or freshly-failed cache can leave every key unscoreable; one
    synchronous refresh recovers the common case. Callers fall back to the
    primary key when this still returns nothing, so a transient accounting gap
    never turns into an outage.
    """
    name, reservation = _pick(excluded)
    if name is not None or not refresh:
        return name, reservation
    await asyncio.to_thread(_refresh_all_usage)
    return _pick(excluded)


async def _create_with_failover(kw: dict, rid: str):
    """Non-streaming messages.create across pool keys, least-spent-first.

    Returns ``(message, pool_key)`` with ``pool_key=None`` when pooling is off or
    when the pool had no usable key and the primary key was used as a fallback.
    Raises the last upstream error only when no fallback exists.
    """
    if not _pool_active():
        msg = await asyncio.to_thread(lambda: _client().messages.create(**kw))
        return msg, None

    excluded: set[str] = set()
    last_exc: BaseException | None = None
    first = True
    while True:
        name, reservation = await _resolve_pool_key(excluded, refresh=first)
        first = False
        if name is None:
            _release(name, reservation)
            if _key_material("you"):
                log.warning(
                    "rid=%s pool exhausted%s; falling back to primary key",
                    rid,
                    f" (last status {getattr(last_exc, 'status_code', None)})" if last_exc else "",
                )
                msg = await asyncio.to_thread(lambda: _client().messages.create(**kw))
                return msg, None
            if last_exc is not None:
                raise last_exc
            raise _PoolExhausted(
                "no usable pool key: all disabled, dead, unanchored, or missing"
            )
        try:
            msg = await asyncio.to_thread(
                lambda n=name: _client_for(n).messages.create(**kw)
            )
        except anthropic.APIStatusError as e:
            _release(name, reservation)
            if e.status_code == 401:
                _mark_dead(name, e.status_code)
            elif e.status_code in _POOL_FAILOVER_STATUS:
                log.warning(
                    "rid=%s pool key=%s status=%s -> failover", rid, name, e.status_code
                )
            else:
                raise
            excluded.add(name)
            last_exc = e
            continue
        except BaseException:
            _release(name, reservation)
            raise
        _release(name, reservation)
        _maybe_refresh(name)
        return msg, name


# ── Model list (dashboard API) ─────────────────────────────────────────────────

# Fallback served when the live list is unavailable and nothing is cached.
# Keep in sync manually; the live source is the dashboard API below.
_STUB_MODELS = [
    "claude-opus-5",
    "claude-opus-4-8",
    "gpt-5.6-sol",
    "deepseek-v4-flash",
    "gpt-6-astra",
]

# Last successful live list + when it was fetched (monotonic seconds).
_models_cache: dict = {"data": None, "at": 0.0}


def _dashboard_credentials() -> tuple[str, str]:
    """Return (user_id, cookie) for the dashboard API.

    Env vars win so the proxy can run without the CLI's files; otherwise
    reuse the files written by `agentrouter-proxy set-cookie`. Read fresh
    each call so a cookie refresh needs no proxy restart.
    """
    user_id = os.environ.get("AGENTROUTER_USER_ID", "").strip()
    cookie = os.environ.get("AGENTROUTER_COOKIE", "").strip()
    if not user_id and USER_ID_FILE.exists():
        user_id = USER_ID_FILE.read_text().strip()
    if not cookie and COOKIE_FILE.exists():
        cookie = COOKIE_FILE.read_text().strip()
    return user_id, cookie


def _fetch_models() -> list[str]:
    """Fetch the live model ids from the dashboard API.

    The dashboard endpoints aren't behind the WAF's TLS/SDK fingerprint
    check (only the /messages LLM path is), so plain httpx works here.
    Raises on missing credentials or a non-success upstream response.
    """
    user_id, cookie = _dashboard_credentials()
    if not user_id or not cookie:
        raise RuntimeError(
            "no dashboard credentials; run: agentrouter-proxy set-cookie"
        )

    resp = _HTTP_CLIENT.get(
        f"{TARGET}/api/user/models",
        headers={
            "Accept": "application/json, text/plain, */*",
            "New-API-User": user_id,
            "Cookie": cookie,
            "Referer": "https://agentrouter.org/console/token",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/152.0.0.0 Safari/537.36"
            ),
        },
        timeout=MODELS_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("success", False):
        raise RuntimeError(f"upstream model API error: {str(payload)[:200]}")

    data = payload.get("data") or []
    models = [m for m in data if isinstance(m, str) and m]
    if not models:
        raise RuntimeError("upstream model API returned an empty list")
    return models


def _models() -> list[str]:
    """Live model list with a TTL cache and stub fallback."""
    now = time.monotonic()
    cached = _models_cache["data"]
    if cached is not None and now - _models_cache["at"] < MODELS_CACHE_TTL:
        return cached

    try:
        models = _fetch_models()
    except Exception as exc:
        if cached is not None:
            log.warning("models fetch failed (%s); serving last-good cache", exc)
            # Serve stale rather than fail; retry on the next TTL window.
            _models_cache["at"] = now
            return cached
        log.warning("models fetch failed (%s); serving stub list", exc)
        return _STUB_MODELS

    _models_cache["data"] = models
    _models_cache["at"] = now
    return models


# ── Request translation ───────────────────────────────────────────────────────

_SKIP = {"stream"}                     # handled separately
_STRIP = {"thinking", "output_config"}  # non-standard; trigger agentrouter content filter
_THINKING_BLOCK_TYPES = {"thinking", "redacted_thinking"}


def _normalize_thinking_blocks(messages, mode: str) -> tuple[list, int, int]:
    """Make request history satisfy AgentRouter's thinking-mode schema.

    Rules (verified empirically, see README "Thinking history"):

    - ``redacted_thinking`` is not a recognised content-block variant
      upstream, so it is always dropped (deserialization would 400).
    - In thinking mode, every assistant message containing a ``tool_use``
      block must also carry a ``thinking`` block, or upstream returns
      "The `content[].thinking` in the thinking mode must be passed back to
      the API." A minimal ``{"type": "thinking", "thinking": ""}`` is enough;
      the ``signature`` field is optional.
    - Plain text-only assistant turns do not need a thinking block.

    Modes:
        ``ensure`` (default) inject where missing, keep existing thinking
        ``strip``  drop all thinking blocks
        ``off``    leave history untouched

    Returns ``(messages, injected, dropped)``. Non-list content (strings,
    ``None``) and non-dict blocks are passed through untouched.
    """
    if mode == "off" or not isinstance(messages, list):
        return messages, 0, 0

    out: list = []
    injected = 0
    dropped = 0

    for msg in messages:
        if not isinstance(msg, dict):
            out.append(msg)
            continue

        content = msg.get("content")
        if not isinstance(content, list):
            out.append(msg)
            continue

        had_tool_use = False
        had_thinking = False
        new_content: list = []

        for block in content:
            if isinstance(block, dict) and block.get("type") in _THINKING_BLOCK_TYPES:
                if block.get("type") == "redacted_thinking" or mode == "strip":
                    dropped += 1
                    continue
                # Upstream requires the `thinking` field to be present (an
                # empty string is accepted), so normalise a missing one.
                if not isinstance(block.get("thinking"), str):
                    block = {**block, "thinking": ""}
                had_thinking = True
                new_content.append(block)
                continue

            if isinstance(block, dict) and block.get("type") == "tool_use":
                had_tool_use = True
            new_content.append(block)

        if mode == "ensure" and had_tool_use and not had_thinking:
            new_content.insert(0, {"type": "thinking", "thinking": ""})
            injected += 1

        # Drop a message that had content but no longer has any blocks.
        if not new_content and content:
            continue

        new_msg = dict(msg)
        new_msg["content"] = new_content
        out.append(new_msg)

    return out, injected, dropped


def _kwargs(body: dict) -> dict:
    """Forward all fields except stream (handled separately) and non-standard extras."""
    kw = {k: v for k, v in body.items() if k not in _SKIP and k not in _STRIP}
    msgs, injected, dropped = _normalize_thinking_blocks(kw.get("messages"), THINKING_HISTORY)
    if injected or dropped:
        kw["messages"] = msgs
        log.info(
            "thinking-history mode=%s injected=%d dropped=%d",
            THINKING_HISTORY, injected, dropped,
        )
    return kw


def _exc_chain(exc: BaseException) -> str:
    """One-line cause chain, e.g. APITimeoutError <- ReadTimeout."""
    parts = [type(exc).__name__]
    seen = 0
    cur = exc.__cause__ or exc.__context__
    while cur is not None and seen < 4:
        parts.append(type(cur).__name__)
        cur = cur.__cause__ or cur.__context__
        seen += 1
    return " <- ".join(parts)


def _exc_phase(exc: BaseException) -> str:
    """Human-readable failure phase from the cause chain.

    Lets the next incident explain itself: an upload stall (large body,
    slow WAF ingest) reads very differently from a mid-stream stall.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen and len(seen) < 6:
        seen.add(id(cur))
        if isinstance(cur, httpx.WriteTimeout):
            return "while uploading the request (large body or slow upstream ingest?)"
        if isinstance(cur, httpx.ReadTimeout):
            return "while waiting for/reading the upstream response"
        if isinstance(cur, httpx.ConnectTimeout):
            return "while connecting upstream"
        if isinstance(cur, httpx.PoolTimeout):
            return "while waiting for a pooled connection"
        if isinstance(cur, httpx.ConnectError):
            return "connecting upstream"
        cur = cur.__cause__ or cur.__context__
    return "talking to upstream"


# ── Streaming helper ──────────────────────────────────────────────────────────

def _stream_worker(
    kw: dict,
    client: anthropic.Anthropic,
    q: queue.Queue,
    stop: threading.Event,
    handle: dict,
    rid: str,
    model: str,
    pool_key: str | None = None,
) -> None:
    """
    Run inside a thread. Uses the sync Anthropic SDK's with_streaming_response
    to get raw SSE bytes and puts them into the queue, stripping any
    non-standard event types (e.g. billing_summary) that break OpenCode's parser.

    `client` is resolved by the caller (pooled or the primary singleton).
    `stop` is set by the async side on downstream disconnect / stall abort;
    `handle` shares the live response object so the async side can close it
    to unblock a stuck socket read.
    """
    SKIP_EVENTS: set[bytes] = {b"billing_summary"}

    t0 = time.monotonic()
    first_at: float | None = None
    chunks = 0
    bytes_out = 0
    log.info("rid=%s upstream start model=%s pool_key=%s", rid, model, pool_key or "-")

    try:
        with client.messages.with_streaming_response.create(**kw) as resp:
            handle["resp"] = resp
            if stop.is_set():
                return
            log.info(
                "rid=%s upstream headers status=%s ttfb=%.1fs",
                rid, resp.status_code, time.monotonic() - t0,
            )

            buf = b""
            skip_block = False
            last_log = time.monotonic()

            for raw_chunk in resp.iter_bytes(chunk_size=1024):
                if stop.is_set():
                    log.info("rid=%s worker stopping after %d chunks", rid, chunks)
                    return
                if raw_chunk:
                    if first_at is None:
                        first_at = time.monotonic()
                    chunks += 1
                    bytes_out += len(raw_chunk)

                buf += raw_chunk

                while True:
                    nl = buf.find(b"\n")
                    if nl == -1:
                        break
                    line = buf[: nl + 1]   # include \n
                    buf = buf[nl + 1:]
                    stripped = line.rstrip(b"\r\n")

                    if stripped.startswith(b"event:"):
                        event_name = stripped[6:].strip()
                        skip_block = event_name in SKIP_EVENTS
                        if skip_block:
                            if log.isEnabledFor(logging.DEBUG):
                                log.debug("rid=%s filtered event=%s", rid, event_name.decode("ascii", "replace"))
                            continue
                    elif skip_block:
                        if stripped == b"":
                            skip_block = False  # blank line ends the event block
                        continue

                    q.put(line)

                now = time.monotonic()
                if now - last_log >= 30 and chunks:
                    log.info(
                        "rid=%s upstream active chunks=%d bytes=%d elapsed=%.0fs",
                        rid, chunks, bytes_out, now - t0,
                    )
                    last_log = now

            if buf:
                q.put(buf)

    except Exception as exc:
        log.warning(
            "rid=%s upstream exc=%s msg=%.200s elapsed=%.1fs chunks=%d\n%s",
            rid, _exc_chain(exc), str(exc),
            time.monotonic() - t0, chunks,
            "".join(traceback.format_exception(exc))[-2000:],
        )
        q.put(exc)
    else:
        log.info(
            "rid=%s upstream done chunks=%d bytes=%d elapsed=%.1fs",
            rid, chunks, bytes_out, time.monotonic() - t0,
        )
    finally:
        handle.pop("resp", None)
        q.put(None)  # sentinel


# Sentinel for "queue wait timed out, no data yet" (internal pump tick).
_TICK = object()


async def _stream_attempt(
    kw: dict,
    request: Request,
    rid: str,
    model: str,
    pool_key: str | None,
    loop,
    t0: float,
):
    """One upstream streaming attempt, pumped to the client.

    Failover is only safe *before* any byte reaches the client, so an upfront
    failover status is re-raised as ``_PoolUpfrontFailure`` only while
    ``forwarded == 0`` and ``pool_key`` is set (pooled); otherwise the error
    propagates exactly as before.
    """
    client = _client_for(pool_key)
    q: queue.Queue = queue.Queue()
    stop = threading.Event()
    handle: dict = {}
    t = threading.Thread(
        target=_stream_worker,
        args=(kw, client, q, stop, handle, rid, model, pool_key),
        daemon=True,
        name=f"ar-stream-{rid}",
    )
    t.start()
    last_upstream = time.monotonic()
    last_heartbeat = last_upstream
    forwarded = 0
    heartbeats = 0

    def _close_upstream() -> None:
        resp = handle.pop("resp", None)
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass

    try:
        while True:
            if await request.is_disconnected():
                log.info(
                    "rid=%s downstream disconnected after %.1fs forwarded=%d; stopping worker",
                    rid, time.monotonic() - t0, forwarded,
                )
                stop.set()
                await loop.run_in_executor(None, _close_upstream)
                break

            try:
                chunk = await loop.run_in_executor(None, q.get, True, QUEUE_TICK)
            except queue.Empty:
                chunk = _TICK

            now = time.monotonic()

            if chunk is _TICK:
                # No upstream data within QUEUE_TICK: check stall budget,
                # maybe heartbeat, then loop (disconnect is checked at top).
                idle = now - last_upstream
                if idle >= CHUNK_TIMEOUT:
                    log.warning(
                        "rid=%s upstream stalled idle=%.0fs elapsed=%.0fs forwarded=%d — aborting",
                        rid, idle, now - t0, forwarded,
                    )
                    stop.set()
                    await loop.run_in_executor(None, _close_upstream)
                    raise TimeoutError(
                        f"No chunk received from agentrouter.org in {CHUNK_TIMEOUT:g}s "
                        f"(idle {idle:.0f}s, elapsed {now - t0:.0f}s, forwarded {forwarded}) — upstream stalled"
                    )
                if (
                    SSE_HEARTBEAT_S > 0
                    and now - last_heartbeat >= SSE_HEARTBEAT_S
                    and idle >= SSE_HEARTBEAT_S
                ):
                    last_heartbeat = now
                    heartbeats += 1
                    if log.isEnabledFor(logging.DEBUG):
                        log.debug("rid=%s heartbeat #%d idle=%.0fs", rid, heartbeats, idle)
                    yield b": keepalive\n\n"
                continue

            if chunk is None:
                break

            if isinstance(chunk, Exception):
                if pool_key is not None and forwarded == 0 and _is_pool_failover_exc(chunk):
                    raise _PoolUpfrontFailure(chunk)
                raise chunk

            last_upstream = now
            forwarded += 1
            yield chunk
    finally:
        stop.set()
        if t.is_alive():
            t.join(timeout=5)
            if t.is_alive():
                log.warning("rid=%s worker thread still alive after join", rid)
        log.info(
            "rid=%s stream end elapsed=%.1fs forwarded=%d heartbeats=%d pool_key=%s",
            rid, time.monotonic() - t0, forwarded, heartbeats, pool_key or "-",
        )


async def _stream_gen(kw: dict, request: Request, rid: str, model: str):
    """Stream to the client; when pooling, pick a key once per attempt.

    Once bytes flow the key is fixed (one stream = one billing identity).
    Failover happens only on an upfront 401/402/429/503 with nothing forwarded.
    """
    loop = asyncio.get_running_loop()
    t0 = time.monotonic()

    if not _pool_active():
        async for chunk in _stream_attempt(kw, request, rid, model, None, loop, t0):
            yield chunk
        return

    excluded: set[str] = set()
    first = True
    while True:
        name, reservation = await _resolve_pool_key(excluded, refresh=first)
        first = False
        if name is None:
            _release(name, reservation)
            if _key_material("you"):
                log.warning("rid=%s pool exhausted; falling back to primary key", rid)
                async for chunk in _stream_attempt(kw, request, rid, model, None, loop, t0):
                    yield chunk
                return
            raise _PoolExhausted(
                "no usable pool key: all disabled, dead, unanchored, or missing"
            )
        try:
            async for chunk in _stream_attempt(kw, request, rid, model, name, loop, t0):
                yield chunk
        except _PoolUpfrontFailure as up:
            exc = up.exc
            if isinstance(exc, anthropic.APIStatusError) and exc.status_code == 401:
                _mark_dead(name, exc.status_code)
            else:
                log.warning(
                    "rid=%s pool key=%s upfront failure status=%s -> failover",
                    rid, name, getattr(exc, "status_code", None),
                )
            excluded.add(name)
        else:
            _maybe_refresh(name)
            return
        finally:
            _release(name, reservation)


# ── Routes ────────────────────────────────────────────────────────────────────


async def _pool_usage_loop() -> None:
    """Background usage refresh (POOL_REFRESH_S); no-op when the pool is off."""
    while True:
        try:
            await asyncio.to_thread(_refresh_all_usage)
        except Exception as exc:
            log.warning("pool: usage refresh loop error (%s)", type(exc).__name__)
        await asyncio.sleep(POOL_REFRESH_S)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Start the usage refresher whenever pool.json exists, so status stays fresh
    # even when routing is forced off. With no keys it is a cheap no-op.
    await asyncio.to_thread(_pool_startup_log)
    task = asyncio.create_task(_pool_usage_loop())
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


app = FastAPI(lifespan=_lifespan)


@app.post("/v1/messages")
@app.post("/messages")
async def messages(request: Request):
    body = await request.json()
    kw = _kwargs(body)
    model = str(body.get("model", "?"))
    rid = uuid.uuid4().hex[:8]
    try:
        body_kb = len(json.dumps(body, separators=(",", ":")).encode("utf-8")) / 1024
    except Exception:
        body_kb = -1
    log.info(
        "rid=%s request start model=%s stream=%s body_kb=%.1f",
        rid, model, bool(body.get("stream", False)), body_kb,
    )

    if body.get("stream", False):
        kw["stream"] = True

        # Best-effort debug header: the first key an attempt will use. The
        # generator may re-pick on an upfront failure, so the log is authoritative.
        pool_key = None
        if _pool_active():
            pool_key, reservation = _pick(set())
            _release(pool_key, reservation)

        async def _safe_stream():
            try:
                async for chunk in _stream_gen(kw, request, rid, model):
                    yield chunk
            except _PoolExhausted as e:
                log.warning("rid=%s pool exhausted: %s", rid, e)
                err = json.dumps({"type": "error", "error": {"type": "api_error", "message": f"{e} (rid={rid})"}})
                yield f"event: error\ndata: {err}\n\n".encode()
            except anthropic.APIStatusError as e:
                log.warning("rid=%s APIStatusError status=%s body=%.300s", rid, e.status_code, str(e.body))
                err = json.dumps({"type": "error", "error": {"type": "api_error", "message": str(e.body)}})
                yield f"event: error\ndata: {err}\n\n".encode()
            except (anthropic.APITimeoutError, anthropic.APIConnectionError) as e:
                log.warning("rid=%s upstream %s: %.200s", rid, type(e).__name__, str(e))
                err = json.dumps({
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": f"upstream {type(e).__name__} {_exc_phase(e)}: {e} (rid={rid})",
                    },
                })
                yield f"event: error\ndata: {err}\n\n".encode()
            except Exception as e:
                log.warning("rid=%s proxy exc=%s msg=%.200s", rid, _exc_chain(e), str(e))
                err = json.dumps({"type": "error", "error": {"type": "api_error", "message": f"{e} (rid={rid})"}})
                yield f"event: error\ndata: {err}\n\n".encode()

        headers = {"cache-control": "no-cache", "x-accel-buffering": "no"}
        if pool_key:
            headers["x-pool-key"] = pool_key
        return StreamingResponse(
            _safe_stream(),
            media_type="text/event-stream",
            headers=headers,
        )

    # Non-streaming: sync SDK call in a thread to keep the event loop free.
    t0 = time.monotonic()
    try:
        msg, pool_key = await _create_with_failover(kw, rid)
        log.info(
            "rid=%s non-stream ok elapsed=%.1fs pool_key=%s",
            rid, time.monotonic() - t0, pool_key or "-",
        )
        headers = {"x-pool-key": pool_key} if pool_key else None
        return Response(
            content=msg.model_dump_json(),
            media_type="application/json",
            headers=headers,
        )
    except _PoolExhausted as e:
        log.warning("rid=%s pool exhausted: %s", rid, e)
        return Response(
            content=json.dumps({"error": {"message": str(e), "type": "proxy_error"}}),
            status_code=503,
            media_type="application/json",
        )
    except anthropic.APIStatusError as e:
        log.warning("rid=%s non-stream APIStatusError status=%s", rid, e.status_code)
        return Response(
            content=json.dumps(e.body) if e.body else b"",
            status_code=e.status_code,
            media_type="application/json",
        )
    except (anthropic.APITimeoutError, anthropic.APIConnectionError) as e:
        log.warning("rid=%s non-stream upstream %s", rid, type(e).__name__)
        return Response(
            content=json.dumps({"error": {"message": f"upstream {type(e).__name__} {_exc_phase(e)}: {e}", "type": "proxy_error"}}),
            status_code=504,
            media_type="application/json",
        )
    except Exception as e:
        log.warning("rid=%s non-stream exc=%s", rid, _exc_chain(e))
        return Response(
            content=json.dumps({"error": {"message": str(e), "type": "proxy_error"}}),
            status_code=500,
            media_type="application/json",
        )


@app.get("/pool/status")
async def pool_status():
    """Local-only pool status: per-key remaining/enabled/dead, no secrets."""
    return await asyncio.to_thread(_pool_status, True)


@app.get("/v1/models")
@app.get("/models")
async def models():
    """Live model list from the AgentRouter dashboard API (TTL-cached)."""
    ids = await asyncio.to_thread(_models)
    return {
        "object": "list",
        "data": [{"id": mid, "object": "model"} for mid in ids],
    }


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"AgentRouter proxy  →  {TARGET}")
    print(f"Listening on http://127.0.0.1:{PORT}")
    log.info(
        "config chunk_timeout=%sg queue_tick=%sg heartbeat=%sg connect=%s write=%s pool=%s retries=%d pool_routing=%s pool_env=%r pool_refresh=%sg reserve=%s",
        CHUNK_TIMEOUT, QUEUE_TICK, SSE_HEARTBEAT_S,
        CONNECT_TIMEOUT, WRITE_TIMEOUT, POOL_TIMEOUT, UPSTREAM_MAX_RETRIES,
        "on" if _pool_routing_enabled() else "off",
        POOL_ENABLED_ENV, POOL_REFRESH_S, POOL_RESERVE_USD,
    )
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
