#!/usr/bin/env python3
"""MCP Context-Cost Profiler.

Estimates how many context tokens the tool definitions of your MCP servers
consume, reports where they go, prices them, and simulates optimizations.

Stdlib only. Makes no network calls unless you pass --verify, which is handled
by the separate mcp_verify module. Python 3.8+.
"""
import argparse
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

__version__ = "1.1.0"

METHOD = (
    "Each tool definition (name, description, inputSchema) is serialized to compact JSON "
    "and divided by chars-per-token (default 4), rounded up. This is a common rule of thumb, "
    "not calibrated against any specific model, and is not billing truth. JSON-heavy "
    "schemas may tokenize denser than 4 characters per token, and model-specific "
    "tokenizers and the way a client wraps tool definitions in its prompt will shift "
    "the real number. Use --chars-per-token to match a count you trust."
)

# Illustrative list prices, USD per million INPUT tokens. Verify against your
# provider's current price page; override with --price or --prices-file.
PRICES = {
    "claude-opus": 15.00,
    "claude-sonnet": 3.00,
    "claude-haiku": 1.00,
    "gpt-4o": 2.50,
    "gpt-4o-mini": 0.15,
    "gemini-2.5-pro": 1.25,
    "gemini-2.5-flash": 0.30,
}

# Assumptions for the "defer loading" simulation: a small tool-search stub is
# always loaded, each server keeps a one-line index entry, and a session pulls
# in `defer_fetch_tools` full tool definitions on demand.
DEFER_BASE_TOKENS = 150
DEFER_PER_SERVER_TOKENS = 10

DEFAULTS = dict(
    top=5,
    chars_per_token=4.0,
    requests_per_month=2200,
    context_window=200000,
    model="claude-sonnet",
    min_usage=5,
    description_limit=80,
    defer_fetch_tools=5,
)


class ConfigError(Exception):
    """A config/sidecar file could not be used; message includes the path."""


def make_options(**overrides):
    values = dict(DEFAULTS)
    values["prices"] = dict(PRICES)
    values.update(overrides)
    return SimpleNamespace(**values)


# --------------------------------------------------------------------------
# Token estimation
# --------------------------------------------------------------------------

def estimate_tokens(text, chars_per_token=4.0):
    """Heuristic token count: ceil(len/chars_per_token); empty text is 0."""
    if not text:
        return 0
    return int(math.ceil(len(text) / float(chars_per_token)))


def serialize_tool(tool):
    """Compact JSON of the fields a client sends to the model."""
    core = {}
    for key in ("name", "description"):
        if tool.get(key):
            core[key] = tool[key]
    schema = tool.get("inputSchema", tool.get("input_schema"))
    if schema:
        core["input_schema"] = schema
    if not core:
        return ""
    return json.dumps(core, separators=(",", ":"), ensure_ascii=False)


def tool_tokens(tool, chars_per_token=4.0):
    return estimate_tokens(serialize_tool(tool), chars_per_token)


# --------------------------------------------------------------------------
# Config discovery and parsing
# --------------------------------------------------------------------------

def load_json(path):
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise ConfigError("%s: not valid UTF-8 text" % path)
    except OSError as exc:
        raise ConfigError("%s: cannot read file (%s)" % (path, exc.strerror or exc))
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError("%s: malformed JSON at line %d, column %d: %s"
                          % (path, exc.lineno, exc.colno, exc.msg))


def infer_client(path):
    path = Path(path)
    parts = [p.lower() for p in path.parts]
    name = path.name.lower()
    if ".cursor" in parts:
        return "Cursor"
    if name == "claude_desktop_config.json":
        return "Claude Desktop"
    if ".claude" in parts or name in (".claude.json", ".mcp.json"):
        return "Claude Code"
    return "Generic"


def discover_configs(home, cwd):
    """Return existing (path, client) pairs in precedence order."""
    home, cwd = Path(home), Path(cwd)
    candidates = [
        cwd / ".claude" / "settings.local.json",
        cwd / ".mcp.json",
        cwd / ".claude" / "settings.json",
        home / ".claude.json",
        home / ".claude" / "settings.json",
        home / ".claude" / "settings.local.json",
        cwd / ".cursor" / "mcp.json",
        home / ".cursor" / "mcp.json",
        home / ".config" / "Claude" / "claude_desktop_config.json",
        home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
    ]
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "Claude" / "claude_desktop_config.json")
    found, seen = [], set()
    for cand in candidates:
        try:
            ok = cand.is_file()
            key = str(cand.resolve())
        except OSError:
            continue
        if ok and key not in seen:
            seen.add(key)
            found.append((cand, infer_client(cand)))
    return found


def _norm_path(value):
    return os.path.normcase(str(value).replace("\\", "/").rstrip("/"))


def _looks_like_server(value):
    return isinstance(value, dict) and any(k in value for k in ("command", "url", "type", "args"))


def _find_blocks(data, project):
    """Return [(scope, {name: raw})] of server maps found in a parsed config."""
    blocks = []
    projects = data.get("projects")
    if isinstance(projects, dict):
        for ppath, pval in projects.items():
            if (isinstance(pval, dict) and isinstance(pval.get("mcpServers"), dict)
                    and _norm_path(ppath) == _norm_path(project)):
                blocks.append(("project", pval["mcpServers"]))
    for key in ("mcpServers", "servers"):
        if isinstance(data.get(key), dict):
            blocks.append(("user" if key == "mcpServers" else "servers", data[key]))
    if not blocks and data and all(_looks_like_server(v) for v in data.values()):
        blocks.append(("bare", data))
    return blocks


def _disabled_names(data, project):
    names = set()
    sources = [data]
    projects = data.get("projects")
    if isinstance(projects, dict):
        sources += [v for k, v in projects.items()
                    if isinstance(v, dict) and _norm_path(k) == _norm_path(project)]
    for src in sources:
        lst = src.get("disabledMcpjsonServers")
        if isinstance(lst, list):
            names.update(x for x in lst if isinstance(x, str))
    return names


