"""Tests for factory_trace.py. Run: python3 -m unittest discover -s factory/scripts"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import factory_trace as trace
from factory_lib import ApiError, FactoryError
from test_factory import HUB, REGISTRY, UPSTREAM_MENTION, FakeGitHub
from test_verify import fork_pull, quietly

RUN = "77"
RUN_URL = f"{trace.SERVER}/{HUB}/actions/runs/{RUN}"
USAGE = {
    "ai_credits": 1.257613,
    "primary_model": "claude-haiku-5.5",
    "input_tokens_inclusive": 363063,
    "cache_read_tokens": 311851,
    "output_tokens": 7182,
}
REVIEW = {
    "result": "pass",
    "findings": [
        {"name": "rationale", "status": "present", "confidence": "high", "detail": "1 prompt captured."},
        {"name": "scope", "status": "partial", "confidence": "medium", "detail": "x | y `z`\n" + "w" * 300},
        {"name": "rationale", "status": "missing", "confidence": "high", "detail": "a second rationale"},
        {"name": "vibes", "status": "present", "confidence": "high", "detail": "not a finding we show"},
        {"name": "validation", "status": "great", "confidence": "high", "detail": "not a status we know"},
    ],
}
LINT = {"errors": 0, "warnings": 3, "infos": 1,
        "findings": [{"rule": "reasoning-spiral"}, {"rule": "reasoning-spiral"}, {"rule": "Not A Rule"}]}
# Each agent-strace command and its stdout, stderr and exit code.
STRACE = {
    "--version": ["agent-strace 0.125.0\n", "", 0],
    "import": ["", "Imported session sess-1\n", 0],
    "review json": [json.dumps(REVIEW), "", 0],
    "review markdown": ["## Review\n", "", 0],
    "health json": [json.dumps({"status": "healthy"}), "", 0],
    "lint json": [json.dumps(LINT), "", 1],
    "share": ["", "", 0],
}
FAKE_STRACE = """
import json, os, sys
from pathlib import Path

here = Path(sys.argv[0]).parent
args = sys.argv[1:]
with (here / "calls.jsonl").open("a") as log:
    log.write(json.dumps({"args": args, "cwd": os.getcwd(), "env": sorted(os.environ)}) + "\\n")
if args[:1] == ["--trace-dir"]:
    args = args[2:]
key = args[0] + (" " + args[args.index("--format") + 1] if "--format" in args else "")
if key == "share":
    Path(args[args.index("-o") + 1]).write_text("<html></html>")
