import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import mcp_profile as mp  # noqa: E402

FIX = HERE / "fixtures"
PROJECT = "/home/dev/app"


def run_cli(argv, **kw):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = mp.main(argv, **kw)
    return code, out.getvalue(), err.getvalue()


class ParsingTests(unittest.TestCase):
    def test_claude_code_user_and_project_scope(self):
        servers = mp.parse_config(FIX / "claude.json", PROJECT)
        names = sorted(s["name"] for s in servers)
        self.assertEqual(names, ["filesystem", "github", "ripgrep-search"])
        self.assertNotIn("unrelated", names)
        gh = [s for s in servers if s["name"] == "github"][0]
        self.assertEqual(gh["command"], "npx")
        self.assertEqual(gh["transport"], "stdio")

    def test_other_project_is_not_loaded(self):
        servers = mp.parse_config(FIX / "claude.json", "/home/dev/other")
        self.assertEqual(sorted(s["name"] for s in servers), ["filesystem", "unrelated"])

    def test_project_mcp_json_and_http_server(self):
        servers = {s["name"]: s for s in mp.parse_config(FIX / "mcp.json")}
        self.assertEqual(servers["remote-docs"]["transport"], "http")
        self.assertEqual(servers["remote-docs"]["url"], "https://example.invalid/mcp")

    def test_settings_json(self):
        servers = mp.parse_config(FIX / "settings.json")
        self.assertEqual([s["name"] for s in servers], ["filesystem"])

    def test_cursor_and_disabled_flag(self):
        servers = {s["name"]: s for s in mp.parse_config(FIX / "cursor_mcp.json")}
        self.assertFalse(servers["filesystem"]["disabled"])
        self.assertTrue(servers["ripgrep-search"]["disabled"])
        self.assertEqual(mp.infer_client(Path("/x/.cursor/mcp.json")), "Cursor")
        self.assertEqual(mp.infer_client(Path("/x/.mcp.json")), "Claude Code")
        self.assertEqual(mp.infer_client(Path("/x/whatever.json")), "Generic")

    def test_generic_format_inline_tools_and_unknown_fields(self):
        servers = {s["name"]: s for s in mp.parse_config(FIX / "generic.json")}
        self.assertEqual(len(servers["filesystem"]["inline_tools"]), 2)
        self.assertIsNone(servers["bare-entry-ignored-field"]["inline_tools"])

    def test_malformed_json_reports_path_and_line(self):
        with self.assertRaises(mp.ConfigError) as ctx:
            mp.parse_config(FIX / "malformed.json")
        msg = str(ctx.exception)
        self.assertIn("malformed.json", msg)
        self.assertIn("line 3", msg)

    def test_other_bad_inputs(self):
        with tempfile.TemporaryDirectory() as d:
            empty = Path(d) / "empty.json"
            empty.write_text("")
            arr = Path(d) / "arr.json"
            arr.write_text("[1, 2]")
            binary = Path(d) / "bin.json"
            binary.write_bytes(b"\xff\xfe\x00\x80")
            for p in (empty, arr, binary, Path(d) / "missing.json"):
                with self.assertRaises(mp.ConfigError, msg=str(p)):
                    mp.parse_config(p)
            odd = Path(d) / "odd.json"
            odd.write_text('{"mcpServers": {"a": 5, "b": {"command": "x"}}}')
            warnings = []
            servers = mp.parse_config(odd, warnings=warnings)
            self.assertEqual([s["name"] for s in servers], ["b"])
            self.assertTrue(any("'a'" in w for w in warnings))

    def test_discover_configs(self):
        with tempfile.TemporaryDirectory() as d:
            home, cwd = Path(d) / "home", Path(d) / "proj"
            (home / ".cursor").mkdir(parents=True)
            cwd.mkdir()
            (home / ".cursor" / "mcp.json").write_text("{}")
            (cwd / ".mcp.json").write_text("{}")
            (home / ".claude.json").write_text("{}")
            found = {(p.name, c) for p, c in mp.discover_configs(home, cwd)}
            self.assertEqual(found, {("mcp.json", "Cursor"), (".mcp.json", "Claude Code"),
                                     (".claude.json", "Claude Code")})

    def test_tool_snapshot_and_usage_formats(self):
        w = []
        a = mp.parse_tools_snapshot({"s": [{"name": "t"}, {"nope": 1}]}, "f", w)
        b = mp.parse_tools_snapshot({"servers": {"s": {"tools": [{"name": "t"}]}}})
        c = mp.parse_tools_snapshot({"s": {"result": {"tools": [{"name": "t"}]}}})
        self.assertEqual(len(a["s"]), 1)
        self.assertEqual(b, c)
        self.assertTrue(w)
        self.assertEqual(mp.parse_usage({"usage": {"a": 3, "b": {"calls": 4}, "c": "x"}}), {"a": 3, "b": 4})


