# Security Policy

## Reporting a vulnerability

Please report security issues privately via GitHub Security Advisories:

1. Go to the **Security** tab of this repository.
2. Click **Report a vulnerability**.

Please do **not** open a public issue for anything that could expose
credentials. If you must include logs, redact API keys (`sk-...`), session
cookies, and `New-API-User` ids before posting.

## Handling of credentials

This proxy is designed to run locally and handle sensitive credentials:

- **Bind address** — the proxy listens on `127.0.0.1` only (`proxy.py`),
  never `0.0.0.0`. Do not change this without adding authentication;
  exposing the proxy gives anyone who can reach it use of your API keys.
- **Storage** — API keys and dashboard cookies are read from
  `~/.config/opencode/api_keys/` (files are written with mode `600`).
  They are never committed: `.gitignore` excludes `.env`, `pool.json`,
  `pool-state.json`, and `AGENTROUTER_FRIEND_*`.
- **Logs** — key material is read from stdin only, never from argv, and is
  never logged or echoed. Pool status output contains no secrets.
- **Third parties** — the optional key pool shares *your* account bearer key
  with consenting friends' proxy instances, or vice versa. Only pool keys you
  trust, and prefer anchoring balances over sharing session cookies. The
  proxy deliberately does **not** support cookie-based sharing.
- **Referral accounts** — pooled accounts created solely for referral bonuses
  can be clawed back by the upstream provider. This is an accepted risk of the
  pool feature; see `docs/design-pooled-credentials.md`.

## Scope

This project is a compatibility shim for a third-party service
(agentrouter.org). Issues in that service — its WAF, model availability, or
billing — are out of scope here. Report upstream problems to AgentRouter.