out, err, status = json.loads((here / "replies.json").read_text()).get(key, ["", "", 2])
sys.stdout.write(out)
sys.stderr.write(err)
sys.exit(status)
"""


def pull_record(kind: str = "safe_output.result", phase: str = "safe_outputs", **data) -> dict:
    return {"type": kind, "provenance": {"component": "safe_output", "phase": phase},
            "data": {"type": "create_pull_request", "status": "success", **data}}


def tool(success: bool = True, phase: str | None = "agent") -> dict:
    record = {"type": "tool.execution_complete", "data": {"success": success}}
    if phase:
        record["provenance"] = {"component": "agent", "phase": phase}
    return record


SESSION = [
    pull_record(url=f"{trace.SERVER}/Lab/JS-SDK/pull/5/", repo="lab/js-sdk", number=5),
    pull_record("safe_output.execution", url=f"{trace.SERVER}/lab/js-sdk/pull/5", staged=False),
    pull_record("safe_output.outcome", repo="lab/openfga-go-sdk", number=6),
    pull_record(url=f"{trace.SERVER}/lab/elsewhere/pull/1"),
    # The agent writes its own session log, so only the safe outputs job counts.
    pull_record(phase="agent", url=f"{trace.SERVER}/lab/java-sdk/pull/9"),
    pull_record(status="error", url=f"{trace.SERVER}/lab/dotnet-sdk/pull/10"),
    pull_record(staged=True, url=f"{trace.SERVER}/lab/dotnet-sdk/pull/11"),
    tool(), tool(success=False), tool(phase=None), tool(phase="detection"),
]


def provenance(url: str = RUN_URL) -> str:
    return f"<!-- gh-aw-agentic-workflow: SDK factory port worker, engine: copilot, id: {RUN}, run: {url} -->"


def worker_trace(**changes) -> dict:
    data = trace.blank(RUN, HUB)
    data.update(
        worker=True, found=True, agent_strace="0.125.0", session_id="sess-1", model="claude-haiku-5.5",
        ai_credits=1.2576, tokens_in=363063, tokens_cached=311851, tokens_out=7182, tools=11, failed_tools=1,
        result="pass", health="healthy", lint={"errors": 0, "warnings": 2, "infos": 0, "rules": ["reasoning-spiral"]},
        findings=[{"name": "scope", "status": "partial", "confidence": "medium",
                   "detail": "x | y @someone https://github.com/openfga/go-sdk/pull/1"}],
        prs=[{"sdk": "js", "repo": "lab/js-sdk", "number": 5}],
    )
    data.update(changes)
    return data


class ReadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.strace = self.bin / "agent-strace"
        self.strace.write_text(f"#!{sys.executable}\n{FAKE_STRACE}")
        self.strace.chmod(0o755)
        self.reply(STRACE)
        self.usage = self.tmp / "usage"
        self.usage.mkdir()
        lines = [json.dumps(record) for record in SESSION] + ["not json", "[]", '{"type": "tool.execution_complete"}']
        (self.usage / trace.SESSION_FILE).write_text("\n".join(lines) + "\n")
        (self.usage / trace.USAGE_FILE).write_text(json.dumps(USAGE))
        self.out = self.tmp / "out"
        self.outputs = self.tmp / "outputs"

    def reply(self, replies: dict) -> None:
        (self.bin / "replies.json").write_text(json.dumps(replies))

    def calls(self) -> list:
        path = self.bin / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def read(self, run=None, run_repo: str = HUB, binary: Path | None = None) -> dict:
        gh = FakeGitHub({
            ("GET", f"repos/{HUB}/actions/runs/{RUN}"): run or {"path": trace.WORKER_PATH, "event": "workflow_dispatch"},
        })
        env = {"GITHUB_OUTPUT": str(self.outputs), "GH_TOKEN": "hub-token", "FACTORY_TOKEN": "fork-token"}
        with mock.patch.dict(os.environ, env):
            os.environ.pop("GITHUB_STEP_SUMMARY", None)
            status = quietly(trace.read, gh, REGISTRY, HUB, self.usage, self.out, RUN, run_repo, str(binary or self.strace))
        self.assertEqual(status, 0)
        return json.loads((self.out / trace.TRACE_FILE).read_text())

    def count(self) -> str:
        return self.outputs.read_text().splitlines()[-1]

    def test_reads_a_worker_run_without_tokens(self):
        data = self.read()
        self.assertEqual(self.count(), "count=2")
        self.assertEqual(data["prs"], [{"sdk": "js", "repo": "lab/js-sdk", "number": 5},
                                       {"sdk": "go", "repo": "lab/openfga-go-sdk", "number": 6}])
        self.assertEqual((data["other_prs"], data["tools"], data["failed_tools"]), (1, 3, 1))
        self.assertEqual((data["model"], data["ai_credits"], data["tokens_in"], data["tokens_cached"], data["tokens_out"]),
                         ("claude-haiku-5.5", 1.2576, 363063, 311851, 7182))
        self.assertEqual((data["agent_strace"], data["session_id"], data["result"], data["health"]),
                         ("0.125.0", "sess-1", "pass", "healthy"))
        self.assertEqual([(item["name"], item["status"]) for item in data["findings"]],
                         [("rationale", "present"), ("scope", "partial")])
        self.assertEqual(len(data["findings"][1]["detail"]), trace.DETAIL_CHARS)
        self.assertEqual(data["lint"], {"errors": 0, "warnings": 3, "infos": 1, "rules": ["reasoning-spiral"]})
        self.assertEqual(data["notes"], [])
        for name in ("review.json", "review.md", "health.json", "lint.json", "evidence.html"):
            self.assertTrue((self.out / name).is_file(), name)

        calls = self.calls()
        self.assertEqual(calls[0]["args"], ["--version"])
        self.assertTrue(all(call["args"][:1] == ["--trace-dir"] for call in calls[1:]))
        self.assertFalse({call["cwd"] for call in calls} & {os.getcwd(), str(self.out)})
        self.assertFalse([key for call in calls for key in call["env"] if "TOKEN" in key])
        self.assertEqual(trace.load_trace(self.out / trace.TRACE_FILE, REGISTRY, HUB, RUN), data)

    def test_only_the_port_worker_posts(self):
        cases = {
            "another workflow": ({"path": ".github/workflows/other.lock.yml", "event": "workflow_dispatch"}, HUB),
            "another event": ({"path": trace.WORKER_PATH, "event": "push"}, HUB),
            "an unknown run": (ApiError("runs", "HTTP 404: Not Found"), HUB),
            "another repository": (None, "lab/elsewhere"),
        }
        for name, (run, run_repo) in cases.items():
            with self.subTest(name):
                data = self.read(run, run_repo)
                self.assertEqual((data["worker"], len(data["prs"])), (False, 2))
                self.assertEqual(self.count(), "count=0")
                with self.assertRaises(FactoryError):
                    trace.load_trace(self.out / trace.TRACE_FILE, REGISTRY, HUB, RUN)

    def test_missing_session_log(self):
        (self.usage / trace.SESSION_FILE).unlink()
        data = self.read()
        self.assertEqual((data["found"], data["notes"]), (False, ["no-session"]))
        self.assertEqual(self.count(), "count=0")
        self.assertEqual(self.calls(), [])

    def test_agent_strace_failures_keep_gh_aw_numbers(self):
        cases = {
            "not installed": (STRACE, self.bin / "missing", ["not-installed"]),
            "import fails": ({**STRACE, "import": ["", "error: bad log\n", 1]}, None, ["import-failed"]),
            "unsafe session id": ({**STRACE, "import": ["", "Imported session ../x\n", 0]}, None, ["import-failed"]),
            "review errors": ({**STRACE, "review json": ["{}", "", 2], "health json": ["[]", "", 0]}, None,
                              ["review-failed", "health-failed"]),
        }
        for name, (replies, binary, notes) in cases.items():
            with self.subTest(name):
                self.reply(replies)
                data = self.read(binary=binary)
                self.assertEqual(data["notes"], notes)
                self.assertEqual(self.count(), "count=2")
                self.assertEqual((data["model"], data["ai_credits"]), ("claude-haiku-5.5", 1.2576))
                self.assertEqual(trace.load_trace(self.out / trace.TRACE_FILE, REGISTRY, HUB, RUN), data)


class RenderTest(unittest.TestCase):
    def test_comment_keeps_untrusted_text_in_code(self):
        body = trace.render(worker_trace())
        lines = body.splitlines()
        self.assertEqual(lines[0], trace.MARKER.format(run_id=RUN))
        self.assertIn("| AI credits (gh-aw) | 1.26, about $0.01 |", lines)
        row = next(line for line in lines if line.startswith("| Scope |"))
        self.assertEqual(len(re.split(r"(?<!\\)\|", row)), 6)
        outside = re.sub(r"`[^`\n]*`", "", body)
        self.assertNotIn("@someone", outside)
        self.assertIsNone(UPSTREAM_MENTION.search(outside))
        self.assertEqual((trace.credits_text(0.3), trace.credits_text(400), trace.credits_text(None)),
                         ("0.30, under $0.01", "400.00, about $4.00", "unknown"))


class ReportTest(unittest.TestCase):
    def gh(self, pull, comments=()) -> FakeGitHub:
        return FakeGitHub({
            ("GET", "repos/lab/js-sdk/pulls/5"): pull,
            ("GET", "repos/lab/js-sdk/issues/5/comments"): list(comments),
            ("POST", "repos/lab/js-sdk/issues/5/comments"): {},
            ("PATCH", "repos/lab/js-sdk/issues/comments/41"): {},
        })

    def report(self, gh: FakeGitHub, token: str = "fork-token") -> tuple:
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            status = trace.report(gh, HUB, worker_trace(), token)
        writes = [(call["method"], call["path"], call["token"]) for call in gh.calls if call["method"] != "GET"]
        return status, writes, printed.getvalue()

    def test_posts_once_then_updates_its_own_comment(self):
        pull = fork_pull(5, body="Port the change.\n\n" + provenance())
        marker = trace.MARKER.format(run_id=RUN)
        cases = {
            "first run": ([], ("POST", "repos/lab/js-sdk/issues/5/comments")),
            "rerun": ([{"id": 41, "author_association": "OWNER", "body": marker + "\nold"}],
                      ("PATCH", "repos/lab/js-sdk/issues/comments/41")),
            "copied marker": ([{"id": 41, "author_association": "NONE", "body": marker}],
                              ("POST", "repos/lab/js-sdk/issues/5/comments")),
        }
        for name, (comments, write) in cases.items():
            with self.subTest(name):
                gh = self.gh(pull, comments)
                status, writes, _ = self.report(gh)
                self.assertEqual((status, writes), (0, [(*write, "fork-token")]))
                body = next(call for call in gh.calls if call["method"] != "GET")["fields"]["body"]
                self.assertEqual(body, trace.render(worker_trace()))

    def test_skips_prs_this_run_did_not_open(self):
        cases = {
            "run link without the gh-aw marker": fork_pull(5, body=f"Port the change. {RUN_URL}"),
            "another run": fork_pull(5, body=provenance(RUN_URL + "0")),
            "another repository": fork_pull(5, body=provenance(f"{trace.SERVER}/lab/elsewhere/actions/runs/{RUN}")),
            "closed": {**fork_pull(5, body=provenance()), "state": "closed"},
            "no factory label": fork_pull(5, labels=(), body=provenance()),
            "head in another fork": fork_pull(5, repo="mallory/js-sdk", body=provenance()),
        }
        for name, pull in cases.items():
            with self.subTest(name):
                status, writes, printed = self.report(self.gh(pull))
                self.assertEqual((status, writes), (0, []))
                self.assertIn("skipped, not an open factory PR from this run", printed)

    def test_needs_the_token_and_reports_api_errors(self):
        status, writes, printed = self.report(self.gh(fork_pull(5, body=provenance())), token="")
        self.assertEqual((status, writes), (0, []))
        self.assertIn("`FACTORY_TOKEN` is not set", printed)
        status, writes, printed = self.report(self.gh(ApiError("pulls", "HTTP 502: Bad Gateway")))
        self.assertEqual((status, writes), (1, []))
        self.assertIn("failed (HTTP 502)", printed)


class LoadTraceTest(unittest.TestCase):
    def test_rejects_unexpected_fields(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        path = tmp / trace.TRACE_FILE
        good = worker_trace()
        path.write_text(json.dumps(good))
        self.assertEqual(trace.load_trace(path, REGISTRY, HUB, RUN), good)
        bad = {
            "another run": {"run_id": "78"},
            "another repository": {"run_repo": "lab/elsewhere"},
            "not the worker": {"worker": False},
            "no PRs": {"prs": []},
            "a PR outside the forks": {"prs": [{"sdk": "js", "repo": "mallory/js-sdk", "number": 5}]},
            "an unknown SDK": {"prs": [{"sdk": "ruby", "repo": "lab/ruby-sdk", "number": 5}]},
            "a text PR number": {"prs": [{"sdk": "js", "repo": "lab/js-sdk", "number": "5"}]},
            "a repeated PR": {"prs": [{"sdk": "js", "repo": "lab/js-sdk", "number": 5}] * 2},
            "Markdown in the model": {"model": "x](https://example.com)"},
            "a path as session id": {"session_id": "../x"},
            "a long detail": {"findings": [{"name": "scope", "status": "partial", "confidence": "low", "detail": "x" * 201}]},
            "an unknown finding": {"findings": [{"name": "vibes", "status": "present", "confidence": "high", "detail": ""}]},
            "an unknown lint rule": {"lint": {"errors": 0, "warnings": 1, "infos": 0, "rules": ["<b>"]}},
            "an unknown note": {"notes": ["hello"]},
            "a boolean count": {"tools": True},
            "more failures than calls": {"failed_tools": 12},
            "credits that are not finite": {"ai_credits": float("nan")},
        }
        for name, change in bad.items():
            with self.subTest(name):
                path.write_text(json.dumps({**good, **change}))
                with self.assertRaises(FactoryError):
                    trace.load_trace(path, REGISTRY, HUB, RUN)
        path.write_text(json.dumps(good) + " " * trace.SMALL_BYTES)
        with self.assertRaises(FactoryError):
            trace.load_trace(path, REGISTRY, HUB, RUN)


class MainTest(unittest.TestCase):
    def test_rejects_bad_run_ids_and_repositories(self):
        cases = {
            "text run id": (HUB, ["read", "--run-id", "12a"]),
            "zero run id": (HUB, ["read", "--run-id", "0"]),
            "dots as run repository": (HUB, ["read", "--run-id", RUN, "--run-repo", "../.."]),
            "no repository": ("", ["report", "--run-id", RUN]),
        }
        for name, (hub, argv) in cases.items():
            with self.subTest(name), mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": hub}), \
                    mock.patch.object(trace, "load_registry") as registry:
                self.assertEqual(quietly(trace.main, argv), 1)
                registry.assert_not_called()


if __name__ == "__main__":
    unittest.main()