def normalize_tool(raw):
    """Accept an MCP tools/list entry; return normalized dict or None."""
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str) or not raw["name"]:
        return None
    tool = {"name": raw["name"]}
    if isinstance(raw.get("description"), str):
        tool["description"] = raw["description"]
    schema = raw.get("inputSchema", raw.get("input_schema"))
    if isinstance(schema, dict):
        tool["inputSchema"] = schema
    return tool


def normalize_tools(raw_list, label, warnings):
    out = []
    for item in raw_list:
        tool = normalize_tool(item)
        if tool is None:
            warnings.append("%s: skipped a tool entry without a string 'name'" % label)
        else:
            out.append(tool)
    return out


def parse_config(path, project=None, warnings=None):
    """Parse one config file into a list of server dicts."""
    warnings = warnings if warnings is not None else []
    project = project or os.getcwd()
    path = Path(path)
    data = load_json(path)
    if not isinstance(data, dict):
        raise ConfigError("%s: top-level JSON value must be an object" % path)
    disabled_json = _disabled_names(data, project)
    servers = []
    for scope, block in _find_blocks(data, project):
        for name, raw in block.items():
            if not isinstance(raw, dict):
                warnings.append("%s: server '%s' is not an object, skipped" % (path, name))
                continue
            url = raw.get("url") if isinstance(raw.get("url"), str) else None
            command = raw.get("command") if isinstance(raw.get("command"), str) else None
            transport = raw.get("type") if isinstance(raw.get("type"), str) else (
                "http" if url and not command else "stdio")
            args = [str(a) for a in raw["args"]] if isinstance(raw.get("args"), list) else []
            env = raw.get("env") if isinstance(raw.get("env"), dict) else {}
            inline = None
            if isinstance(raw.get("tools"), list):
                inline = normalize_tools(raw["tools"], "%s (%s)" % (path, name), warnings)
            disabled = bool(raw.get("disabled")) or (
                path.name == ".mcp.json" and name in disabled_json)
            servers.append({
                "name": name, "transport": transport, "command": command, "args": args,
                "url": url, "env": env, "cwd": raw.get("cwd") if isinstance(raw.get("cwd"), str) else None,
                "disabled": disabled, "inline_tools": inline,
                "source": str(path), "scope": scope,
            })
    if not servers:
        warnings.append("%s: no MCP servers found" % path)
    return servers


def load_configs(pairs, project, warnings):
    """pairs: [(path, client)] -> ordered {client: [servers]} (first name wins)."""
    clients = {}
    for path, client in pairs:
        try:
            servers = parse_config(path, project, warnings)
        except ConfigError as exc:
            warnings.append(str(exc))
            continue
        bucket = clients.setdefault(client, [])
        known = {s["name"] for s in bucket}
        for srv in servers:
            if srv["name"] not in known:
                known.add(srv["name"])
                bucket.append(srv)
    return {k: v for k, v in clients.items() if v}


# --------------------------------------------------------------------------
# Sidecars: tool snapshots and usage counts
# --------------------------------------------------------------------------

def parse_tools_snapshot(data, label="tools file", warnings=None):
    """{server: [tools]} | {server: {"tools": [...]}} | {"servers": {...}} wrappers."""
    warnings = warnings if warnings is not None else []
    if not isinstance(data, dict):
        raise ConfigError("%s: expected a JSON object mapping server name to tools" % label)
    if isinstance(data.get("servers"), dict):
        data = data["servers"]
    result = {}
    for name, value in data.items():
        if isinstance(value, dict) and isinstance(value.get("result"), dict):
            value = value["result"]
        if isinstance(value, dict):
            value = value.get("tools")
        if not isinstance(value, list):
            warnings.append("%s: server '%s' has no tools list, ignored" % (label, name))
            continue
        result[name] = normalize_tools(value, "%s (%s)" % (label, name), warnings)
    return result


def parse_usage(data, label="usage file", warnings=None):
    warnings = warnings if warnings is not None else []
    if not isinstance(data, dict):
        raise ConfigError("%s: expected a JSON object mapping server name to a count" % label)
    if isinstance(data.get("usage"), dict):
        data = data["usage"]
    result = {}
    for name, value in data.items():
        if isinstance(value, dict):
            value = value.get("calls", value.get("count"))
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            warnings.append("%s: count for '%s' is not a non-negative number, ignored" % (label, name))
            continue
        result[name] = value
    return result


def find_sidecar(cwd, names):
    for rel in names:
        cand = Path(cwd) / rel
        if cand.is_file():
            return cand
    return None


# --------------------------------------------------------------------------
# Optional live probe (opt-in): run a stdio server locally and ask tools/list
# --------------------------------------------------------------------------

class ProbeError(Exception):
    pass


def probe_server(srv, timeout=20.0):
    """Launch a configured stdio server and return its tools/list result."""
    if srv.get("transport") not in ("stdio", None) or not srv.get("command"):
        raise ProbeError("only stdio servers with a command can be probed")
    exe = shutil.which(srv["command"]) or srv["command"]
    env = dict(os.environ)
    env.update({str(k): str(v) for k, v in srv.get("env", {}).items()})
    try:
        proc = subprocess.Popen([exe] + srv.get("args", []), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env=env, cwd=srv.get("cwd") or None,
                                universal_newlines=True, encoding="utf-8", bufsize=1)
    except OSError as exc:
        raise ProbeError("cannot start '%s': %s" % (srv["command"], exc.strerror or exc))
    lines = queue.Queue()

    def pump():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()

    def send(msg):
        try:
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()
        except (OSError, ValueError):
            raise ProbeError("server closed its input")

    def wait_for(msg_id):
        while True:
            try:
                line = lines.get(timeout=timeout)
            except queue.Empty:
                raise ProbeError("timed out after %.0fs" % timeout)
            if line is None:
                raise ProbeError("server exited before answering")
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("id") == msg_id and "method" not in msg:
                if "error" in msg:
                    raise ProbeError("server returned an error: %s" % json.dumps(msg["error"])[:120])
                return msg.get("result") or {}

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "mcp-profile", "version": __version__}}})
        wait_for(1)
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools, cursor, next_id = [], None, 2
        while True:
            params = {"cursor": cursor} if cursor else {}
            send({"jsonrpc": "2.0", "id": next_id, "method": "tools/list", "params": params})
            result = wait_for(next_id)
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            next_id += 1
            if not cursor:
                return tools
    finally:
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait()
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except (OSError, ValueError):
                pass


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------