class RealFixtureTests(unittest.TestCase):
    """Gate: a realistic Claude Code config plus MCP-spec-shaped tool lists."""

    def setUp(self):
        self.opts = mp.make_options()
        self.warnings = []
        self.clients = mp.load_configs([(FIX / "claude.json", "Claude Code")], PROJECT, self.warnings)
        self.tools = mp.parse_tools_snapshot(json.loads((FIX / "tools.json").read_text()))
        self.report = mp.build_report(self.clients, self.tools, None, self.opts, self.warnings)

    def test_fixture_matches_spec_shape_and_scale(self):
        for tools in self.tools.values():
            for t in tools:
                self.assertEqual(set(t), {"name", "description", "inputSchema"})
                self.assertEqual(t["inputSchema"]["type"], "object")
        big = self.tools["github"]
        self.assertGreaterEqual(len(big), 10)
        self.assertTrue(all(len(t["description"]) > 150 for t in big))

    def test_report_covers_three_servers(self):
        (cr,) = self.report["clients"]
        self.assertEqual(cr["server_count"], 3)
        self.assertEqual(cr["tool_count"], 20)
        self.assertEqual(cr["unmeasured"], [])
        self.assertEqual(cr["total_tokens"], sum(s["tokens"] for s in cr["servers"]))
        self.assertEqual(cr["top_tools"][0]["server"], "github")
        self.assertAlmostEqual(sum(s["share_pct"] for s in cr["servers"]), 100.0, places=6)

    def test_overlap_detection(self):
        (cr,) = self.report["clients"]
        pairs = {(o["a"]["tool"], o["kind"]) for o in cr["overlaps"]}
        self.assertIn(("search_files", "same name"), pairs)
        self.assertIn(("read_file", "same name"), pairs)

    def test_missing_tool_data_is_flagged_not_guessed(self):
        report = mp.build_report(self.clients, {}, None, self.opts, [])
        cr = report["clients"][0]
        self.assertEqual(cr["total_tokens"], 0)
        self.assertEqual(len(cr["unmeasured"]), 3)
        self.assertEqual(cr["optimizations"], [])
        text = mp.render_text(report)
        self.assertIn("No tool data", text)
        self.assertIn("--probe", text)
        self.assertNotIn("token hogs", text)
        self.assertNotIn("$0.00", text)

    def test_env_values_never_in_output(self):
        for text in (json.dumps(self.report), mp.render_markdown(self.report), mp.render_text(self.report)):
            self.assertNotIn("GITHUB_PERSONAL_ACCESS_TOKEN", text)
            self.assertNotIn("<redacted>", text)


class EstimationTests(unittest.TestCase):
    def test_empty_is_zero(self):
        self.assertEqual(mp.estimate_tokens(""), 0)
        self.assertEqual(mp.tool_tokens({}), 0)
        self.assertEqual(mp.tool_tokens({"name": "", "description": ""}), 0)

    def test_known_values(self):
        self.assertEqual(mp.estimate_tokens("abcd"), 1)
        self.assertEqual(mp.estimate_tokens("abcde"), 2)
        self.assertEqual(mp.estimate_tokens("abcdefgh", chars_per_token=2), 4)

    def test_monotonic_in_schema_size(self):
        prev = 0
        tool = {"name": "t", "description": "", "inputSchema": {"type": "object", "properties": {}}}
        for i in range(30):
            tool["inputSchema"]["properties"]["p%d" % i] = {"type": "string", "description": "x" * (i * 7)}
            cur = mp.tool_tokens(tool)
            self.assertGreaterEqual(cur, prev)
            prev = cur
        self.assertGreater(prev, mp.tool_tokens({"name": "t"}))

    def test_longer_description_costs_more(self):
        short = mp.tool_tokens({"name": "t", "description": "a" * 40})
        long_ = mp.tool_tokens({"name": "t", "description": "a" * 400})
        self.assertGreater(long_, short)

    def test_unknown_fields_do_not_count(self):
        base = {"name": "t", "description": "d"}
        self.assertEqual(mp.tool_tokens(base), mp.tool_tokens(dict(base, annotations={"x": "y" * 500})))


