"""Opt-in accuracy check for MCP Context-Cost Profiler (the --verify flag).

This is the ONLY module that touches the network, and it is imported only
when --verify is passed. It sends the serialized tool definitions of each
server to Anthropic's count_tokens endpoint (POST /v1/messages/count_tokens)
and compares the real count with the profiler's estimate.

Stdlib only (urllib). The API key is read from ANTHROPIC_API_KEY and is only
ever sent as the x-api-key header to api.anthropic.com; it is never printed.
"""
import json
import urllib.error
import urllib.request

API_URL = "https://api.anthropic.com/v1/messages/count_tokens"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5-5"
ENV_VAR = "ANTHROPIC_API_KEY"
NO_KEY_MESSAGE = ("--verify needs the ANTHROPIC_API_KEY environment variable (not set); "
                  "nothing was sent anywhere. Set it and re-run, or drop --verify.")

# Baseline request: one stub tool, so that the fixed tool-use overhead the API
# adds whenever tools are present is subtracted from every server's count.
_STUB_TOOL = {"name": "stub", "description": "", "input_schema": {"type": "object"}}


class VerifyError(Exception):
    """A count_tokens call failed; the message is safe to print."""


def api_tool(tool):
    """A normalized tool as the API's `tools` parameter expects it."""
    schema = tool.get("inputSchema", tool.get("input_schema"))
    if not isinstance(schema, dict) or not schema:
        schema = {"type": "object"}
    out = {"name": tool["name"], "input_schema": schema}
    if tool.get("description"):
        out["description"] = tool["description"]
    return out


def count_tokens(api_key, model, tools, timeout=30.0):
    """Input-token count of a one-word user message plus `tools`."""
    body = json.dumps({"model": model, "tools": tools,
                       "messages": [{"role": "user", "content": "x"}]}).encode("utf-8")
    req = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "x-api-key": api_key, "anthropic-version": API_VERSION,
        "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", {}).get("message", "")
        except (ValueError, OSError, AttributeError):
            pass
        raise VerifyError("HTTP %s%s" % (exc.code, ": " + detail[:120] if detail else ""))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise VerifyError("request failed: %s" % getattr(exc, "reason", exc))
    count = data.get("input_tokens") if isinstance(data, dict) else None
    if isinstance(count, bool) or not isinstance(count, int):
        raise VerifyError("unexpected response (no input_tokens)")
    return count


def verify_client(api_key, model, servers, timeout=30.0):
    """Compare estimate vs actual per server.

    `servers` is a list of dicts with name, tools (normalized), estimated
    (int tokens) and chars (length of the serialized definitions). Returns a
    dict: model, rows, total_estimated, total_actual, suggested_chars_per_token.
    Per-server failures become rows with an "error" field rather than raising.
    """
    baseline = count_tokens(api_key, model, [_STUB_TOOL], timeout)
    rows = []
    for srv in servers:
        row = {"server": srv["name"], "estimated": srv["estimated"], "actual": None,
               "delta_pct": None, "chars": srv["chars"], "error": None}
        try:
            row["actual"] = max(0, count_tokens(api_key, model, [api_tool(t) for t in srv["tools"]], timeout) - baseline)
            if row["actual"]:
                row["delta_pct"] = 100.0 * (srv["estimated"] - row["actual"]) / row["actual"]
        except VerifyError as exc:
            row["error"] = str(exc)
        rows.append(row)
    ok = [r for r in rows if r["actual"]]
    est = sum(r["estimated"] for r in ok)
    act = sum(r["actual"] for r in ok)
    return {"model": model, "baseline_tokens": baseline, "rows": rows,
            "total_estimated": est, "total_actual": act,
            "total_delta_pct": (100.0 * (est - act) / act) if act else None,
            "suggested_chars_per_token": round(sum(r["chars"] for r in ok) / float(act), 2) if act else None}
