# Changelog

## 1.1.0

Repositioned as a cross-client MCP token auditor with a verified-accuracy mode. The estimation engine is unchanged.

- New `--verify` flag: sends each server's tool definitions to Anthropic's `count_tokens` API (needs `ANTHROPIC_API_KEY`) and adds a table of estimated vs actual tokens and delta per server, plus a suggested `--chars-per-token`. Opt-in only; the only network call in the tool. Without the key it prints one line and exits 0 without sending anything.
- New `--verify-model MODEL` (default `claude-sonnet-5-5`).
- Network code is isolated in the new `mcp_verify.py` (stdlib `urllib`), loaded only for `--verify`; `mcp_profile.py` still contains no networking code.
- README: new positioning, "How this compares to free alternatives" section, Tool Search note (deferred loading reduces session-start overhead in one client; this tool audits what is loaded across all clients and configs), updated privacy FAQ.
- LISTING: hook and description rewritten to match.
- Tests: mocked `count_tokens` tests for `--verify` (no network in the suite); isolation test for the network module.

## 1.0.0

Initial release.

- Parses Claude Code (`~/.claude.json` user and project scopes, `.mcp.json`, `settings.json`), Cursor (`.cursor/mcp.json`), Claude Desktop, and generic `mcpServers` / `servers` JSON. Malformed files report path, line, and column.
- Per-server and per-tool token estimates (characters / 4 on compact JSON, adjustable).
- Tool data from inline `tools` lists, snapshot files (`--tools`), or an opt-in local `--probe` of stdio servers.
- Reports: totals, per-server breakdown, top-N token hogs, duplicated or overlapping tools, monthly input-token cost for seven models with price overrides.
- Optimization simulation: deferred loading, disabling low-use servers (usage sidecar), description minification, and an overlap-aware combined total.
- Output: terminal tables (color on a TTY), `--json`, `--markdown`.
- `--demo` built-in sample config.
- Stdlib only, no network access, Python 3.8+.