class SimulationTests(unittest.TestCase):
    def setUp(self):
        self.tools = mp.parse_tools_snapshot(json.loads((FIX / "tools.json").read_text()))
        self.measured = [{"name": n, "tools": t} for n, t in self.tools.items()]
        self.opts = mp.make_options()

    def rows(self, usage=None, **kw):
        opts = mp.make_options(**kw)
        return {r["key"]: r for r in mp.simulate(self.measured, usage, opts)}

    def test_savings_never_negative_and_bounded(self):
        for limit in (0, 10, 80, 10000):
            for min_usage in (0, 5, 1000):
                for fetch in (0, 5, 500):
                    rows = self.rows(mp.DEMO_USAGE, description_limit=limit, min_usage=min_usage,
                                     defer_fetch_tools=fetch)
                    for r in rows.values():
                        self.assertGreaterEqual(r["saved"], 0)
                        self.assertLessEqual(r["after"], r["before"])
                        self.assertGreaterEqual(r["after"], 0)

    def test_combined_not_more_than_sum_of_parts(self):
        for limit in (0, 20, 80, 400):
            rows = self.rows(mp.DEMO_USAGE, description_limit=limit)
            parts = rows["defer"]["saved"] + rows["disable"]["saved"] + rows["minify"]["saved"]
            self.assertLessEqual(rows["combined"]["saved"], parts)
            self.assertGreaterEqual(rows["combined"]["saved"], max(r["saved"] for k, r in rows.items() if k != "combined") - 0)

    def test_overlap_is_not_double_counted(self):
        rows = self.rows(mp.DEMO_USAGE)
        self.assertLess(rows["combined"]["saved"],
                        rows["defer"]["saved"] + rows["disable"]["saved"] + rows["minify"]["saved"])

    def test_disable_math(self):
        rows = self.rows(mp.DEMO_USAGE)
        rg = sum(mp.tool_tokens(t) for t in self.tools["ripgrep-search"])
        self.assertEqual(rows["disable"]["saved"], rg)
        self.assertEqual(rows["disable"]["before"], rows["minify"]["before"])

    def test_disable_skipped_without_usage(self):
        rows = self.rows(None)
        self.assertFalse(rows["disable"]["applicable"])
        self.assertEqual(rows["disable"]["saved"], 0)

    def test_minify_monotonic_and_idempotent(self):
        saved = [self.rows(description_limit=n)["minify"]["saved"] for n in (400, 200, 80, 20, 0)]
        self.assertEqual(saved, sorted(saved))
        t = self.tools["github"][0]
        once = mp.minify_tool(t, 30)
        self.assertEqual(mp.minify_tool(once, 30), once)
        self.assertEqual(once["name"], t["name"])
        self.assertLessEqual(len(once["description"]), 30)

    def test_minify_keeps_property_named_description(self):
        t = {"name": "n", "description": "x" * 50, "inputSchema": {"type": "object", "properties": {
            "description": {"type": "string", "description": "y" * 50}}}}
        out = mp.minify_tool(t, 10)
        self.assertIn("description", out["inputSchema"]["properties"])
        self.assertEqual(len(out["inputSchema"]["properties"]["description"]["description"]), 10)

    def test_dollar_savings_scale_with_price(self):
        cheap = self.rows(mp.DEMO_USAGE, model="gpt-4o-mini")["combined"]["monthly_usd_saved"]
        dear = self.rows(mp.DEMO_USAGE, model="claude-opus")["combined"]["monthly_usd_saved"]
        self.assertGreater(dear, cheap)

    def test_empty_input(self):
        rows = mp.simulate([], None, self.opts)
        self.assertTrue(all(r["saved"] == 0 for r in rows))