def minify_tool(tool, limit):
    """Truncate every string 'description' in a tool (including schema properties)."""
    def walk(obj):
        if isinstance(obj, dict):
            out = {}
            for key, val in obj.items():
                if key == "description" and isinstance(val, str):
                    if limit <= 0:
                        continue
                    out[key] = val[:limit].rstrip() if len(val) > limit else val
                else:
                    out[key] = walk(val)
            return out
        if isinstance(obj, list):
            return [walk(x) for x in obj]
        return obj
    return walk(tool)


def _norm_name(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _words(text):
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def find_overlaps(measured, cpt, limit=10):
    """Tools on different servers that share a name or have similar descriptions."""
    items = []
    for srv in measured:
        for tool in srv["tools"]:
            items.append((srv["name"], tool, tool_tokens(tool, cpt),
                          _norm_name(tool["name"]), _words(tool.get("description"))))
    found = []
    for i in range(len(items)):
        sa, ta, ka, na, wa = items[i]
        for j in range(i + 1, len(items)):
            sb, tb, kb, nb, wb = items[j]
            if sa == sb:
                continue
            kind = None
            if na == nb:
                kind = "same name"
            elif len(wa) >= 6 and len(wb) >= 6:
                if len(wa & wb) / float(len(wa | wb)) >= 0.7:
                    kind = "similar description"
            if kind:
                found.append({"kind": kind,
                              "a": {"server": sa, "tool": ta["name"], "tokens": ka},
                              "b": {"server": sb, "tool": tb["name"], "tokens": kb},
                              "wasted_tokens": min(ka, kb)})
    found.sort(key=lambda d: (-d["wasted_tokens"], d["a"]["tool"]))
    return found[:limit], sum(d["wasted_tokens"] for d in found)


def simulate(measured, usage, opts):
    """Return optimization scenarios. `measured` items: {"name", "tools": [...]}."""
    cpt = opts.chars_per_token
    price = opts.prices[opts.model]

    def total(servers):
        return sum(tool_tokens(t, cpt) for s in servers for t in s["tools"])

    def deferred(servers, current):
        n_tools = sum(len(s["tools"]) for s in servers)
        if not n_tools:
            return current
        avg = current / float(n_tools)
        residual = (DEFER_BASE_TOKENS + DEFER_PER_SERVER_TOKENS * len(servers)
                    + min(opts.defer_fetch_tools, n_tools) * avg)
        return min(current, int(round(residual)))

    def usd(saved):
        return saved / 1e6 * price * opts.requests_per_month

    def row(key, label, before, after, note, applicable=True):
        after = min(before, max(0, after))
        saved = before - after
        return {"key": key, "label": label, "before": before, "after": after,
                "saved": saved, "saved_pct": (100.0 * saved / before) if before else 0.0,
                "monthly_usd_saved": usd(saved), "note": note, "applicable": applicable}

    base = total(measured)
    rows = []

    after_defer = deferred(measured, base)
    rows.append(row("defer", "Defer loading (fetch tools on demand)", base, after_defer,
                    "keeps ~%d-token stub + %d/server index + %d tools fetched per session"
                    % (DEFER_BASE_TOKENS, DEFER_PER_SERVER_TOKENS, opts.defer_fetch_tools)))

    if usage is None:
        keep, dropped = measured, []
        rows.append(row("disable", "Disable low-use servers", base, base,
                        "skipped: no usage file (see --usage)", applicable=False))
    else:
        dropped = [s for s in measured if usage.get(s["name"], 0) < opts.min_usage]
        keep = [s for s in measured if s not in dropped]
        names = ", ".join(s["name"] for s in dropped) or "none"
        rows.append(row("disable", "Disable low-use servers", base, total(keep),
                        "usage < %g calls: %s" % (opts.min_usage, names)))

    mini = [{"name": s["name"], "tools": [minify_tool(t, opts.description_limit) for t in s["tools"]]}
            for s in measured]
    rows.append(row("minify", "Minify descriptions to %d chars" % opts.description_limit,
                    base, total(mini), "truncates tool and property descriptions"))

    keep_names = {s["name"] for s in keep}
    mini_keep = [s for s in mini if s["name"] in keep_names]
    cur = total(mini_keep)
    combined_after = deferred(mini_keep, cur)
    # Combining fixes can never do worse than the best single fix, nor save more
    # than the fixes do separately (they overlap).
    combined_after = min(combined_after, min(r["after"] for r in rows))
    combined_after = max(combined_after, base - sum(r["saved"] for r in rows))
    rows.append(row("combined", "All fixes combined", base, combined_after,
                    "applied in order: disable, minify, defer; never below the best single fix or above the sum of all",
                    applicable=any(r["applicable"] for r in rows)))
    return rows


def analyze_client(label, servers, tools_by_server, usage, opts):
    cpt = opts.chars_per_token
    srv_out, measured, notes = [], [], []
    for srv in servers:
        rec = {"name": srv["name"], "transport": srv["transport"],
               "command": Path(srv["command"]).name if srv.get("command") else None,
               "source": srv["source"], "usage": (usage or {}).get(srv["name"]),
               "tool_count": 0, "tokens": 0, "tools": []}
        tools = srv["inline_tools"] if srv["inline_tools"] is not None else tools_by_server.get(srv["name"])
        if srv["disabled"]:
            rec["status"] = "disabled"
        elif tools is None:
            rec["status"] = "unmeasured"
            notes.append(srv["name"])
        else:
            rec["status"] = "measured"
            rec["tools"] = [{"name": t["name"], "tokens": tool_tokens(t, cpt)} for t in tools]
            rec["tool_count"] = len(tools)
            rec["tokens"] = sum(t["tokens"] for t in rec["tools"])
            measured.append({"name": srv["name"], "tools": tools})
        srv_out.append(rec)
    total = sum(s["tokens"] for s in srv_out)
    for rec in srv_out:
        rec["share_pct"] = (100.0 * rec["tokens"] / total) if total else 0.0
    hogs = sorted(((t["tokens"], s["name"], t["name"]) for s in srv_out for t in s["tools"]),
                  key=lambda x: (-x[0], x[1], x[2]))[:opts.top]
    overlaps, overlap_tokens = find_overlaps(measured, cpt)
    cost = [{"model": m, "usd_per_mtok": p,
             "monthly_tokens": total * opts.requests_per_month,
             "monthly_usd": total / 1e6 * p * opts.requests_per_month}
            for m, p in sorted(opts.prices.items(), key=lambda kv: -kv[1])]
    return {
        "client": label,
        "sources": sorted({s["source"] for s in srv_out}),
        "total_tokens": total,
        "tool_count": sum(s["tool_count"] for s in srv_out),
        "server_count": len(srv_out),
        "context_window_pct": 100.0 * total / opts.context_window if opts.context_window else 0.0,
        "servers": srv_out,
        "unmeasured": notes,
        "top_tools": [{"rank": i + 1, "tokens": t, "server": s, "tool": n,
                       "share_pct": (100.0 * t / total) if total else 0.0}
                      for i, (t, s, n) in enumerate(hogs)],
        "overlaps": overlaps,
        "overlap_wasted_tokens": overlap_tokens,
        "cost": cost,
        "optimizations": simulate(measured, usage, opts) if measured else [],
    }


def build_report(clients, tools_by_server, usage, opts, warnings, demo=False):
    return {
        "tool": "mcp-profile", "version": __version__, "demo": demo,
        "method": {"description": METHOD, "chars_per_token": opts.chars_per_token},
        "settings": {"requests_per_month": opts.requests_per_month,
                     "context_window": opts.context_window, "reference_model": opts.model,
                     "min_usage": opts.min_usage, "description_limit": opts.description_limit,
                     "defer_fetch_tools": opts.defer_fetch_tools,
                     "prices_usd_per_mtok_input": opts.prices},
        "clients": [analyze_client(label, servers, tools_by_server, usage, opts)
                    for label, servers in clients.items()],
        "warnings": list(warnings),
    }


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def fmt_int(n):
    return "{:,}".format(int(round(n)))


def fmt_usd(x):
    return "${:,.2f}".format(x)


def bar(pct, width=12):
    return "#" * int(round(width * pct / 100.0))


def sections(cr, report):
    """Format-neutral section list for one client report."""
    s = report["settings"]
    out = []
    out.append({"title": "Summary", "text": [
        "Context overhead: %s tokens (estimated) across %d servers / %d tools"
        % (fmt_int(cr["total_tokens"]), cr["server_count"], cr["tool_count"]),
        "That is %.1f%% of a %s-token context window, paid on every request."
        % (cr["context_window_pct"], fmt_int(s["context_window"])),
        "Config sources: " + ", ".join(cr["sources"])]})
    rows = [[v["name"], v["transport"], v["tool_count"] if v["status"] == "measured" else "-",
             fmt_int(v["tokens"]) if v["status"] == "measured" else v["status"],
             "%5.1f%% %s" % (v["share_pct"], bar(v["share_pct"])) if v["status"] == "measured" else "-"]
            for v in sorted(cr["servers"], key=lambda v: -v["tokens"])]
    text = []
    if cr["unmeasured"]:
        text.append("No tool data for: %s. Excluded from totals; supply --tools FILE or use --probe."
                    % ", ".join(cr["unmeasured"]))
    out.append({"title": "Per-server breakdown", "headers": ["Server", "Transport", "Tools", "Tokens", "Share"],
                "aligns": "llrrl", "rows": rows, "text": text})
    if not cr["total_tokens"] and not cr["tool_count"]:
        out.append({"title": "Next step", "text": [
            "No tool definitions were available, so nothing could be measured. Run with --probe to ask "
            "your stdio servers for their tools, or pass --tools FILE (see --help), or try --demo."]})
        return out
    out.append({"title": "Top %d token hogs" % len(cr["top_tools"]),
                "headers": ["#", "Tool", "Server", "Tokens", "% of total"], "aligns": "rllrr",
                "rows": [[t["rank"], t["tool"], t["server"], fmt_int(t["tokens"]), "%.1f%%" % t["share_pct"]]
                         for t in cr["top_tools"]], "text": []})
    if cr["overlaps"]:
        out.append({"title": "Duplicated / overlapping tools",
                    "headers": ["Kind", "Tool A", "Tool B", "Overlap tokens"], "aligns": "lllr",
                    "rows": [[o["kind"], "%s / %s" % (o["a"]["server"], o["a"]["tool"]),
                              "%s / %s" % (o["b"]["server"], o["b"]["tool"]), fmt_int(o["wasted_tokens"])]
                             for o in cr["overlaps"]],
                    "text": ["Overlap tokens = size of the smaller definition (what you would save by dropping one). "
                             "Total across all pairs: %s." % fmt_int(cr["overlap_wasted_tokens"])]})
    else:
        out.append({"title": "Duplicated / overlapping tools", "text": ["None found."]})
    out.append({"title": "Estimated monthly input-token cost",
                "headers": ["Model", "USD / M input", "Tokens / month", "Est. USD / month"], "aligns": "lrrr",
                "rows": [[c["model"], "%.2f" % c["usd_per_mtok"], fmt_int(c["monthly_tokens"]),
                          fmt_usd(c["monthly_usd"])] for c in cr["cost"]],
                "text": ["Assumes %s requests/month, each carrying the full tool list, with no prompt caching. "
                         "Prices are illustrative; override with --price." % fmt_int(s["requests_per_month"])]})
    ver = cr.get("verification")
    if ver:
        def _row(r):
            if r["error"]:
                return [r["server"], fmt_int(r["estimated"]), "error", r["error"]]
            return [r["server"], fmt_int(r["estimated"]), fmt_int(r["actual"]),
                    "%+.1f%%" % r["delta_pct"] if r["delta_pct"] is not None else "n/a"]
        rows = [_row(r) for r in ver["rows"]]
        if ver["total_actual"]:
            rows.append(["TOTAL (verified servers)", fmt_int(ver["total_estimated"]),
                         fmt_int(ver["total_actual"]), "%+.1f%%" % ver["total_delta_pct"]])
        text = ["Actual = Anthropic count_tokens for model %s, minus a one-stub-tool baseline (%s tokens) so the "
                "API's fixed tool-use overhead is not charged to any server. Delta = (estimated - actual) / actual."
                % (ver["model"], fmt_int(ver["baseline_tokens"]))]
        if ver["suggested_chars_per_token"]:
            text.append("Measured ratio on your tools: %.2f characters per token. Pass --chars-per-token %.2f to "
                        "calibrate every figure in this report." % ((ver["suggested_chars_per_token"],) * 2))
        out.append({"title": "Verification (estimated vs actual tokens)",
                    "headers": ["Server", "Estimated", "Actual", "Delta"], "aligns": "lrrr",
                    "rows": rows, "text": text})
    if cr["optimizations"]:
        out.append({"title": "Optimization simulation",
                    "headers": ["Fix", "Before", "After", "Saved", "Saved %", "USD / month (%s)" % s["reference_model"]],
                    "aligns": "lrrrrr",
                    "rows": [[o["label"], fmt_int(o["before"]), fmt_int(o["after"]),
                              fmt_int(o["saved"]) if o["applicable"] else "n/a",
                              "%.1f%%" % o["saved_pct"] if o["applicable"] else "n/a",
                              fmt_usd(o["monthly_usd_saved"]) if o["applicable"] else "n/a"]
                             for o in cr["optimizations"]],
                    "text": ["%s: %s" % (o["label"], o["note"]) for o in cr["optimizations"]]})
    return out


def table_lines(headers, rows, aligns, markdown):
    rows = [[str(c) for c in r] for r in rows]
    if markdown:
        sep = ["---:" if a == "r" else ":---" for a in aligns]
        lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(sep) + " |"]
        lines += ["| " + " | ".join(c.strip().replace("|", "\\|") for c in r) + " |" for r in rows]
        return lines
    cut = [[c if len(c) <= 44 else c[:41] + "..." for c in r] for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in cut]) for i, h in enumerate(headers)]

    def fmt(cells):
        parts = [c.rjust(w) if a == "r" else c.ljust(w) for c, w, a in zip(cells, widths, aligns)]
        return "  ".join(parts).rstrip()
    return [fmt(headers), "  ".join("-" * w for w in widths)] + [fmt(r) for r in cut]


