"""Unit tests for the sk--only key pool (proxy.py).

No network: key material is written to a temp KEY_DIR, the usage endpoint and
the Anthropic clients are stubbed, and the usage ledger is populated directly.

Run from the repo root:
    .venv/bin/python -m unittest tests.test_pool -v
"""

import asyncio
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import anthropic
import httpx

import proxy

SECRET = "sk-do-not-log-me-123456"


def _status_error(status: int) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://agentrouter.org/v1/messages")
    body = {"error": {"type": "api_error", "message": f"upstream {status}"}}
    response = httpx.Response(status, request=request, json=body)
    return anthropic.APIStatusError(f"upstream {status}", response=response, body=body)


def _raiser(exc: BaseException):
    def _create(**_kwargs):
        raise exc
    return _create


class _FakeMessages:
    def __init__(self, create):
        self.create = create


class _FakeClient:
    def __init__(self, create):
        self.messages = _FakeMessages(create)


class _FakeMessage:
    def model_dump_json(self):
        return json.dumps({"id": "msg_1"})


class _FakeRequest:
    async def is_disconnected(self):
        return False


class PoolTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.key_dir = Path(self.tmp.name)
        for patcher in (
            mock.patch.object(proxy, "KEY_DIR", self.key_dir),
            mock.patch.object(proxy, "KEY_FILE", self.key_dir / "AGENT_ROUTER_API_KEY"),
            mock.patch.object(proxy, "POOL_FILE", self.key_dir / "pool.json"),
            mock.patch.object(proxy, "POOL_STATE_FILE", self.key_dir / "pool-state.json"),
            mock.patch.object(proxy, "POOL_ENABLED_ENV", "1"),
            mock.patch.dict(os.environ, {"AGENTROUTER_API_KEY": ""}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        proxy._pool_clients.clear()
        proxy._pool_usage.clear()
        proxy._pool_in_flight.clear()
        proxy._pool_order = []
        proxy._pool_meta = {}
        proxy._pool_dead = {}
        proxy._pool_mtime = -1.0
        proxy._pool_virtual = set()
        proxy._you_quota.update({"usd": None, "at": 0.0, "ok_at": 0.0, "unknown": False, "stale": False})
        self.addCleanup(self.tmp.cleanup)

    # ── helpers ───────────────────────────────────────────────────────────────

    def write_key(self, name: str, key: str = SECRET, mode: int = 0o600) -> Path:
        path = self.key_dir / f"AGENTROUTER_FRIEND_{name}"
        path.write_text(key + "\n")
        os.chmod(path, mode)
        return path

    def write_pool(self, entries: dict):
        proxy.POOL_FILE.write_text(json.dumps(entries))
        proxy._pool_reload(force=True)

    def write_state(self, entries: dict):
        proxy.POOL_STATE_FILE.write_text(json.dumps(entries))

    def set_usage(self, name: str, cents, unknown: bool = False, stale: bool = False):
        proxy._pool_usage[name] = {
            "cents": cents,
            "at": time.monotonic(),
            "ok_at": time.monotonic(),
            "unknown": unknown,
            "stale": stale,
        }

    def use_own_key(self, key: str = SECRET):
        """Make the operator's own key resolvable for the virtual 'you' member."""
        proxy.KEY_FILE.write_text(key + "\n")
        return key

    @staticmethod
    def entry(remaining, anchor_cents, enabled: bool = True, recalibrated=None) -> dict:
        return {
            "enabled": enabled,
            "anchor_remaining_usd": remaining,
            "anchor_usage_cents": anchor_cents,
            "recalibrated": recalibrated,
        }


class RemainingMathTest(PoolTestBase):
    def test_anchor_plus_usage_delta(self):
        self.write_key("friend-a")
        self.write_pool({"friend-a": self.entry(30.0, 1000.0)})
        self.set_usage("friend-a", 1234.5)

        self.assertAlmostEqual(proxy._remaining("friend-a"), 27.655, places=6)

    def test_unanchored_key_has_no_remaining(self):
        self.write_key("friend-a")
        self.write_pool({"friend-a": self.entry(None, None)})
        self.set_usage("friend-a", 100.0)

        self.assertIsNone(proxy._remaining("friend-a"))

    def test_unknown_flag_alone_does_not_block_routing(self):
        # A failed refresh keeps the last-good cents usable (fail-open for
        # availability); only a key with no reading at all is unscoreable.
        self.write_key("friend-a")
        self.write_pool({"friend-a": self.entry(30.0, 1000.0)})
        self.set_usage("friend-a", 1234.5, unknown=True, stale=True)

        self.assertAlmostEqual(proxy._remaining("friend-a"), 27.655, places=6)

    def test_never_fetched_usage_is_unscoreable(self):
        self.write_key("friend-a")
        self.write_pool({"friend-a": self.entry(30.0, 1000.0)})
        self.set_usage("friend-a", None)

        self.assertIsNone(proxy._remaining("friend-a"))

    def test_status_reports_spent_and_stale(self):
        self.write_key("friend-a")
        self.write_key("friend-b")
        self.write_pool({
            "friend-a": self.entry(30.0, 1000.0),
            "friend-b": self.entry(10.0, 0.0),
        })
        self.set_usage("friend-a", 1234.5)
        self.set_usage("friend-b", 250.0, stale=True)

        status = proxy._pool_status(refresh=False)

        self.assertTrue(status["enabled"])
        self.assertAlmostEqual(status["per_key"]["friend-a"]["remaining"], 27.655, places=4)
        self.assertAlmostEqual(status["per_key"]["friend-a"]["spent"], 2.345, places=4)
        # friend-b has a reading (250c) so it stays scored and marked stale.
        self.assertAlmostEqual(status["per_key"]["friend-b"]["remaining"], 7.5, places=4)
        self.assertTrue(status["per_key"]["friend-b"]["stale"])
        self.assertFalse(status["per_key"]["friend-b"]["usage_unknown"])
        self.assertEqual(status["usage_unknown_count"], 0)
        self.assertAlmostEqual(status["total_remaining"], 35.155, places=4)


class DeadStateTest(PoolTestBase):
    def test_dead_mark_persists_atomically_and_600(self):
        self.write_key("friend-a")
        self.write_pool({"friend-a": self.entry(30.0, 1000.0)})

        proxy._mark_dead("friend-a", 401)

        payload = json.loads(proxy.POOL_STATE_FILE.read_text())
        self.assertTrue(payload["friend-a"]["dead"])
        self.assertEqual(payload["friend-a"]["last_status"], 401)
        self.assertEqual(stat.S_IMODE(os.stat(proxy.POOL_STATE_FILE).st_mode), 0o600)
        self.assertEqual(list(self.key_dir.glob("*.tmp.*")), [])

    def test_recalibration_after_dead_date_reenables(self):
        self.write_key("friend-a")
        self.write_state({"friend-a": {"dead": True, "dead_at": "2026-09-10T00:00:00+00:00"}})
        self.write_pool({"friend-a": self.entry(30.0, 1000.0, recalibrated="2026-09-01")})
        self.assertTrue(proxy._is_dead("friend-a"))

        self.write_pool({"friend-a": self.entry(30.0, 1000.0, recalibrated="2026-09-16")})
        self.assertFalse(proxy._is_dead("friend-a"))

    def test_dead_without_recalibration_stays_dead(self):
        self.write_key("friend-a")
        self.write_state({"friend-a": {"dead": True, "dead_at": "2026-09-10T00:00:00+00:00"}})
        self.write_pool({"friend-a": self.entry(30.0, 1000.0)})

        self.assertTrue(proxy._is_dead("friend-a"))


class RoutingTest(PoolTestBase):
    def test_least_spent_first(self):
        for name in ("friend-a", "friend-b", "friend-c"):
            self.write_key(name)
        self.write_pool({
            "friend-a": self.entry(10.0, 0.0),
            "friend-b": self.entry(20.0, 0.0),
            "friend-c": self.entry(5.0, 0.0),
        })
        for name in ("friend-a", "friend-b", "friend-c"):
            self.set_usage(name, 0.0)

        name, reservation = proxy._pick(set())

        self.assertEqual(name, "friend-b")
        self.assertAlmostEqual(reservation, proxy.POOL_RESERVE_USD)

    def test_tie_breaks_on_config_order(self):
        for name in ("friend-a", "friend-b"):
            self.write_key(name)
        self.write_pool({
            "friend-a": self.entry(10.0, 0.0),
            "friend-b": self.entry(10.0, 0.0),
        })
        self.set_usage("friend-a", 0.0)
        self.set_usage("friend-b", 0.0)

        name, _ = proxy._pick(set())
        self.assertEqual(name, "friend-a")

    def test_reservation_avoids_concurrent_double_pick(self):
        for name in ("friend-a", "friend-b"):
            self.write_key(name)
        self.write_pool({
            "friend-a": self.entry(10.0, 0.0),
            "friend-b": self.entry(10.0, 0.0),
        })
        self.set_usage("friend-a", 0.0)
        self.set_usage("friend-b", 0.0)

        first, r1 = proxy._pick(set())
        second, r2 = proxy._pick(set())

        self.assertEqual(first, "friend-a")
        self.assertEqual(second, "friend-b")
        proxy._release(first, r1)
        proxy._release(second, r2)
        self.assertEqual(proxy._pool_in_flight, {})

    def test_dead_and_disabled_are_skipped(self):
        for name in ("friend-a", "friend-b", "friend-c"):
            self.write_key(name)
        self.write_state({"friend-a": {"dead": True, "dead_at": "2099-01-01T00:00:00+00:00"}})
        self.write_pool({
            "friend-a": self.entry(99.0, 0.0),
            "friend-b": self.entry(50.0, 0.0),
            "friend-c": self.entry(1.0, 0.0, enabled=False),
        })
        for name in ("friend-a", "friend-b", "friend-c"):
            self.set_usage(name, 0.0)

        name, _ = proxy._pick(set())
        self.assertEqual(name, "friend-b")

    def test_unanchored_and_missing_key_files_are_skipped(self):
        self.write_key("friend-a")
        self.write_key("friend-b")
        # friend-c has a pool entry but no key file.
        self.write_pool({
            "friend-a": self.entry(None, None),
            "friend-b": self.entry(5.0, 0.0),
            "friend-c": self.entry(50.0, 0.0),
        })
        for name in ("friend-a", "friend-b", "friend-c"):
            self.set_usage(name, 0.0)

        name, _ = proxy._pick(set())
        self.assertEqual(name, "friend-b")

    def test_startup_log_warns_about_unanchored_without_key_material(self):
        self.write_key("friend-a")
        self.write_key("friend-b")
        self.write_pool({
            "friend-a": self.entry(5.0, 0.0),
            "friend-b": self.entry(None, None),
        })

        with self.assertLogs(proxy.log, level="INFO") as captured:
            proxy._pool_startup_log()

        joined = "\n".join(captured.output)
        self.assertIn("un-anchored", joined)
        self.assertIn("friend-b", joined)
        self.assertNotIn(SECRET, joined)

    def test_world_readable_key_file_is_warned(self):
        self.write_key("friend-a", mode=0o644)
        self.write_pool({"friend-a": self.entry(5.0, 0.0)})

        with self.assertLogs(proxy.log, level="WARNING") as captured:
            proxy._warn_key_permissions("friend-a")

        self.assertIn("chmod 600", "\n".join(captured.output))

    def test_disabling_a_key_evicts_its_cached_client(self):
        self.write_key("friend-a")
        self.write_pool({"friend-a": self.entry(5.0, 0.0)})
        proxy._pool_clients["friend-a"] = ("fp", _FakeClient(lambda **_kw: None))
        proxy._pool_usage["friend-a"] = {"cents": 0.0, "at": time.monotonic(), "unknown": False}

        self.write_pool({"friend-a": self.entry(5.0, 0.0, enabled=False)})

        self.assertNotIn("friend-a", proxy._pool_clients)
        self.assertNotIn("friend-a", proxy._pool_usage)


class FailoverTest(PoolTestBase):
    def setUp(self):
        super().setUp()
        for name in ("primary", "secondary"):
            self.write_key(name)
        self.write_pool({
            "primary": self.entry(50.0, 0.0),
            "secondary": self.entry(10.0, 0.0),
        })
        self.set_usage("primary", 0.0)
        self.set_usage("secondary", 0.0)

    def _run(self, clients):
        with mock.patch.object(proxy, "_client_for", side_effect=lambda n: clients[n]):
            return asyncio.run(proxy._create_with_failover({"model": "x"}, "rid123"))

    def test_failover_on_402_then_success(self):
        clients = {
            "primary": _FakeClient(_raiser(_status_error(402))),
            "secondary": _FakeClient(lambda **_kw: _FakeMessage()),
        }
        msg, used = self._run(clients)

        self.assertEqual(used, "secondary")
        self.assertIsInstance(msg, _FakeMessage)

    def test_failover_on_429_and_503(self):
        for status in (429, 503):
            clients = {
                "primary": _FakeClient(_raiser(_status_error(status))),
                "secondary": _FakeClient(lambda **_kw: _FakeMessage()),
            }
            _, used = self._run(clients)
            self.assertEqual(used, "secondary", status)

    def test_401_marks_dead_and_fails_over(self):
        clients = {
            "primary": _FakeClient(_raiser(_status_error(401))),
            "secondary": _FakeClient(lambda **_kw: _FakeMessage()),
        }
        _, used = self._run(clients)

        self.assertEqual(used, "secondary")
        payload = json.loads(proxy.POOL_STATE_FILE.read_text())
        self.assertTrue(payload["primary"]["dead"])

    def test_non_failover_status_propagates(self):
        clients = {
            "primary": _FakeClient(_raiser(_status_error(400))),
            "secondary": _FakeClient(lambda **_kw: _FakeMessage()),
        }
        with self.assertRaises(anthropic.APIStatusError) as ctx:
            self._run(clients)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_exhausted_pool_raises(self):
        self.write_state({
            "primary": {"dead": True, "dead_at": "2099-01-01T00:00:00+00:00"},
            "secondary": {"dead": True, "dead_at": "2099-01-01T00:00:00+00:00"},
        })
        proxy._pool_reload(force=True)

        with mock.patch.object(proxy, "_refresh_all_usage"):
            with self.assertRaises(proxy._PoolExhausted):
                self._run({})

    def test_failover_prefers_richest_then_next_usable(self):
        # Richest = primary (fails 503). secondary is dead, so tertiary serves.
        self.write_key("tertiary")
        self.write_pool({
            "primary": self.entry(50.0, 0.0),
            "secondary": self.entry(10.0, 0.0),
            "tertiary": self.entry(5.0, 0.0),
        })
        self.set_usage("tertiary", 0.0)
        proxy._pool_dead["secondary"] = "2099-01-01T00:00:00+00:00"

        calls = []

        def primary_create(**_kw):
            calls.append("primary")
            raise _status_error(503)

        clients = {
            "primary": _FakeClient(primary_create),
            "secondary": _FakeClient(lambda **_kw: _FakeMessage()),
            "tertiary": _FakeClient(lambda **_kw: _FakeMessage()),
        }
        _, used = self._run(clients)

        self.assertEqual(used, "tertiary")
        self.assertEqual(calls, ["primary"])


class StreamingFailoverTest(PoolTestBase):
    """_stream_gen picks once per attempt and fails over only before any bytes."""

    def setUp(self):
        super().setUp()
        for name in ("primary", "secondary"):
            self.write_key(name)
        self.write_pool({
            "primary": self.entry(50.0, 0.0),
            "secondary": self.entry(10.0, 0.0),
        })
        self.set_usage("primary", 0.0)
        self.set_usage("secondary", 0.0)

    def _run(self, clients, attempts):
        def fake_worker(kw, client, q, stop, handle, rid, model, pool_key=None):
            _, payload = attempts[pool_key]
            q.put(payload)
            if not isinstance(payload, Exception):
                q.put(None)

        async def drive():
            chunks = []
            with mock.patch.object(proxy, "_client_for", side_effect=lambda n: clients[n]), \
                 mock.patch.object(proxy, "_stream_worker", new=fake_worker):
                async for chunk in proxy._stream_gen(
                    {"stream": True}, _FakeRequest(), "rid-stream", "m"
                ):
                    chunks.append(chunk)
            return chunks

        return asyncio.run(drive())

    def test_upfront_503_retries_on_the_next_key(self):
        clients = {
            "primary": _FakeClient(lambda **_kw: None),
            "secondary": _FakeClient(lambda **_kw: None),
        }
        chunks = self._run(clients, {
            "primary": (503, _status_error(503)),
            "secondary": (200, b'data: {"ok":true}\n\n'),
        })

        self.assertEqual(chunks, [b'data: {"ok":true}\n\n'])
        self.assertEqual(proxy._pool_in_flight, {})

    def test_upfront_401_marks_primary_dead(self):
        clients = {
            "primary": _FakeClient(lambda **_kw: None),
            "secondary": _FakeClient(lambda **_kw: None),
        }
        self._run(clients, {
            "primary": (401, _status_error(401)),
            "secondary": (200, b"data: {}\n\n"),
        })

        payload = json.loads(proxy.POOL_STATE_FILE.read_text())
        self.assertTrue(payload["primary"]["dead"])

    def test_midstream_error_does_not_retry(self):
        clients = {
            "primary": _FakeClient(lambda **_kw: None),
            "secondary": _FakeClient(lambda **_kw: None),
        }

        def fake_worker(kw, client, q, stop, handle, rid, model, pool_key=None):
            q.put(b"data: partial\n\n")
            q.put(_status_error(503))

        async def drive():
            with mock.patch.object(proxy, "_client_for", side_effect=lambda n: clients[n]), \
                 mock.patch.object(proxy, "_stream_worker", new=fake_worker):
                async for _ in proxy._stream_gen({"stream": True}, _FakeRequest(), "rid", "m"):
                    pass

        with self.assertRaises(anthropic.APIStatusError):
            asyncio.run(drive())
        # Reservation from the single failed attempt must be released.
        self.assertEqual(proxy._pool_in_flight, {})

    def test_exhausted_pool_raises_before_any_attempt(self):
        self.write_state({
            "primary": {"dead": True, "dead_at": "2099-01-01T00:00:00+00:00"},
            "secondary": {"dead": True, "dead_at": "2099-01-01T00:00:00+00:00"},
        })
        proxy._pool_reload(force=True)

        async def drive():
            async for _ in proxy._stream_gen({"stream": True}, _FakeRequest(), "rid", "m"):
                pass

        with mock.patch.object(proxy, "_client_for", side_effect=lambda n: _FakeClient(lambda **_kw: None)), \
             mock.patch.object(proxy, "_refresh_all_usage"):
            with self.assertRaises(proxy._PoolExhausted):
                asyncio.run(drive())


class RoutingActivationTest(PoolTestBase):
    """POOL_ENABLED unset = auto; explicit 0/1 always wins."""

    def _auto(self):
        return mock.patch.object(proxy, "POOL_ENABLED_ENV", None)

    def test_auto_enables_when_an_anchored_key_exists(self):
        self.write_key("friend-a")
        self.write_pool({"friend-a": self.entry(30.0, 100.0)})
        with self._auto():
            self.assertTrue(proxy._pool_routing_enabled())
            self.assertTrue(proxy._pool_active())

    def test_auto_off_without_keys(self):
        with self._auto():
            self.assertFalse(proxy._pool_routing_enabled())

    def test_auto_off_for_unanchored_key(self):
        self.write_key("friend-a")
        self.write_pool({"friend-a": self.entry(None, None)})
        with self._auto():
            self.assertFalse(proxy._pool_routing_enabled())

    def test_auto_off_for_disabled_key(self):
        self.write_key("friend-a")
        self.write_pool({"friend-a": self.entry(30.0, 100.0, enabled=False)})
        with self._auto():
            self.assertFalse(proxy._pool_routing_enabled())

    def test_explicit_zero_is_a_kill_switch(self):
        self.write_key("friend-a")
        self.write_pool({"friend-a": self.entry(30.0, 100.0)})
        with mock.patch.object(proxy, "POOL_ENABLED_ENV", "0"):
            self.assertFalse(proxy._pool_routing_enabled())
            self.assertFalse(proxy._pool_active())

    def test_explicit_one_forces_on_without_keys(self):
        with mock.patch.object(proxy, "POOL_ENABLED_ENV", "1"):
            self.assertTrue(proxy._pool_routing_enabled())

    def test_status_reports_routing_and_configured(self):
        self.write_key("friend-a")
        self.write_pool({"friend-a": self.entry(30.0, 100.0)})
        self.set_usage("friend-a", 100.0)

        with self._auto():
            status = proxy._pool_status(refresh=False)
        self.assertTrue(status["enabled"])
        self.assertEqual(status["configured"], 1)

        with mock.patch.object(proxy, "POOL_ENABLED_ENV", "0"):
            status = proxy._pool_status(refresh=False)
        self.assertFalse(status["enabled"])
        self.assertEqual(status["configured"], 1)


class StreamZeroBytesTest(PoolTestBase):
    """A clean upstream close (no error, no bytes) must not be treated as failover."""

    def test_empty_upstream_closes_stream_without_retry(self):
        self.write_key("primary")
        self.write_pool({"primary": self.entry(50.0, 0.0)})
        self.set_usage("primary", 0.0)

        def fake_worker(kw, client, q, stop, handle, rid, model, pool_key=None):
            q.put(None)

        async def drive():
            chunks = []
            with mock.patch.object(proxy, "_client_for", return_value=_FakeClient(lambda **_kw: None)), \
                 mock.patch.object(proxy, "_stream_worker", new=fake_worker):
                async for chunk in proxy._stream_gen({"stream": True}, _FakeRequest(), "rid", "m"):
                    chunks.append(chunk)
            return chunks

        self.assertEqual(asyncio.run(drive()), [])
        self.assertEqual(proxy._pool_in_flight, {})


class RedactionTest(PoolTestBase):
    def test_no_key_material_in_logs(self):
        self.write_key("friend-a", key=SECRET)
        self.write_pool({"friend-a": self.entry(30.0, 1000.0)})
        self.set_usage("friend-a", 1234.5)

        with mock.patch.object(proxy, "_fetch_usage_cents", side_effect=RuntimeError("boom")):
            with self.assertLogs(proxy.log, level="DEBUG") as captured:
                proxy._pool_startup_log()
                proxy._pool_status(refresh=False)
                proxy._mark_dead("friend-a", 401)
                proxy._refresh_usage("friend-a")

        for line in captured.output:
            self.assertNotIn(SECRET, line)
            self.assertNotIn("sk-", line)


class UsageRefreshTest(PoolTestBase):
    def test_refresh_failure_fails_closed_to_unknown(self):
        self.write_key("friend-a")
        self.write_pool({"friend-a": self.entry(30.0, 1000.0)})

        with mock.patch.object(proxy, "_fetch_usage_cents", side_effect=RuntimeError("boom")):
            proxy._refresh_usage("friend-a")

        usage = proxy._pool_usage["friend-a"]
        self.assertTrue(usage["unknown"])
        self.assertIsNone(usage["cents"])
        self.assertIsNone(proxy._remaining("friend-a"))

    def test_refresh_success_updates_remaining(self):
        self.write_key("friend-a")
        self.write_pool({"friend-a": self.entry(30.0, 1000.0)})

        with mock.patch.object(proxy, "_fetch_usage_cents", return_value=1500.0):
            proxy._refresh_usage("friend-a")

        self.assertAlmostEqual(proxy._remaining("friend-a"), 25.0, places=6)
        self.assertFalse(proxy._pool_usage["friend-a"]["stale"])

    def test_failed_refresh_keeps_last_good_and_marks_stale(self):
        self.write_key("friend-a")
        self.write_pool({"friend-a": self.entry(30.0, 1000.0)})

        with mock.patch.object(proxy, "_fetch_usage_cents", return_value=1500.0):
            proxy._refresh_usage("friend-a")
        with mock.patch.object(proxy, "_fetch_usage_cents", side_effect=RuntimeError("boom")):
            proxy._refresh_usage("friend-a")

        usage = proxy._pool_usage["friend-a"]
        self.assertEqual(usage["cents"], 1500.0)
        self.assertTrue(usage["stale"])
        self.assertFalse(usage["unknown"])
        self.assertAlmostEqual(proxy._remaining("friend-a"), 25.0, places=6)

    def test_fetch_usage_parses_total_usage(self):
        captured = {}

        def fake_get(url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs["headers"]
            captured["params"] = kwargs.get("params")
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                json={"object": "list", "total_usage": 57965.2012},
            )

        with mock.patch.object(proxy._HTTP_CLIENT, "get", side_effect=fake_get):
            cents = proxy._fetch_usage_cents(SECRET)

        self.assertAlmostEqual(cents, 57965.2012, places=4)
        self.assertTrue(captured["url"].endswith("/v1/dashboard/billing/usage"))
        self.assertEqual(captured["headers"]["Authorization"], f"Bearer {SECRET}")
        self.assertEqual(captured["params"]["start_date"], "1970-01-01")


class OwnAccountTest(PoolTestBase):
    """'you': explicit anchor wins; otherwise a virtual cookie-sourced member."""

    def test_virtual_you_appears_once_a_friend_is_pooled(self):
        self.use_own_key()
        self.write_key("friend-a")
        self.write_pool({"friend-a": self.entry(30.0, 0.0)})

        self.assertIn("you", proxy._pool_order)
        self.assertIn("you", proxy._pool_virtual)

    def test_no_virtual_you_without_friends(self):
        self.use_own_key()
        proxy._pool_reload(force=True)

        self.assertNotIn("you", proxy._pool_order)

    def test_virtual_you_remaining_comes_from_cookie(self):
        self.use_own_key()
        self.write_key("friend-a")
        self.write_pool({"friend-a": self.entry(30.0, 0.0)})
        proxy._you_quota["usd"] = 12.5

        self.assertAlmostEqual(proxy._remaining("you"), 12.5, places=6)

        status = proxy._pool_status(refresh=False)
        self.assertTrue(status["per_key"]["you"]["virtual"])
        self.assertFalse(status["per_key"]["you"]["usage_unknown"])
        self.assertAlmostEqual(status["per_key"]["you"]["remaining"], 12.5, places=4)

    def test_virtual_you_unknown_when_cookie_unavailable(self):
        self.use_own_key()
        self.write_key("friend-a")
        self.write_pool({"friend-a": self.entry(30.0, 0.0)})
        self.set_usage("friend-a", 0.0)
        proxy._you_quota["usd"] = None
        proxy._you_quota["unknown"] = True

        self.assertIsNone(proxy._remaining("you"))
        status = proxy._pool_status(refresh=False)
        self.assertTrue(status["per_key"]["you"]["usage_unknown"])
        self.assertEqual(status["usage_unknown_count"], 1)

    def test_explicit_you_uses_anchor_not_cookie(self):
        self.use_own_key()
        self.write_pool({"you": self.entry(50.0, 100.0)})
        proxy._you_quota["usd"] = 999.0
        self.set_usage("you", 300.0)

        self.assertNotIn("you", proxy._pool_virtual)
        self.assertAlmostEqual(proxy._remaining("you"), 48.0, places=6)

    def test_fetch_you_quota_rejects_waf_html(self):
        self.use_own_key()

        def fake_get(url, **kwargs):
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                headers={"content-type": "text/html; charset=utf-8"},
                content=b"<html>aliyun_waf_aa</html>",
            )

        with mock.patch.object(proxy._HTTP_CLIENT, "get", side_effect=fake_get):
            with self.assertRaises(RuntimeError):
                proxy._fetch_you_quota_usd()

    def test_refresh_you_handles_waf_gracefully(self):
        self.use_own_key()
        with mock.patch.object(proxy, "_fetch_you_quota_usd", side_effect=RuntimeError("WAF")):
            proxy._refresh_you()

        self.assertIsNone(proxy._you_quota["usd"])
        self.assertTrue(proxy._you_quota["unknown"])


class FallbackToPrimaryTest(PoolTestBase):
    """A cold/unusable pool must fall back to the primary key, not error."""

    def setUp(self):
        super().setUp()
        self.use_own_key()
        self.write_key("friend-a")
        self.write_pool({"friend-a": self.entry(30.0, 1000.0)})
        # No usage cached -> friend-a unscoreable.

    def test_resolve_refreshes_then_picks(self):
        def refresh():
            self.set_usage("friend-a", 1000.0)

        with mock.patch.object(proxy, "_refresh_all_usage", side_effect=refresh):
            name, _ = asyncio.run(proxy._resolve_pool_key(set(), refresh=True))

        self.assertEqual(name, "friend-a")

    def test_non_stream_falls_back_to_primary(self):
        primary = _FakeClient(lambda **_kw: _FakeMessage())
        with mock.patch.object(proxy, "_refresh_all_usage"), \
             mock.patch.object(proxy, "_client", return_value=primary):
            msg, pool_key = asyncio.run(proxy._create_with_failover({"model": "x"}, "rid"))

        self.assertIsNone(pool_key)
        self.assertIsInstance(msg, _FakeMessage)

    def test_stream_falls_back_to_primary(self):
        chunks_seen = []

        def fake_worker(kw, client, q, stop, handle, rid, model, pool_key=None):
            chunks_seen.append(pool_key)
            q.put(b"data: ok\n\n")
            q.put(None)

        primary = _FakeClient(lambda **_kw: None)
        with mock.patch.object(proxy, "_refresh_all_usage"), \
             mock.patch.object(proxy, "_client", return_value=primary), \
             mock.patch.object(proxy, "_stream_worker", new=fake_worker):
            async def drive():
                out = []
                async for chunk in proxy._stream_gen({"stream": True}, _FakeRequest(), "rid", "m"):
                    out.append(chunk)
                return out
            out = asyncio.run(drive())

        self.assertEqual(out, [b"data: ok\n\n"])
        self.assertEqual(chunks_seen, [None])

    def test_exhausted_without_primary_raises(self):
        # No own key resolvable -> no fallback.
        proxy.KEY_FILE.unlink(missing_ok=True)
        with mock.patch.object(proxy, "_refresh_all_usage"):
            with self.assertRaises(proxy._PoolExhausted):
                asyncio.run(proxy._create_with_failover({"model": "x"}, "rid"))


if __name__ == "__main__":
    unittest.main()