class ReportTests(unittest.TestCase):
    def test_demo_text(self):
        code, out, _ = run_cli(["--demo", "--no-color"])
        self.assertEqual(code, 0)
        for needle in ("Per-server breakdown", "token hogs", "overlapping", "monthly", "Optimization simulation",
                       "All fixes combined", "github"):
            self.assertIn(needle, out)
        self.assertNotIn("\033[", out)

    def test_demo_json_valid(self):
        code, out, _ = run_cli(["--demo", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["clients"][0]["server_count"], 3)
        self.assertGreater(data["clients"][0]["total_tokens"], 0)
        self.assertEqual(len(data["clients"][0]["optimizations"]), 4)

    def test_demo_markdown_tables(self):
        code, out, _ = run_cli(["--demo", "--markdown"])
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("# MCP Context-Cost Report"))
        table_blocks = [b for b in out.split("\n\n") if b.startswith("| ")]
        self.assertGreaterEqual(len(table_blocks), 5)
        for block in table_blocks:
            lines = block.splitlines()
            self.assertRegex(lines[1], r"^\|( :?---:? \|)+$")
            widths = {l.count("|") for l in lines}
            self.assertEqual(len(widths), 1, block)

    def test_fixture_files_all_modes(self):
        args = [str(FIX / "claude.json"), "--project", PROJECT, "--tools", str(FIX / "tools.json"),
                "--usage", str(FIX / "usage.json")]
        for mode in ([], ["--json"], ["--markdown"]):
            code, out, err = run_cli(args + mode)
            self.assertEqual(code, 0, err)
            self.assertIn("github", out)
        _, out, _ = run_cli(args + ["--json"])
        opt = {o["key"]: o for o in json.loads(out)["clients"][0]["optimizations"]}
        self.assertTrue(opt["disable"]["applicable"])

    def test_color_only_when_asked_by_tty(self):
        text = mp.render_text(json.loads(run_cli(["--demo", "--json"])[1]), color=True)
        self.assertIn("\033[", text)

    def test_price_override_and_errors(self):
        _, out, _ = run_cli(["--demo", "--json", "--price", "mymodel=7.5", "--model", "mymodel"])
        cost = {c["model"]: c for c in json.loads(out)["clients"][0]["cost"]}
        self.assertEqual(cost["mymodel"]["usd_per_mtok"], 7.5)
        total = json.loads(out)["clients"][0]["total_tokens"]
        self.assertAlmostEqual(cost["mymodel"]["monthly_usd"], total / 1e6 * 7.5 * 2200)
        for bad in (["--price", "oops"], ["--model", "nope"], ["--chars-per-token", "0"], ["--top", "-1"]):
            code, _, err = run_cli(["--demo"] + bad)
            self.assertEqual(code, 2)
            self.assertIn("error:", err)

    def test_prices_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "prices.json"
            p.write_text('{"claude-sonnet": 1.0}')
            _, out, _ = run_cli(["--demo", "--json", "--prices-file", str(p)])
            cost = {c["model"]: c for c in json.loads(out)["clients"][0]["cost"]}
            self.assertEqual(cost["claude-sonnet"]["usd_per_mtok"], 1.0)

    def test_malformed_config_cli(self):
        code, _, err = run_cli([str(FIX / "malformed.json")])
        self.assertEqual(code, 2)
        self.assertIn("malformed.json", err)
        self.assertIn("line 3", err)
        code, _, err = run_cli([str(FIX / "nope.json")])
        self.assertEqual(code, 2)

    def test_no_args_without_any_config(self):
        with tempfile.TemporaryDirectory() as d:
            code, _, err = run_cli([], home=d, cwd=d)
            self.assertEqual(code, 2)
            self.assertIn("--demo", err)

    def test_no_args_autodiscovers(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / ".claude.json").write_text((FIX / "claude.json").read_text())
            (home / "mcp-tools.json").write_text((FIX / "tools.json").read_text())
            code, out, err = run_cli(["--project", PROJECT, "--no-color"], home=home, cwd=home)
            self.assertEqual(code, 0, err)
            self.assertIn("Claude Code", out)
            self.assertIn("github", out)

    def test_save_tools_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "saved.json"
            code, _, _ = run_cli(["--demo", "--save-tools", str(dest)])
            self.assertEqual(code, 0)
            self.assertEqual(set(json.loads(dest.read_text())), {"filesystem", "github", "ripgrep-search"})

    def test_help_mentions_essentials(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            mp.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        for needle in ("--demo", "--json", "--markdown", "--probe", "chars", "not billing truth", "mcp-usage.json"):
            self.assertIn(needle, out.getvalue())


class ProbeTests(unittest.TestCase):
    def test_probe_fake_server_with_pagination(self):
        srv = {"name": "fake", "transport": "stdio", "command": sys.executable,
               "args": [str(HERE / "fake_mcp_server.py")], "env": {}}
        tools = mp.probe_server(srv, timeout=15)
        self.assertEqual([t["name"] for t in tools], ["echo", "ping"])

    def test_probe_failures_are_errors_not_crashes(self):
        with self.assertRaises(mp.ProbeError):
            mp.probe_server({"name": "x", "transport": "stdio", "command": "definitely-not-a-real-binary-xyz", "args": []})
        with self.assertRaises(mp.ProbeError):
            mp.probe_server({"name": "x", "transport": "http", "command": None, "url": "https://example.invalid"})
        with self.assertRaises(mp.ProbeError):
            mp.probe_server({"name": "x", "transport": "stdio", "command": sys.executable,
                             "args": ["-c", "pass"]}, timeout=5)

    def test_probe_via_cli(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "probe.json"
            cfg.write_text(json.dumps({"mcpServers": {"fake": {"command": sys.executable,
                           "args": [str(HERE / "fake_mcp_server.py")]}}}))
            code, out, err = run_cli([str(cfg), "--probe", "--json"], cwd=d)
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["clients"][0]["tool_count"], 2)


class NoNetworkTests(unittest.TestCase):
    def test_source_imports_no_network_modules(self):
        src = (HERE.parent / "mcp_profile.py").read_text()
        for banned in ("import socket", "urllib", "http.client", "import requests", "import ssl", "ftplib", "smtplib"):
            self.assertNotIn(banned, src)

    def test_network_code_is_isolated_in_verify_module(self):
        src = (HERE.parent / "mcp_verify.py").read_text()
        self.assertIn("urllib.request", src)
        for banned in ("import socket", "http.client", "import requests", "ftplib", "smtplib"):
            self.assertNotIn(banned, src)


class _FakeResponse:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class VerifyTests(unittest.TestCase):
    """--verify with urllib mocked: no test here touches the network."""

    def setUp(self):
        import os
        import urllib.request
        import mcp_verify
        self.mv, self.os, self.urlreq = mcp_verify, os, urllib.request
        self.calls = []
        self._orig_urlopen = urllib.request.urlopen
        self._orig_key = os.environ.get("ANTHROPIC_API_KEY")
        urllib.request.urlopen = self.fake_urlopen
        self.addCleanup(self.restore)
        self.fail_server = None

    def restore(self):
        self.urlreq.urlopen = self._orig_urlopen
        if self._orig_key is None:
            self.os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            self.os.environ["ANTHROPIC_API_KEY"] = self._orig_key

    def fake_urlopen(self, req, timeout=None):
        body = json.loads(req.data.decode("utf-8"))
        self.calls.append((req, body))
        names = [t["name"] for t in body["tools"]]
        if names == ["stub"]:
            return _FakeResponse({"input_tokens": 100})
        if self.fail_server and self.fail_server in names:
            import urllib.error
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(
                b'{"error":{"message":"tools.0.name: invalid"}}'))
        return _FakeResponse({"input_tokens": 100 + 10 * len(names)})

    def test_missing_key_prints_one_line_exits_0_and_sends_nothing(self):
        self.os.environ.pop("ANTHROPIC_API_KEY", None)
        code, out, err = run_cli(["--demo", "--verify"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertIn("ANTHROPIC_API_KEY", err)
        self.assertEqual(self.calls, [])

    def test_no_network_without_verify_flag_even_with_key(self):
        self.os.environ["ANTHROPIC_API_KEY"] = "sk-test-not-real"
        code, out, err = run_cli(["--demo", "--json"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.calls, [])
        self.assertNotIn("verification", json.loads(out)["clients"][0])

    def test_verify_table_and_request_shape(self):
        self.os.environ["ANTHROPIC_API_KEY"] = "sk-test-not-real"
        code, out, err = run_cli(["--demo", "--verify", "--no-color"])
        self.assertEqual(code, 0, err)
        self.assertIn("Verification (estimated vs actual tokens)", out)
        self.assertIn("Estimated", out)
        self.assertIn("Delta", out)
        # demo: 12 + 6 + 2 tools -> actual 120, 60, 20 after baseline subtraction
        for server, actual in (("github", "120"), ("filesystem", "60"), ("ripgrep-search", "20")):
            line = [ln for ln in out.splitlines() if ln.startswith(server) and "%" in ln][-1]
            self.assertIn(actual, line.split())
        # 1 baseline + 3 servers
        self.assertEqual(len(self.calls), 4)
        req, body = self.calls[1]
        self.assertEqual(req.full_url, "https://api.anthropic.com/v1/messages/count_tokens")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("X-api-key"), "sk-test-not-real")
        self.assertEqual(req.get_header("Anthropic-version"), "2023-06-01")
        self.assertEqual(body["model"], "claude-sonnet-5-5")
        self.assertEqual(sorted(body["tools"][0]), ["description", "input_schema", "name"])
        self.assertNotIn("sk-test-not-real", out + err)

    def test_verify_json_has_delta_math(self):
        self.os.environ["ANTHROPIC_API_KEY"] = "sk-test-not-real"
        code, out, err = run_cli(["--demo", "--verify", "--verify-model", "claude-haiku-5-5", "--json"])
        self.assertEqual(code, 0, err)
        ver = json.loads(out)["clients"][0]["verification"]
        self.assertEqual(ver["model"], "claude-haiku-5-5")
        gh = [r for r in ver["rows"] if r["server"] == "github"][0]
        self.assertEqual(gh["actual"], 120)
        self.assertAlmostEqual(gh["delta_pct"], 100.0 * (gh["estimated"] - 120) / 120)
        self.assertEqual(ver["total_actual"], 200)
        self.assertGreater(ver["suggested_chars_per_token"], 0)

    def test_verify_markdown_renders_table(self):
        self.os.environ["ANTHROPIC_API_KEY"] = "sk-test-not-real"
        code, out, err = run_cli(["--demo", "--verify", "--markdown"])
        self.assertEqual(code, 0, err)
        self.assertIn("| Server | Estimated | Actual | Delta |", out)

    def test_per_server_api_error_is_reported_not_fatal(self):
        self.os.environ["ANTHROPIC_API_KEY"] = "sk-test-not-real"
        self.fail_server = "read_file"
        code, out, err = run_cli(["--demo", "--verify", "--json"])
        self.assertEqual(code, 0, err)
        rows = {r["server"]: r for r in json.loads(out)["clients"][0]["verification"]["rows"]}
        self.assertIn("HTTP 400", rows["filesystem"]["error"])
        self.assertIsNone(rows["filesystem"]["actual"])
        self.assertEqual(rows["github"]["actual"], 120)

    def test_baseline_failure_becomes_warning(self):
        self.os.environ["ANTHROPIC_API_KEY"] = "sk-test-not-real"

        def boom(req, timeout=None):
            import urllib.error
            raise urllib.error.URLError("offline")
        self.urlreq.urlopen = boom
        code, out, err = run_cli(["--demo", "--verify", "--json"])
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertNotIn("verification", report["clients"][0])
        self.assertTrue(any("--verify failed" in w for w in report["warnings"]))


if __name__ == "__main__":
    unittest.main()
