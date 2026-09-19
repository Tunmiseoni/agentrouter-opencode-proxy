---
name: Bug report
about: Something isn't working (proxy errors, WAF blocks, client issues)
title: ""
labels: bug
assignees: ""
---

## What happened

<!-- A clear description of the problem and the exact error message. -->

## Which client are you using?

<!-- OpenCode / Claude Code / Cursor / Cline / Continue / Zed / Aider / other -->

## Environment

- OS:
- Python version (`python3 --version`):
- Proxy version / commit:
- Client version:

## Error output

<!--
Paste the error from the client AND from the proxy log (`/tmp/agentrouter-proxy.log`).
Redact API keys (sk-...), session cookies, and New-API-User ids.
-->

```
paste here
```

## Direct SDK test

<!--
This tells us whether the problem is upstream (AgentRouter) or in the proxy.
Redact your key before pasting output.
-->

```bash
cd ~/.config/opencode/agentrouter-proxy
.venv/bin/python -c "
import anthropic
c = anthropic.Anthropic(api_key='YOUR_KEY', base_url='https://agentrouter.org')
r = c.messages.create(model='claude-opus-4-6', max_tokens=20, messages=[{'role':'user','content':'hi'}])
print(r.content[0].text)
"
```

Result:

- [ ] Passes (returns text) → likely a proxy bug
- [ ] Fails with `unauthorized client detected` → likely an upstream WAF change
- [ ] Fails with `503 no available channel` → upstream capacity issue

## Anything else

<!-- Config snippets, screenshots, workarounds you tried. -->
