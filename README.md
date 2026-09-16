# agentrouter-opencode-proxy

A local proxy that lets Node.js AI coding clients use [AgentRouter](https://agentrouter.org) as an Anthropic-compatible LLM backend.

## Get $150 in free credits

Sign up with this referral link and you get **$150 in free credits** to use on any model (Claude, GPT-4o, DeepSeek, Gemini, and more):

**👉 https://agentrouter.org/register?aff=pP0u**

---

## Quickest setup: use the agent prompt

If you're already inside an AI coding agent (OpenCode, Claude Code, Cursor, Cline, etc.), the fastest way to get set up is to paste the prompt from **[AGENT_SETUP_PROMPT.md](./AGENT_SETUP_PROMPT.md)** directly into your agent's chat.

The agent will:
1. Check prerequisites (Python 3.11+, git)
2. Clone this repo
3. Create the virtualenv and install dependencies
4. Ask for your API key and store it securely
5. Patch your `opencode.json` (or set env vars for other clients)
6. Start the proxy
7. Run both streaming and non-streaming end-to-end tests
8. Report results and tell you which model to select

No manual steps needed — the agent handles everything and confirms it works before finishing.

---

## Supported clients

| Client | Built on |
|--------|----------|
| [OpenCode](https://opencode.ai) | Vercel AI SDK (`@ai-sdk/anthropic`) |
| [Claude Code](https://claude.ai/code) | `@anthropic-ai/sdk` |
| [Cursor](https://cursor.com) | Anthropic / OpenAI API |
| [Cline](https://github.com/cline/cline) | `@anthropic-ai/sdk` |
| [Continue.dev](https://continue.dev) | `@anthropic-ai/sdk` |
| [Zed](https://zed.dev) | Anthropic API |
| [Aider](https://aider.chat) | `litellm` (Python, OpenAI-compatible) |
| Any app using [Vercel AI SDK](https://sdk.vercel.ai) | `@ai-sdk/anthropic` |
| Any app using [`@anthropic-ai/sdk`](https://github.com/anthropic-ai/sdk-python) | Node.js Anthropic SDK |
| Any app using [LangChain.js](https://js.langchain.com) | `@langchain/anthropic` |

Point any of these at `http://localhost:7187` and they'll work with AgentRouter.

---

## Why this exists

AgentRouter is a Chinese API aggregator that provides cheap or free access to models like `claude-opus-4-6`, GPT-4o, DeepSeek, and others. It's fronted by an **Aliyun WAF that fingerprints TLS handshakes at the connection level** — not just bearer tokens.

Only a small allowlist of clients pass the check:
- Claude Code
- Codex CLI
- Gemini CLI
- RooCode / Kilocode / Qwen Code
- The **Python sync `anthropic` SDK** (which those tools are built on)

Everything else — `curl`, Node.js `fetch`, Python `httpx.AsyncClient`, raw `httpx.Client` — gets:

```
{"error":{"message":"unauthorized client detected, contact support..."}}
```

This proxy runs locally on port `7187`, receives requests from any Node.js AI SDK client, and re-issues them to AgentRouter using the **Python sync `anthropic` SDK** — which carries the correct TLS fingerprint.

---

## Research trail

This solution was pieced together from several public issues and PRs. Here is the full chain of discovery in the order the clues surfaced.

### 1. The WAF allowlist is documented publicly

**[agentrouter-org/docs#21](https://github.com/agentrouter-org/docs/issues/21)** — *"Support ForgeCode as an official client (currently rejected with 'unauthorized client detected')"*

This issue, opened by a ForgeCode contributor, contains the key finding:

> AgentRouter is fronted by an Aliyun WAF that allowlists requests by **client fingerprint**, not just by bearer token. Generic API calls are rejected regardless of whether the API key is valid. Only the officially-supported clients and the Anthropic/OpenAI SDKs they're built on get past the check.
>
> The same token **does** get past client authentication when used with the official Claude Code client configured with `ANTHROPIC_BASE_URL=https://agentrouter.org/`.

This was the first public confirmation that routing through the Anthropic SDK bypasses the WAF.

---

### 2. OpenCode hits the same wall

**[anomalyco/opencode#5060](https://github.com/anomalyco/opencode/issues/5060)** — *"Agentrouter not working"*

An OpenCode user reports the identical `unauthorized client detected` error after connecting with a valid AgentRouter key. The issue confirms that OpenCode's Node.js AI SDK cannot communicate with AgentRouter directly — the same WAF fingerprint block documented in #1.

---

### 3. First working fix: sync Anthropic SDK

**[yowanda/Reckora#67](https://github.com/yowanda/Reckora/pull/67)** — *"fix(reasoning): route AgentRouter through Anthropic SDK so its WAF allowlist accepts requests"*

The Reckora project (a Python app) hit the same wall and documented the fix in detail:

> Switch the AgentRouter dispatch path from `AsyncOpenAI` (chat-completions shape) to `AsyncAnthropic` (messages shape) against the Anthropic-compatible `/v1/messages` route. This is the same wire shape Claude Code uses, so AgentRouter's WAF accepts our requests as a "Claude Code-shaped client".

Live verification from their production VPS:
```
OpenAI SDK   → /v1/chat/completions  → 401 unauthorized client detected
Anthropic SDK → /v1/messages         → 200 / 503 (downstream pool routing — WAF passed)
```

The 503 "no available channel" is a capacity issue on AgentRouter's pool, not a code bug. It means the WAF check passed.

---

### 4. Base URL trap: `/v1` double-prefix

**[yowanda/Reckora#68](https://github.com/yowanda/Reckora/pull/68)** — *"fix(reasoning): coerce stale AgentRouter base URL to root so Anthropic SDK lands on /v1/messages"*

Immediately after #67 shipped, a new failure:

> The Anthropic SDK appends `/v1/messages` to whatever `base_url` it's given. If `base_url` is `https://agentrouter.org/v1`, the runtime URL becomes `/v1/v1/messages` and AgentRouter returns 404 "Invalid URL".

**Fix:** set `base_url` to `https://agentrouter.org` (no `/v1` suffix). The SDK constructs the correct `/v1/messages` path on its own.

This applies directly to our `opencode.json` config — `baseURL` must be `http://localhost:7187` with no path, and the proxy's `TARGET` must be `https://agentrouter.org` with no path.

---

### 5. Async SDK is also blocked — sync-in-thread required

**[yowanda/Reckora#69](https://github.com/yowanda/Reckora/pull/69)** — *"fix(reasoning): use sync Anthropic SDK in asyncio.to_thread (AgentRouter WAF rejects async-httpx fingerprint)"*

After #68 deployed, another 401 — even though the Anthropic SDK was being used:

> Live A/B inside the production container, same key, same `anthropic==0.100.0`:
> ```
> Anthropic(...).messages.create(...)       → 503  (WAF passed, downstream pool routing)
> AsyncAnthropic(...).messages.create(...)  → 401  "unauthorized client detected"
> ```
> The differentiator is the async-httpx (`AsyncHTTPTransport` / anyio-backed) TLS handshake itself; AgentRouter's Aliyun WAF fingerprints at the connection level and **only the sync httpx handshake is on the allowlist**.

**Fix:** use `anthropic.Anthropic` (sync) and call `messages.create` inside `asyncio.to_thread` so the event loop stays non-blocking but the wire request uses the sync httpx TLS stack.

This is the exact architecture of `proxy.py` in this repo.

---

### Summary of the discovery chain

| Step | Finding | Source |
|------|---------|--------|
| 1 | WAF blocks by TLS fingerprint, not just bearer token | [agentrouter-org/docs#21](https://github.com/agentrouter-org/docs/issues/21) |
| 2 | OpenCode (Node.js) is blocked the same way | [anomalyco/opencode#5060](https://github.com/anomalyco/opencode/issues/5060) |
| 3 | Python sync `Anthropic` SDK passes the WAF | [yowanda/Reckora#67](https://github.com/yowanda/Reckora/pull/67) |
| 4 | `base_url` must not include `/v1` — SDK appends it | [yowanda/Reckora#68](https://github.com/yowanda/Reckora/pull/68) |
| 5 | `AsyncAnthropic` is also blocked — must use sync SDK in thread | [yowanda/Reckora#69](https://github.com/yowanda/Reckora/pull/69) |
| 6 | AgentRouter injects `billing_summary` SSE events that break OpenCode's parser | Discovered live during setup |
| 7 | `thinking` and `output_config` fields trigger AgentRouter's content filter | Discovered live during setup |
| 8 | OpenCode AI SDK calls `/messages` not `/v1/messages` | Discovered live during setup |

---

## Additional quirks discovered during setup

| Issue | Fix |
|---|---|
| WAF also blocks `httpx.AsyncClient` and raw `httpx.Client` | Must use `anthropic.Anthropic` (sync), not `AsyncAnthropic` or bare httpx |
| AgentRouter injects `billing_summary` SSE events | Proxy filters them — OpenCode's Zod parser rejects unknown event types |
| OpenCode sends `thinking: {type: adaptive}` and `output_config` fields | Proxy strips these — AgentRouter's content filter blocks requests containing them |
| AgentRouter requires a `thinking` block on assistant `tool_use` turns, and rejects `redacted_thinking` outright; `@ai-sdk/anthropic` drops unsigned reasoning, leaving some tool-use turns without thinking → ``The `content[].thinking` in the thinking mode must be passed back to the API`` | Proxy drops `redacted_thinking` and injects an empty `thinking` block into tool-use turns that lack one (see [Thinking history](#thinking-history)) |
| OpenCode AI SDK calls `/messages` (no `/v1` prefix) | Proxy mounts on both `/messages` and `/v1/messages` |
| `GET /v1/models` (LLM path) is WAF-blocked | Proxy fetches the list from the dashboard API (`/api/user/models`) using your `set-cookie` credentials, cached for 5 min |

---

## Manual setup

Prefer to do it yourself? Follow these steps.

### Prerequisites

- Python 3.11+
- An [AgentRouter](https://agentrouter.org/register?aff=pP0u) account with an API key (`sk-...` from `agentrouter.org/console/token`)
- One of the supported clients above

### 1. Clone the repo

```bash
git clone https://github.com/Goodnessmbakara/agentrouter-opencode-proxy
cd agentrouter-opencode-proxy
```

### 2. Create the virtualenv and install dependencies

```bash
python3 -m venv .venv
.venv/bin/pip install fastapi "uvicorn[standard]" "httpx<1" "anthropic<1"
```

### 3. Store your API key

```bash
mkdir -p ~/.config/opencode/api_keys
echo 'sk-YOUR_KEY_HERE' > ~/.config/opencode/api_keys/AGENT_ROUTER_API_KEY
chmod 600 ~/.config/opencode/api_keys/AGENT_ROUTER_API_KEY
```

Or set it as an environment variable instead:

```bash
export AGENTROUTER_API_KEY=sk-YOUR_KEY_HERE
```

### 4. Configure your client

#### OpenCode

Add this to `~/.config/opencode/opencode.json`:

```json
{
  "provider": {
    "anthropic": {
      "options": {
        "apiKey": "{file:~/.config/opencode/api_keys/AGENT_ROUTER_API_KEY}",
        "baseURL": "http://localhost:7187"
      },
      "whitelist": ["claude-opus-4-6"]
    }
  }
}
```

#### Claude Code / Cline / Continue / any Anthropic-compatible client

```bash
export ANTHROPIC_BASE_URL=http://localhost:7187
export ANTHROPIC_API_KEY=sk-YOUR_KEY_HERE
```

#### Keeping the OpenCode TUI model picker in sync

The TUI reads its model list from the static `models` map in `opencode.json` — it never
queries the proxy's `/v1/models`. After initial setup (and whenever AgentRouter adds or
retires models), refresh it with:

```bash
agentrouter-proxy sync-models          # rewrites the models block, backs up to opencode.json.bak
agentrouter-proxy sync-models --dry-run  # preview only, changes nothing
```

Then restart the OpenCode TUI. The proxy's `/v1/models` stays live on its own (dashboard
API fetch, 5-min TTL cache, stub fallback) for clients that do query it.

#### Cursor / any OpenAI-compatible client

Set the base URL to `http://localhost:7187` and the API key to your AgentRouter key in the client's settings.

> **Note:** Only `claude-opus-4-6` had reliable capacity on AgentRouter at the time of writing. Other models return `503 no available channel`. Check the [AgentRouter model list](https://agentrouter.org/models) for current availability.

### 5. Start the proxy

```bash
bash start.sh
# or directly:
.venv/bin/python proxy.py
```

You should see:

```
AgentRouter proxy  →  https://agentrouter.org
Listening on http://127.0.0.1:7187
```

### 6. Start your client

Select `anthropic/claude-opus-4-6` (or the equivalent model name in your client).

## Auto-start on macOS (optional)

```bash
cat > ~/Library/LaunchAgents/org.agentrouter.proxy.plist << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>org.agentrouter.proxy</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(pwd)/.venv/bin/python</string>
    <string>$(pwd)/proxy.py</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/agentrouter-proxy.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/agentrouter-proxy.log</string>
</dict>
</plist>
EOF

launchctl load ~/Library/LaunchAgents/org.agentrouter.proxy.plist
```

## Testing the proxy directly

```bash
# Non-streaming
curl -s http://localhost:7187/messages \
  -H "content-type: application/json" \
  -d '{"model":"claude-opus-4-6","max_tokens":50,"messages":[{"role":"user","content":"hello"}]}'

# Streaming
curl -s http://localhost:7187/messages \
  -H "content-type: application/json" \
  -d '{"model":"claude-opus-4-6","max_tokens":50,"stream":true,"messages":[{"role":"user","content":"hello"}]}'
```

A successful response means the WAF check passed and the model has capacity.

## Thinking history

AgentRouter runs reasoning models (e.g. `deepseek-v4-flash`) in **thinking
mode**. In that mode it requires every assistant message containing a
`tool_use` block to also carry a `thinking` block; otherwise it returns:

```
The `content[].thinking` in the thinking mode must be passed back to the API.
```

Vercel's `@ai-sdk/anthropic` (used by OpenCode) silently drops reasoning
parts that lack a provider `signature`, so assistant tool-use turns whose
reasoning was unsigned arrive with no `thinking` block and trip that check.
AgentRouter also cannot deserialize `redacted_thinking` at all (it is not a
recognised content-block variant), which produces a separate schema error.

The proxy therefore normalizes the outbound history. Control it with
`THINKING_HISTORY`:

| Value | Behavior |
|---|---|
| `ensure` *(default)* | Drop `redacted_thinking` and inject a minimal empty `thinking` block into any assistant `tool_use` turn that lacks one; keep existing thinking blocks |
| `strip` | Drop all `thinking` + `redacted_thinking` blocks (for upstreams that reject them) |
| `off` | Pass history through untouched (legacy/debug) |

Notes:

- The injected block is `{"type": "thinking", "thinking": ""}`. Verified
  against upstream: the `thinking` field is required (an empty string is
  accepted) and `signature` is optional.
- Text-only assistant turns do **not** need a thinking block; only turns
  containing `tool_use` do.
- This is request-side only — streamed reasoning still reaches the client and
  renders normally in the TUI.
- It cannot restore reasoning the client already dropped; it only satisfies
  upstream's structural requirement.

Example:

```bash
THINKING_HISTORY=strip bash start.sh
```

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `unauthorized client detected` | WAF blocked — not using proxy | Ensure your client points to `http://localhost:7187`, not agentrouter.org directly |
| `503 no available channel` | Model pool exhausted on agentrouter.org | Try another model or wait and retry |
| `content-blocked` | Non-standard request fields | Proxy strips `thinking` and `output_config` already; if it persists, report an issue |
| ``The `content[].thinking` in the thinking mode must be passed back to the API`` | A `tool_use` turn without a `thinking` block (see [Thinking history](#thinking-history)) | Proxy injects a minimal thinking block by default (`THINKING_HISTORY=ensure`) |
| `unknown variant 'redacted_thinking'` | Upstream can't deserialize redacted thinking | Proxy drops `redacted_thinking` by default |
| `Not Found` from proxy | Wrong path | Proxy handles `/messages` and `/v1/messages` — ensure `baseURL` has no path suffix |
| Port 7187 already in use | Old proxy still running | `lsof -ti :7187 \| xargs kill -9` |