def render_text(report, color=False):
    def paint(text, code):
        return "\033[%sm%s\033[0m" % (code, text) if color else text
    lines = [paint("MCP Context-Cost Profiler v%s" % report["version"], "1")
             + (paint("  (demo data)", "33") if report["demo"] else ""),
             paint("Estimates use a chars/4 heuristic on serialized tool definitions "
                   "(a rule of thumb, uncalibrated; not billing truth).", "2"), ""]
    for cr in report["clients"]:
        lines.append(paint("=== %s ===" % cr["client"], "1;36"))
        for sec in sections(cr, report):
            lines += ["", paint(sec["title"], "1")]
            if sec.get("headers"):
                tl = table_lines(sec["headers"], sec["rows"], sec["aligns"], False)
                lines += [paint(tl[0], "1")] + tl[1:]
            lines += sec["text"]
        lines.append("")
    for w in report["warnings"]:
        lines.append(paint("warning: " + w, "33"))
    return "\n".join(lines).rstrip() + "\n"


def render_markdown(report):
    lines = ["# MCP Context-Cost Report", ""]
    if report["demo"]:
        lines += ["_Generated from the built-in demo config._", ""]
    lines += ["Generated by mcp-profile v%s. **Method:** %s" % (report["version"], report["method"]["description"]), ""]
    for cr in report["clients"]:
        lines += ["## %s" % cr["client"], ""]
        for sec in sections(cr, report):
            lines += ["### %s" % sec["title"], ""]
            if sec.get("headers"):
                lines += table_lines(sec["headers"], sec["rows"], sec["aligns"], True) + [""]
                lines += ["- " + t for t in sec["text"]] + ([""] if sec["text"] else [])
            else:
                lines += ["- " + t for t in sec["text"]] + [""]
    if report["warnings"]:
        lines += ["## Warnings", ""] + ["- " + w for w in report["warnings"]] + [""]
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------
# Built-in demo
# --------------------------------------------------------------------------

