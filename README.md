# MCP Context-Cost Profiler

**Buy (one-time, HK$129):** https://buy.polar.sh/polar_cl_Ol9F7CfYSmjVrwjjvoqxwVbftaMuF2YXwatpT0AHbcs


**The only cross-client MCP token auditor with a verified-accuracy mode.**

Every MCP server you connect to an AI coding client adds its tool definitions (name, description, input schema) to the model's context. Anthropic engineers have reported tool definitions from a multi-server setup consuming around 55K tokens before any work starts. This tool audits Claude Code, Cursor, Claude Desktop and generic MCP configs in one pass, shows which servers and tools account for the cost, simulates three common fixes, and, when you opt in with `--verify`, checks its own estimates against Anthropic's `count_tokens` API so you can see the error instead of trusting it.

It is Python, standard library only. It makes no network calls unless you pass `--verify`.

## Install

Unzip and run. There is no install step. Python 3.8 or newer.

```
unzip mcp-context-profiler-v1.1.0.zip -d mcp-context-profiler
cd mcp-context-profiler
python3 mcp_profile.py --demo
```

## Where tool data comes from

MCP config files list servers, not tools. The tool definitions have to come from somewhere, in this order of precedence:

1. An inline `"tools"` array on a server entry in the config.
2. A snapshot file: `--tools FILE`, or `mcp-tools.json` in the current directory. Format: `{"server-name": [ {"name", "description", "inputSchema"}, ... ]}`, the `tools` array of a `tools/list` response.
3. `--probe`: the profiler starts each configured stdio server on your machine, runs the MCP `initialize` and `tools/list` calls over stdin/stdout, and stops it. This runs the commands in your config, the same ones your client runs. HTTP servers are not probed (that would need network access); supply a snapshot for those. Use `--save-tools mcp-tools.json` to keep the result.

Servers with no tool data are listed as unmeasured and left out of totals. The tool never guesses a size.

Config locations searched automatically: `.mcp.json`, `.claude/settings.json`, `.claude/settings.local.json`, `~/.claude.json` (user scope, plus the project entry matching the current directory), `~/.claude/settings.json`, `.cursor/mcp.json`, `~/.cursor/mcp.json`, and Claude Desktop's config. Any other JSON file with an `mcpServers` or `servers` block can be passed as an argument. Servers marked `"disabled": true` are skipped. Unknown fields are ignored.

Usage counts for the "disable low-use servers" fix go in `mcp-usage.json` (`{"server-name": calls}`) or `--usage FILE`.

## Usage examples

All output below is real, produced by the built-in demo config (3 servers, 20 tools).

### 1. See a report with no setup

`python3 mcp_profile.py --demo`

```
Summary
Context overhead: 3,451 tokens (estimated) across 3 servers / 20 tools
That is 1.7% of a 200,000-token context window, paid on every request.
Config sources: <built-in demo config>

Per-server breakdown
Server          Transport  Tools  Tokens  Share
--------------  ---------  -----  ------  ----------------
github          stdio         12   2,621   75.9% #########
filesystem      stdio          6     582   16.9% ##
ripgrep-search  stdio          2     248    7.2% #
```

```
Duplicated / overlapping tools
Kind       Tool A                     Tool B                         Overlap tokens
---------  -------------------------  -----------------------------  --------------
same name  filesystem / search_files  ripgrep-search / search_files             137
same name  filesystem / read_file     ripgrep-search / read_file                 91
Overlap tokens = size of the smaller definition (what you would save by dropping one). Total across all pairs: 228.
```

### 2. Profile your own setup and see what to trim

```
python3 mcp_profile.py --probe --save-tools mcp-tools.json
python3 mcp_profile.py --usage my-usage.json
```

The optimization section on the demo data:

```
Optimization simulation
Fix                                    Before  After  Saved  Saved %  USD / month (claude-sonnet)
-------------------------------------  ------  -----  -----  -------  ---------------------------
Defer loading (fetch tools on demand)   3,451  1,043  2,408    69.8%                       $15.89
Disable low-use servers                 3,451  3,203    248     7.2%                        $1.64
Minify descriptions to 80 chars         3,451  2,785    666    19.3%                        $4.40
All fixes combined                      3,451    886  2,565    74.3%                       $16.93
```

The combined row is not the sum of the others: the fixes overlap (a disabled server cannot also be minified), so the combined figure is computed on the pipeline, then bounded: never worse than the best single fix and never more than the sum of the individual savings.

### 3. Produce a shareable Markdown report, or JSON for scripts

```
python3 mcp_profile.py --markdown > report.md
python3 mcp_profile.py --json | python3 -c "import json,sys; print(json.load(sys.stdin)['clients'][0]['total_tokens'])"
```

Markdown excerpt (renders as tables on GitHub and most editors):

```
### Top 5 token hogs

| # | Tool | Server | Tokens | % of total |
| ---: | :--- | :--- | ---: | ---: |
| 1 | list_issues | github | 300 | 8.7% |
| 2 | list_pull_requests | github | 255 | 7.4% |
| 3 | create_pull_request | github | 253 | 7.3% |
| 4 | create_issue | github | 246 | 7.1% |
| 5 | search_code | github | 230 | 6.7% |
```

Pricing is adjustable: `--price my-model=2.50`, `--prices-file prices.json`, `--requests-per-month 5000`, `--model claude-opus`. Run `python3 mcp_profile.py --help` for every option.

