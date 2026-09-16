"""Unit tests for proxy._normalize_thinking_blocks / _kwargs.

Run from the repo root:
    .venv/bin/python -m unittest tests.test_proxy -v
"""

import unittest

import proxy


def _thinking(text="chain of thought", **extra):
    return {"type": "thinking", "thinking": text, **extra}


def _tool_use(tid="toolu_1"):
    return {"type": "tool_use", "id": tid, "name": "get_weather", "input": {}}


def _assistant(*content):
    return {"role": "assistant", "content": list(content)}


class NormalizeThinkingBlocksTest(unittest.TestCase):
    def test_tool_use_without_thinking_gets_empty_thinking_injected(self):
        msgs = [_assistant(_tool_use())]
        out, injected, dropped = proxy._normalize_thinking_blocks(msgs, "ensure")

        self.assertEqual((injected, dropped), (1, 0))
        self.assertEqual(
            out[0]["content"],
            [{"type": "thinking", "thinking": ""}, _tool_use()],
        )

    def test_tool_use_with_thinking_is_left_alone(self):
        block = _thinking("real reasoning", signature="sig-1")
        msgs = [_assistant(block, _tool_use())]
        out, injected, dropped = proxy._normalize_thinking_blocks(msgs, "ensure")

        self.assertEqual((injected, dropped), (0, 0))
        self.assertEqual(out[0]["content"], [block, _tool_use()])

    def test_text_then_tool_use_without_thinking_is_injected(self):
        msgs = [_assistant({"type": "text", "text": "let me check"}, _tool_use())]
        out, injected, dropped = proxy._normalize_thinking_blocks(msgs, "ensure")

        self.assertEqual((injected, dropped), (1, 0))
        self.assertEqual(out[0]["content"][0], {"type": "thinking", "thinking": ""})
        self.assertEqual(out[0]["content"][1], {"type": "text", "text": "let me check"})

    def test_text_only_assistant_does_not_need_thinking(self):
        msgs = [_assistant({"type": "text", "text": "just an answer"})]
        out, injected, dropped = proxy._normalize_thinking_blocks(msgs, "ensure")

        self.assertEqual((injected, dropped), (0, 0))
        self.assertEqual(out[0]["content"], [{"type": "text", "text": "just an answer"}])

    def test_user_message_with_tool_result_is_untouched(self):
        msgs = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}]
        out, injected, dropped = proxy._normalize_thinking_blocks(msgs, "ensure")

        self.assertEqual((injected, dropped), (0, 0))
        self.assertEqual(out, msgs)

    def test_redacted_thinking_dropped_in_ensure_and_strip(self):
        for mode in ("ensure", "strip"):
            msgs = [_assistant(
                {"type": "redacted_thinking", "data": "opaque"},
                {"type": "text", "text": "keep"},
            )]
            out, injected, dropped = proxy._normalize_thinking_blocks(msgs, mode)
            self.assertEqual((injected, dropped), (0, 1), mode)
            self.assertEqual(out[0]["content"], [{"type": "text", "text": "keep"}], mode)

    def test_redacted_plus_tool_use_still_gets_thinking_injected(self):
        msgs = [_assistant({"type": "redacted_thinking", "data": "x"}, _tool_use())]
        out, injected, dropped = proxy._normalize_thinking_blocks(msgs, "ensure")

        self.assertEqual((injected, dropped), (1, 1))
        self.assertEqual(out[0]["content"][0], {"type": "thinking", "thinking": ""})
        self.assertEqual(out[0]["content"][1], _tool_use())

    def test_thinking_missing_field_is_normalised_to_empty_string(self):
        msgs = [_assistant({"type": "thinking"}, _tool_use())]
        out, injected, dropped = proxy._normalize_thinking_blocks(msgs, "ensure")

        self.assertEqual((injected, dropped), (0, 0))
        self.assertEqual(out[0]["content"][0], {"type": "thinking", "thinking": ""})

    def test_strip_drops_thinking_without_injecting(self):
        msgs = [_assistant(_thinking(), _tool_use(), {"type": "text", "text": "keep"})]
        out, injected, dropped = proxy._normalize_thinking_blocks(msgs, "strip")

        self.assertEqual((injected, dropped), (0, 1))
        self.assertEqual(out[0]["content"], [_tool_use(), {"type": "text", "text": "keep"}])

    def test_thinking_only_message_kept_in_ensure_dropped_in_strip(self):
        msgs = [_assistant(_thinking()), {"role": "user", "content": "hi"}]

        kept, *_ = proxy._normalize_thinking_blocks(msgs, "ensure")
        self.assertEqual(len(kept), 2)
        self.assertEqual(kept[0]["content"], [_thinking()])

        stripped, *_ = proxy._normalize_thinking_blocks(msgs, "strip")
        self.assertEqual(len(stripped), 1)
        self.assertEqual(stripped[0]["role"], "user")

    def test_string_content_untouched(self):
        msgs = [{"role": "user", "content": "plain string"}]
        out, injected, dropped = proxy._normalize_thinking_blocks(msgs, "ensure")

        self.assertEqual((injected, dropped), (0, 0))
        self.assertEqual(out, msgs)

    def test_off_mode_returns_input_untouched(self):
        msgs = [_assistant(_tool_use())]
        out, injected, dropped = proxy._normalize_thinking_blocks(msgs, "off")

        self.assertIs(out, msgs)
        self.assertEqual((injected, dropped), (0, 0))

    def test_non_list_and_non_dict_are_safe(self):
        msgs = [None, "bogus", {"role": "assistant", "content": None}]
        out, injected, dropped = proxy._normalize_thinking_blocks(msgs, "ensure")

        self.assertEqual(out, msgs)
        self.assertEqual((injected, dropped), (0, 0))


class KwargsTest(unittest.TestCase):
    def test_strips_non_standard_top_level_fields(self):
        body = {
            "model": "deepseek-v4-flash",
            "max_tokens": 10,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "high"},
            "messages": [{"role": "user", "content": "hi"}],
        }
        kw = proxy._kwargs(body)

        self.assertNotIn("thinking", kw)
        self.assertNotIn("output_config", kw)
        self.assertEqual(kw["model"], "deepseek-v4-flash")
        self.assertEqual(kw["messages"], [{"role": "user", "content": "hi"}])

    def test_default_mode_is_ensure_and_injects_missing_thinking(self):
        self.assertEqual(proxy.THINKING_HISTORY, "ensure")

        body = {
            "messages": [
                {"role": "user", "content": "weather?"},
                _assistant(_tool_use()),
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "32C"}]},
            ]
        }
        kw = proxy._kwargs(body)

        self.assertEqual(kw["messages"][1]["content"][0], {"type": "thinking", "thinking": ""})
        self.assertEqual(kw["messages"][1]["content"][1], _tool_use())

    def test_missing_messages_is_safe(self):
        kw = proxy._kwargs({"model": "x", "max_tokens": 1})
        self.assertEqual(kw, {"model": "x", "max_tokens": 1})


if __name__ == "__main__":
    unittest.main()