def _tool(name, desc, props, required=()):
    return {"name": name, "description": desc, "inputSchema": {
        "type": "object",
        "properties": {p: {"type": t, "description": d} for p, t, d in props},
        "required": list(required)}}


_P_REPO = [("owner", "string", "Account or organization that owns the repository, for example 'acme-corp'. Case-insensitive."),
           ("repo", "string", "Repository name without the owner prefix, for example 'billing-service'.")]
_P_PAGE = [("page", "number", "Page number for paginated results, starting at 1. Combine with perPage to walk large result sets."),
           ("perPage", "number", "Number of results per page. Defaults to 30, maximum 100. Smaller pages return faster and use less context.")]

DEMO_TOOLS = {
    "filesystem": [
        _tool("read_file", "Read the complete contents of a file from the file system as text. Handles various text encodings and returns an error for paths outside the allowed directories.",
              [("path", "string", "Absolute or workspace-relative path of the file to read.")], ["path"]),
        _tool("write_file", "Create a new file or completely overwrite an existing file with new content. Use with caution as it will overwrite existing files without warning.",
              [("path", "string", "Path of the file to write."), ("content", "string", "Full text content to write to the file.")], ["path", "content"]),
        _tool("list_directory", "Get a detailed listing of all files and directories in a specified path. Results distinguish files from directories with [FILE] and [DIR] prefixes.",
              [("path", "string", "Directory to list.")], ["path"]),
        _tool("search_files", "Recursively search for files and directories matching a pattern. Searches through all subdirectories from the starting path and matches names case-insensitively.",
              [("path", "string", "Directory to start the search from."), ("pattern", "string", "Substring or glob pattern to match against file names."),
               ("excludePatterns", "array", "Optional list of glob patterns to skip, such as node_modules.")], ["path", "pattern"]),
        _tool("get_file_info", "Retrieve detailed metadata about a file or directory: size, creation time, last modified time, permissions, and whether it is a file or directory.",
              [("path", "string", "Path to inspect.")], ["path"]),
        _tool("create_directory", "Create a new directory or ensure a directory exists. Can create multiple nested directories in one operation, and succeeds silently if the directory already exists.",
              [("path", "string", "Directory path to create.")], ["path"]),
    ],
    "github": [
        _tool("create_issue", "Create a new issue in a GitHub repository. Provide a clear title and a body that describes the problem, the expected behavior, and steps to reproduce. Labels and assignees must already exist or be valid collaborators, otherwise the request is rejected with a validation error.",
              _P_REPO + [("title", "string", "Short summary of the issue, ideally under 80 characters."), ("body", "string", "Markdown description of the issue."),
                         ("labels", "array", "Names of existing labels to apply."), ("assignees", "array", "Usernames of collaborators to assign.")], ["owner", "repo", "title"]),
        _tool("list_issues", "List issues in a repository with optional filtering by state, labels, and update time. Results are sorted and paginated; pull requests are excluded. Use this to survey open work before creating a new issue so that duplicates are avoided.",
              _P_REPO + [("state", "string", "Filter by state: open, closed, or all. Defaults to open."), ("labels", "array", "Only return issues carrying all of these labels."),
                         ("since", "string", "ISO 8601 timestamp; only issues updated at or after this time are returned.")] + _P_PAGE, ["owner", "repo"]),
        _tool("get_issue", "Fetch the full details of a single issue by number, including title, body, state, labels, assignees, milestone, and timestamps. Comments are not included; use the comment listing tool for the discussion thread.",
              _P_REPO + [("issue_number", "number", "The number of the issue, as shown after the # sign in the web interface.")], ["owner", "repo", "issue_number"]),
        _tool("update_issue", "Update an existing issue. Any field that is omitted is left unchanged, so send only what should change. Closing an issue with a state_reason of not_planned records that the work was declined rather than completed.",
              _P_REPO + [("issue_number", "number", "Number of the issue to update."), ("title", "string", "New title."), ("body", "string", "New Markdown body."),
                         ("state", "string", "Set to open or closed.")], ["owner", "repo", "issue_number"]),
        _tool("add_issue_comment", "Add a comment to an existing issue or pull request conversation. The comment body supports GitHub-flavored Markdown, including task lists, mentions, and fenced code blocks. Rate limits apply to rapid successive comments.",
              _P_REPO + [("issue_number", "number", "Issue or pull request number to comment on."), ("body", "string", "Markdown text of the comment.")], ["owner", "repo", "issue_number", "body"]),
        _tool("create_pull_request", "Open a new pull request from a head branch into a base branch. The head branch must already be pushed to the remote. Draft pull requests do not notify reviewers and cannot be merged until marked ready for review.",
              _P_REPO + [("title", "string", "Title of the pull request."), ("head", "string", "Branch containing the changes, or user:branch for forks."),
                         ("base", "string", "Branch the changes should be merged into."), ("body", "string", "Markdown description of the change."),
                         ("draft", "boolean", "Open the pull request as a draft.")], ["owner", "repo", "title", "head", "base"]),
        _tool("list_pull_requests", "List pull requests in a repository, filtered by state, head branch, or base branch. Returns summaries only; request an individual pull request for its full description and review status. Results are paginated.",
              _P_REPO + [("state", "string", "open, closed, or all."), ("base", "string", "Only pull requests targeting this branch.")] + _P_PAGE, ["owner", "repo"]),
        _tool("get_pull_request_diff", "Return the unified diff for a pull request. Large diffs are truncated by the server, so for very big changes prefer reading individual files. The diff includes file renames and binary file markers but not review comments.",
              _P_REPO + [("pull_number", "number", "Pull request number.")], ["owner", "repo", "pull_number"]),
        _tool("merge_pull_request", "Merge a pull request using the chosen strategy. The call fails if required status checks have not passed, if there are unresolved merge conflicts, or if branch protection rules require approvals that are missing.",
              _P_REPO + [("pull_number", "number", "Pull request number."), ("merge_method", "string", "merge, squash, or rebase. Defaults to merge."),
                         ("commit_title", "string", "Title for the merge commit when using merge or squash.")], ["owner", "repo", "pull_number"]),
        _tool("search_code", "Search for code across GitHub repositories using the code search syntax. Supports qualifiers such as repo:, language:, path:, and filename:. Results show matching file paths and text fragments, and are limited to files under 384 KB on the default branch.",
              [("q", "string", "Search query including any qualifiers, for example 'addClass repo:jquery/jquery language:js'."),
               ("sort", "string", "Sort field; only 'indexed' is supported."), ("order", "string", "asc or desc.")] + _P_PAGE, ["q"]),
        _tool("search_repositories", "Find repositories by name, topic, language, or description. Supports qualifiers such as stars:>100, language:python, and topic:cli. Returns repository names, descriptions, star counts, and last update times for the top matches.",
              [("query", "string", "Repository search query.")] + _P_PAGE, ["query"]),
        _tool("get_file_contents", "Get the contents of a file or the entries of a directory from a repository at a given ref. Files larger than one megabyte are not returned inline. Use this to read source files without cloning the repository locally.",
              _P_REPO + [("path", "string", "Path inside the repository."), ("ref", "string", "Branch, tag, or commit SHA. Defaults to the default branch.")], ["owner", "repo", "path"]),
    ],
    "ripgrep-search": [
        _tool("search_files", "Search the contents of files under a directory for a regular expression using ripgrep. Returns matching lines with file paths and line numbers, and respects .gitignore rules by default.",
              [("pattern", "string", "Regular expression to search for."), ("path", "string", "Directory to search. Defaults to the workspace root."),
               ("glob", "string", "Optional glob filter such as '*.py'.")], ["pattern"]),
        _tool("read_file", "Read a range of lines from a file, which is useful for large files where the whole content would be too long to include in the conversation.",
              [("path", "string", "File to read."), ("start_line", "number", "First line to return, 1-based."), ("end_line", "number", "Last line to return.")], ["path"]),
    ],
}