### 4. Verify the estimates against the real tokenizer (opt-in)

```
export ANTHROPIC_API_KEY=...        # your own key
python3 mcp_profile.py --verify
```

`--verify` sends each server's tool definitions to Anthropic's `count_tokens` endpoint and adds a table to the report with one row per server and a total row. Columns: `Server`, `Estimated`, `Actual`, `Delta` (estimated minus actual, as a percentage of actual). This table is not reproduced here with numbers because it needs your API key and the real figures depend on your tools and the model.

- It is the only network call in the tool, and it never runs unless you pass `--verify`.
- What is sent: tool names, descriptions and input schemas, plus a one-word user message. Your config file, env values, command arguments and file paths are never sent. The key goes only to `api.anthropic.com` as the `x-api-key` header and is never printed.
- Without `ANTHROPIC_API_KEY` set, `--verify` prints one line to stderr, sends nothing and exits 0.
- Method: one request per server, minus a one-stub-tool baseline request, so the fixed overhead the API adds whenever tools are present is not charged to any server. The table also reports the measured characters-per-token ratio; pass it as `--chars-per-token` to calibrate every other figure in the report.
- `--verify-model MODEL` picks the tokenizer (default `claude-sonnet-5-5`). Different models can tokenize differently. Per-server API errors appear as an error row; the rest of the report is unaffected.
- The network code lives in a separate file, `mcp_verify.py`, so you can audit it in a minute. `mcp_profile.py` itself contains no networking code.

## How this compares to free alternatives

Free, open-source command-line tools that estimate MCP context cost exist, and for a quick single-client number they may be all you need. The differences, stated plainly:

| | Typical free estimators | This tool |
| :--- | :--- | :--- |
| Token counts | Estimates only | Estimates, plus an opt-in `--verify` table of estimated vs actual from `count_tokens` |
| Clients | Usually one client's config | Claude Code (user, project and `.mcp.json`), Cursor, Claude Desktop, and generic `mcpServers` JSON, audited together |
| Output | Numbers printed to the terminal | Terminal report, a shareable Markdown report, and CI-ready JSON (`--json`) |
| Analysis | Totals per server | Per-tool hogs, duplicated tools across servers, monthly cost per model, and simulated savings from three fixes |

"Shareable org reports" means the Markdown output: run it per machine or per repo and paste the reports into an issue, a pull request or a wiki page. For CI, `--json` gives a stable structure you can threshold on (for example, fail a build when `clients[].total_tokens` exceeds a budget you choose).

## Tool Search and deferred loading

Claude Code's Tool Search (deferred MCP loading) reduces session-start overhead for servers it defers. That is good news and it changes the numbers for that client. This tool audits what is loaded across all of your clients and configs: other clients and configs may not defer anything, servers that are not deferred still cost their full size, and a deferred tool's definition still enters context once it is fetched. The "Defer loading" row in the simulation shows what deferral would save, using stated assumptions, not a measurement of any specific client. Use `--verify` to see the real size of what each server contributes.

## How the simulated fixes work

- **Defer loading.** Tools are not sent up front; the model gets a small search stub and fetches definitions as needed. The "after" figure is a 150-token stub, 10 tokens per server for an index, plus 5 full tool definitions per session at the average tool size (`--defer-fetch-tools`). These constants are assumptions, not measurements of any specific client.
- **Disable low-use servers.** Servers with fewer calls than `--min-usage` (default 5) in your usage file are removed. Without a usage file this row is shown as n/a.
- **Minify descriptions.** Every `description` string in each tool and its schema is cut to `--description-limit` characters (default 80). The tool is re-measured after cutting; nothing is extrapolated.

## FAQ

**How accurate are the estimates?**
By default, each tool is serialized to compact JSON and the character count is divided by 4, rounded up. That is a common rule of thumb; it was not calibrated against any specific model here, and no error bound is claimed. To get a real number for your own setup, run `--verify` (see example 4): it reports the actual error per server. JSON schemas with many short keys and punctuation often tokenize denser than 4 characters per token, so the estimate may run low. Real prompts also include client-specific wrapping around each tool. Treat figures as a good basis for comparing servers and ranking fixes, not as billing truth. If you have a real token count for your setup, set `--chars-per-token` so the totals match it.

**Why are dollar figures labelled estimates?**
They multiply your overhead by a request count you set and by illustrative list prices for input tokens. They assume no prompt caching; with caching, cached reads are billed at a reduced rate and the real figure is lower. Prices change; verify them and override with `--price`.

**Does it send my config anywhere?**
Not by default. The tool makes no network calls unless you pass `--verify`, and `mcp_profile.py` imports no networking modules (they are confined to `mcp_verify.py`, loaded only for `--verify`). With `--verify`, only tool definitions are sent to Anthropic's `count_tokens` API, as described in example 4. Environment values and arguments from your config are never printed or sent. `--probe` runs the commands already in your config; it is separate and also opt-in.

**Why does it say a server is unmeasured?**
The config has no tool list and no snapshot or probe result was available. Use `--probe` or `--tools`.

**Does it work on Windows?**
It uses `pathlib` and avoids POSIX-only calls. The development and test machine was Linux; Windows is untested.

## Tests

`python3 -m unittest discover -s tests`

## License

MIT, copyright Timoranjes. See LICENSE.txt.
