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
"""

import asyncio
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

TARGET = "https://agentrouter.org"
PORT = int(os.environ.get("PORT", "7187"))
KEY_DIR = Path.home() / ".config/opencode/api_keys"
KEY_FILE = KEY_DIR / "AGENT_ROUTER_API_KEY"
# Dashboard credentials for the /api/user/* endpoints (model list, quota).
# Written by `agentrouter-proxy set-cookie`; the same files the CLI's
# quota command uses. Kept distinct from the sk- API key.
COOKIE_FILE = KEY_DIR / "AGENT_ROUTER_COOKIE"
USER_ID_FILE = KEY_DIR / "AGENT_ROUTER_USER_ID"
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
    q: queue.Queue,
    stop: threading.Event,
    handle: dict,
    rid: str,
    model: str,
) -> None:
    """
    Run inside a thread. Uses the sync Anthropic SDK's with_streaming_response
    to get raw SSE bytes and puts them into the queue, stripping any
    non-standard event types (e.g. billing_summary) that break OpenCode's parser.

    `stop` is set by the async side on downstream disconnect / stall abort;
    `handle` shares the live response object so the async side can close it
    to unblock a stuck socket read.
    """
    SKIP_EVENTS: set[bytes] = {b"billing_summary"}

    t0 = time.monotonic()
    first_at: float | None = None
    chunks = 0
    bytes_out = 0
    log.info("rid=%s upstream start model=%s", rid, model)

    try:
        with _client().messages.with_streaming_response.create(**kw) as resp:
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


async def _stream_gen(kw: dict, request: Request, rid: str, model: str):
    q: queue.Queue = queue.Queue()
    stop = threading.Event()
    handle: dict = {}
    t = threading.Thread(
        target=_stream_worker,
        args=(kw, q, stop, handle, rid, model),
        daemon=True,
        name=f"ar-stream-{rid}",
    )
    t.start()
    loop = asyncio.get_running_loop()
    t0 = time.monotonic()
    last_upstream = t0
    last_heartbeat = t0
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
            "rid=%s stream end elapsed=%.1fs forwarded=%d heartbeats=%d",
            rid, time.monotonic() - t0, forwarded, heartbeats,
        )


# ── Routes ────────────────────────────────────────────────────────────────────

app = FastAPI()


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

        async def _safe_stream():
            try:
                async for chunk in _stream_gen(kw, request, rid, model):
                    yield chunk
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

        return StreamingResponse(
            _safe_stream(),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )

    # Non-streaming: sync SDK call in a thread to keep the event loop free
    def _run():
        return _client().messages.create(**kw)

    t0 = time.monotonic()
    try:
        msg = await asyncio.to_thread(_run)
        log.info("rid=%s non-stream ok elapsed=%.1fs", rid, time.monotonic() - t0)
        return Response(content=msg.model_dump_json(), media_type="application/json")
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
        "config chunk_timeout=%sg queue_tick=%sg heartbeat=%sg connect=%s write=%s pool=%s retries=%d",
        CHUNK_TIMEOUT, QUEUE_TICK, SSE_HEARTBEAT_S,
        CONNECT_TIMEOUT, WRITE_TIMEOUT, POOL_TIMEOUT, UPSTREAM_MAX_RETRIES,
    )
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