DEMO_CONFIG = {
    "numStartups": 212,
    "mcpServers": {
        "filesystem": {"type": "stdio", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."], "env": {}},
        "github": {"type": "stdio", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"], "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "<redacted>"}},
        "ripgrep-search": {"type": "stdio", "command": "uvx", "args": ["ripgrep-mcp"], "env": {}},
    },
}

DEMO_USAGE = {"filesystem": 140, "github": 62, "ripgrep-search": 2}


def demo_inputs(opts):
    tmp_warn = []
    servers = []
    for name, raw in DEMO_CONFIG["mcpServers"].items():
        servers.append({"name": name, "transport": raw["type"], "command": raw["command"],
                        "args": raw["args"], "url": None, "env": {}, "cwd": None, "disabled": False,
                        "inline_tools": None, "source": "<built-in demo config>", "scope": "user"})
    return {"Claude Code": servers}, dict(DEMO_TOOLS), dict(DEMO_USAGE), tmp_warn


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

HELP_INTRO = """\
what this measures
  An MCP client sends every connected server's tool definitions (name,
  description, input schema) to the model on every request. This tool
  estimates that overhead per server and per tool, prices it, and simulates
  three fixes. Configs list servers but not tools, so tool definitions come
  from one of: an inline "tools" list in the config, a snapshot file
  (--tools), or --probe, which runs your stdio servers locally.

quick start
  python3 mcp_profile.py --demo                # see a full report on built-in data
  python3 mcp_profile.py                       # auto-find Claude Code / Cursor configs
  python3 mcp_profile.py .mcp.json --probe     # measure real tools by asking each server
  python3 mcp_profile.py --probe --save-tools mcp-tools.json   # cache for later runs
  python3 mcp_profile.py --markdown > report.md                # shareable report
"""

HELP_EPILOG = """\
sidecar files (found automatically in the current directory, or via flags)
  mcp-tools.json   {"server": [ {name, description, inputSchema}, ... ]}
                   (the "tools" array from a tools/list response)
  mcp-usage.json   {"server": call_count}   enables the 'disable low-use' fix

accuracy
  Tokens = ceil(characters / 4) of each tool's compact JSON. A common rule of
  thumb, not calibrated against any model; JSON-heavy schemas may tokenize
  denser than 4 chars/token, so the estimate can run low.
  It is not billing truth. Use --chars-per-token to match a real count.
  Dollar figures assume no prompt caching and illustrative list prices
  (override with --price MODEL=USD_PER_MILLION_INPUT_TOKENS).

verified accuracy (opt-in)
  --verify sends the tool definitions (names, descriptions, schemas; never your
  config, env values or args) to Anthropic's count_tokens API and adds a table
  of estimated vs actual tokens per server. Needs ANTHROPIC_API_KEY in the
  environment; without it, --verify prints one line and exits 0 without
  sending anything. count_tokens requests are free of charge but rate limited.

privacy
  No network access unless you pass --verify; it is never automatic.
  Env values and args from your config are never printed.
  --probe is opt-in and executes the server commands from your config.
"""


def build_parser():
    p = argparse.ArgumentParser(
        prog="mcp_profile.py", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Measure how many context tokens your MCP servers' tool definitions cost, "
                    "and what you would save by trimming them.\n\n" + HELP_INTRO,
        epilog=HELP_EPILOG)
    p.add_argument("paths", nargs="*", help="MCP config files (default: auto-detect Claude Code, Cursor, Claude Desktop)")
    p.add_argument("--demo", action="store_true", help="use a built-in demo config (no MCP setup needed)")
    p.add_argument("--version", action="version", version="mcp-profile " + __version__)
    g = p.add_argument_group("tool data")
    g.add_argument("--tools", metavar="FILE", help="tool snapshot JSON (server -> tools/list tools)")
    g.add_argument("--probe", action="store_true", help="run stdio servers locally to fetch tools/list (opt-in)")
    g.add_argument("--probe-timeout", type=float, default=20.0, metavar="SECS", help="per-server probe timeout (default 20)")
    g.add_argument("--save-tools", metavar="FILE", help="write the tool lists used to FILE for reuse with --tools")
    g.add_argument("--usage", metavar="FILE", help="usage counts JSON (server -> calls)")
    g.add_argument("--project", metavar="DIR", default=None, help="project path used to pick project-scoped servers in ~/.claude.json (default: cwd)")
    g = p.add_argument_group("accuracy (opt-in, the only feature that uses the network)")
    g.add_argument("--verify", action="store_true",
                   help="check estimates against Anthropic's count_tokens API (needs ANTHROPIC_API_KEY)")
    g.add_argument("--verify-model", default=None, metavar="MODEL",
                   help="model for --verify (default claude-sonnet-5-5)")
    g = p.add_argument_group("output (default: terminal)")
    fmt = g.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="machine-readable JSON")
    fmt.add_argument("--markdown", action="store_true", help="shareable Markdown report")
    g.add_argument("--no-color", action="store_true", help="disable ANSI color")
    g.add_argument("--top", type=int, default=DEFAULTS["top"], metavar="N", help="token hogs to list (default 5)")
    g = p.add_argument_group("estimation and pricing")
    g.add_argument("--chars-per-token", type=float, default=DEFAULTS["chars_per_token"], metavar="F", help="heuristic divisor (default 4)")
    g.add_argument("--price", action="append", default=[], metavar="MODEL=USD", help="USD per million input tokens; repeatable, adds or overrides a model")
    g.add_argument("--prices-file", metavar="FILE", help='JSON {"model": usd_per_million_input_tokens}')
    g.add_argument("--model", default=DEFAULTS["model"], help="model used for savings in USD (default claude-sonnet)")
    g.add_argument("--requests-per-month", type=int, default=DEFAULTS["requests_per_month"], metavar="N",
                   help="model requests per month (default 2200 = 22 days x 4 sessions x 25 requests)")
    g.add_argument("--context-window", type=int, default=DEFAULTS["context_window"], metavar="N", help="context size for %% figures (default 200000)")
    g = p.add_argument_group("optimization simulation")
    g.add_argument("--min-usage", type=float, default=DEFAULTS["min_usage"], metavar="N", help="servers with fewer calls are 'low-use' (default 5)")
    g.add_argument("--description-limit", type=int, default=DEFAULTS["description_limit"], metavar="N", help="max description chars when minifying (default 80)")
    g.add_argument("--defer-fetch-tools", type=int, default=DEFAULTS["defer_fetch_tools"], metavar="N", help="tools fetched per session when deferring (default 5)")
    return p


def build_options(args):
    prices = dict(PRICES)
    if args.prices_file:
        data = load_json(args.prices_file)
        if not isinstance(data, dict):
            raise ConfigError("%s: expected a JSON object of model -> price" % args.prices_file)
        for k, v in data.items():
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                raise ConfigError("%s: price for '%s' must be a non-negative number" % (args.prices_file, k))
            prices[k] = float(v)
    for item in args.price:
        name, sep, val = item.partition("=")
        try:
            if not sep or not name:
                raise ValueError
            prices[name] = float(val)
            if prices[name] < 0:
                raise ValueError
        except ValueError:
            raise ConfigError("--price expects MODEL=USD (non-negative), got '%s'" % item)
    if args.model not in prices:
        raise ConfigError("unknown --model '%s'; known: %s" % (args.model, ", ".join(sorted(prices))))
    for flag, val in (("--chars-per-token", args.chars_per_token), ("--requests-per-month", args.requests_per_month),
                      ("--context-window", args.context_window)):
        if val <= 0:
            raise ConfigError("%s must be greater than 0" % flag)
    for flag, val in (("--top", args.top), ("--description-limit", args.description_limit),
                      ("--defer-fetch-tools", args.defer_fetch_tools), ("--min-usage", args.min_usage)):
        if val < 0:
            raise ConfigError("%s must be 0 or greater" % flag)
    return make_options(top=args.top, chars_per_token=args.chars_per_token, requests_per_month=args.requests_per_month,
                        context_window=args.context_window, model=args.model, min_usage=args.min_usage,
                        description_limit=args.description_limit, defer_fetch_tools=args.defer_fetch_tools,
                        prices=prices)


def add_verification(report, clients, tools_by_server, api_key, model):
    """Attach a count_tokens comparison to each client in `report` (network call)."""
    import mcp_verify
    for cr, servers in zip(report["clients"], clients.values()):
        est = {s["name"]: s["tokens"] for s in cr["servers"] if s["status"] == "measured"}
        work = []
        for srv in servers:
            if srv["name"] not in est:
                continue
            tools = srv["inline_tools"] if srv["inline_tools"] is not None else tools_by_server.get(srv["name"])
            work.append({"name": srv["name"], "tools": tools, "estimated": est[srv["name"]],
                         "chars": sum(len(serialize_tool(t)) for t in tools)})
        if work:
            try:
                cr["verification"] = mcp_verify.verify_client(api_key, model, work)
            except mcp_verify.VerifyError as exc:
                report["warnings"].append("--verify failed for %s: %s" % (cr["client"], exc))


def main(argv=None, home=None, cwd=None):
    args = build_parser().parse_args(argv)
    api_key = ""
    if args.verify:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            import mcp_verify
            sys.stderr.write(mcp_verify.NO_KEY_MESSAGE + "\n")
            return 0
    home = Path(home) if home else Path.home()
    cwd = Path(cwd) if cwd else Path.cwd()
    warnings = []
    try:
        opts = build_options(args)
        if args.demo:
            clients, tools_by_server, usage, warnings = demo_inputs(opts)
        else:
            pairs = [(Path(p), infer_client(p)) for p in args.paths] or discover_configs(home, cwd)
            if not pairs:
                sys.stderr.write("No MCP config found in the usual places. Pass a config path, or run with --demo "
                                 "to see a sample report.\n")
                return 2
            for path, _ in pairs:
                if args.paths and not Path(path).exists():
                    raise ConfigError("%s: file not found" % path)
            clients = load_configs(pairs, args.project or str(cwd), warnings)
            if not clients:
                for w in warnings:
                    sys.stderr.write("error: %s\n" % w)
                sys.stderr.write("No usable MCP servers found. Try --demo to see a sample report.\n")
                return 2
            tools_file = Path(args.tools) if args.tools else find_sidecar(cwd, ["mcp-tools.json", ".mcp-profile/tools.json"])
            tools_by_server = {}
            if tools_file:
                tools_by_server = parse_tools_snapshot(load_json(tools_file), str(tools_file), warnings)
            usage_file = Path(args.usage) if args.usage else find_sidecar(cwd, ["mcp-usage.json", ".mcp-profile/usage.json"])
            usage = parse_usage(load_json(usage_file), str(usage_file), warnings) if usage_file else None
            if args.probe:
                for servers in clients.values():
                    for srv in servers:
                        if srv["disabled"] or srv["inline_tools"] is not None or srv["name"] in tools_by_server:
                            continue
                        try:
                            raw = probe_server(srv, args.probe_timeout)
                            tools_by_server[srv["name"]] = normalize_tools(raw, "probe " + srv["name"], warnings)
                        except ProbeError as exc:
                            warnings.append("probe of '%s' failed: %s" % (srv["name"], exc))
        if args.save_tools:
            Path(args.save_tools).write_text(json.dumps(tools_by_server, indent=2), encoding="utf-8")
        report = build_report(clients, tools_by_server, usage, opts, warnings, demo=args.demo)
        if args.verify:
            import mcp_verify
            add_verification(report, clients, tools_by_server, api_key,
                             args.verify_model or mcp_verify.DEFAULT_MODEL)
    except ConfigError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    except OSError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2

    if args.json:
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
    elif args.markdown:
        sys.stdout.write(render_markdown(report))
    else:
        color = sys.stdout.isatty() and not args.no_color and "NO_COLOR" not in os.environ
        sys.stdout.write(render_text(report, color))
    if (args.json or args.markdown) and warnings:
        for w in warnings:
            sys.stderr.write("warning: %s\n" % w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
