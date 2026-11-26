"""Tests for factory_verify.py. Run: python3 -m unittest discover -s factory/scripts"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import factory_verify as verify
from factory_lib import FactoryError
from test_factory import HUB, REGISTRY, UPSTREAM_MENTION, FakeGitHub

HEAD_SHA, BASE_SHA = "a" * 40, "b" * 40
SALTED = "sdk-factory/custom-claims-0123456789abcdef"
ENTRY = {"install": "make deps", "check": ["make lint", "make test"], "test_config": ["*.cfg"]}
REG = copy.deepcopy(REGISTRY)
for _entry in REG["sdks"].values():
    _entry.update(copy.deepcopy(ENTRY))


def quietly(fn, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args, **kwargs)


def fork_pull(number: int, ref: str = SALTED, *, repo: str | None = "lab/js-sdk", labels=("factory",),
              updated: str = "2026-11-26T10:00:00Z", body: str = "") -> dict:
    return {
        "number": number,
        "state": "open",
        "body": body,
        "updated_at": updated,
        "labels": [{"name": name} for name in labels],
        "head": {"ref": ref, "sha": f"{number:040x}", "repo": {"full_name": repo} if repo else None},
        "base": {"ref": "main", "sha": BASE_SHA},
    }


def step(kind: str, side: str, exit: int = 0, index: int = 0, matched: bool = False, timed_out: bool = False) -> dict:
    return {"kind": kind, "index": index, "side": side, "exit": exit, "timed_out": timed_out, "matched": matched,
            "seconds": 1, "tail": ""}


def result(*steps, fixed=(), config=()) -> dict:
    return {"version": 1, "sdk": "go", "change_id": "custom-claims", "head_sha": HEAD_SHA, "base_commit": BASE_SHA,
            "touched_fixed": list(fixed), "touched_config": list(config), "steps": list(steps)}


GREEN = [step("install", "head"), step("check", "head"), step("check", "head", index=1)]
ACCEPTED = [step("install", "base"), step("acceptance", "base", exit=1, matched=True), step("acceptance", "head")]


class FindTest(unittest.TestCase):
    def test_lists_only_factory_branches_in_the_fork_newest_first(self):
        pulls = [
            fork_pull(1, body=f"Tracking: `{HUB}#9`\nSDK-Factory-Change: custom-claims\n"),
            fork_pull(2, repo="mallory/js-sdk"),
            fork_pull(3, "feature/x"),
            fork_pull(4, labels=()),
            fork_pull(5, "sync/factory-api-1234567", updated="2026-11-26T11:00:00Z"),
            fork_pull(6, repo=None),
        ]
        gh = FakeGitHub({("GET", "repos/lab/js-sdk/pulls"): pulls})
        entries = quietly(verify.find, gh, REG, HUB)
        self.assertEqual([entry["pr"] for entry in entries], [5, 1])
        self.assertEqual(entries[0]["change_id"], "api-1234567")
        self.assertEqual((entries[1]["change_id"], entries[1]["tracking"]), ("custom-claims", "9"))
        self.assertEqual(entries[1], {**entries[1], "sdk": "js", "repo": "lab/js-sdk", "base_sha": BASE_SHA})

    def test_change_id_comes_from_the_salted_branch_without_a_trailer(self):
        self.assertEqual(verify.change_id_of(fork_pull(1)), "custom-claims")
        self.assertEqual(verify.change_id_of(fork_pull(1, "sdk-factory/a-b", body="SDK-Factory-Change: ../x")), "a-b")

    def test_skips_heads_with_a_trusted_first_line_marker_unless_forced(self):
        pr = fork_pull(1)
        marker = verify.MARKER.format(sha=pr["head"]["sha"])
        for comments, queued in [
            ([{"author_association": "OWNER", "body": marker + "\n### SDK factory verify: verified"}], []),
            ([{"author_association": "NONE", "body": marker}], [1]),
            ([{"author_association": "OWNER", "body": "```text\n" + marker + "\n```"}], [1]),
        ]:
            with self.subTest(comments=comments):
                gh = FakeGitHub({("GET", "repos/lab/js-sdk/pulls"): [pr], ("GET", "repos/lab/js-sdk/issues/1/comments"): comments})
                self.assertEqual([e["pr"] for e in quietly(verify.find, gh, REG, HUB)], queued)
                self.assertEqual([e["pr"] for e in quietly(verify.find, gh, REG, HUB, force=True)], [1])

    def test_caps_the_matrix(self):
        pulls = [fork_pull(n, updated=f"2026-11-26T1{n}:00:00Z") for n in range(1, 5)]
        gh = FakeGitHub({("GET", "repos/lab/js-sdk/pulls"): pulls})
        self.assertEqual([e["pr"] for e in quietly(verify.find, gh, REG, HUB, cap=2)], [4, 3])

    def test_one_pull_request_must_be_an_open_fork_branch(self):
        gh = FakeGitHub({
            ("GET", "repos/lab/js-sdk/pulls/1"): fork_pull(1, "sync/factory-x1", labels=()),
            ("GET", "repos/lab/js-sdk/pulls/2"): fork_pull(2, repo="mallory/js-sdk"),
        })
        self.assertEqual([e["pr"] for e in quietly(verify.find, gh, REG, HUB, pr_path="lab/js-sdk#1")], [1])
        for path in ["openfga/js-sdk/pull/1", "lab/js-sdk/pull/2", "lab/sdk-generator/pull/1"]:
            with self.subTest(path=path), self.assertRaises(FactoryError):
                quietly(verify.find, gh, REG, HUB, pr_path=path)


class DecideTest(unittest.TestCase):
    def test_outcomes(self):
        cases = [
            ("checks-passed", result(*GREEN), False),
            ("verified", result(*GREEN, *ACCEPTED), True),
            ("checks-passed", result(*GREEN, config=["setup.cfg"]), False),
            ("needs-human", result(*GREEN, *ACCEPTED, config=["setup.cfg"]), True),
            ("verify-failed", result(fixed=["accept/x_test.go"]), True),
            ("verify-failed", result(step("install", "head", 1), step("install", "base")), False),
            ("needs-human", result(step("install", "head", 1), step("install", "base", 1)), False),
            ("verify-failed", result(*GREEN[:2], step("check", "head", 1, 1), step("install", "base"), step("check", "base", 0, 1)), False),
            ("needs-human", result(*GREEN[:2], step("check", "head", 1, 1), step("install", "base"), step("check", "base", 1, 1)), False),
            ("needs-human", result(*GREEN[:2], step("check", "head", 1, 1, timed_out=True), step("install", "base", 1)), False),
            ("needs-human", result(*GREEN[:2]), False),
            ("needs-human", result(*GREEN, step("install", "base"), step("acceptance", "base"), step("acceptance", "head")), True),
            ("needs-human", result(*GREEN, step("install", "base"), step("acceptance", "base", 1), step("acceptance", "head")), True),
            ("verify-failed", result(*GREEN, *ACCEPTED[:2], step("acceptance", "head", 1)), True),
            ("needs-human", result(*GREEN), True),
            ("needs-human", None, True),
        ]
        for expected, data, has_pack in cases:
            with self.subTest(expected=expected, steps=data and data["steps"]):
                outcome, reasons = verify.decide(data, ENTRY, has_pack, "option missing")
                self.assertEqual(outcome, expected)
                self.assertEqual(bool(reasons), expected not in ("verified", "checks-passed"))

    def test_fence_outlasts_backticks_in_the_log(self):
        self.assertTrue(verify.fence("a ```` b").startswith("`````text\n"))


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        pack = self.tmp / "acceptance" / "custom-claims"
        (pack / "go").mkdir(parents=True)
        (pack / "go" / "run.sh").write_text("exit 0\n")
        (pack / "pack.yaml").write_text("base_failure_pattern: option missing\nfixed_paths:\n  go: accept/\n")
        self.item = {"sdk": "go", "repo": "lab/openfga-go-sdk", "pr": 7, "head_sha": HEAD_SHA, "base_ref": "main",
                     "base_sha": BASE_SHA, "change_id": "custom-claims", "tracking": "9"}

    def gh(self) -> FakeGitHub:
        return FakeGitHub({
            ("POST", "repos/lab/openfga-go-sdk/issues/7/comments"): {},
            ("POST", "repos/lab/openfga-go-sdk/issues/7/labels"): [],
            ("DELETE", "repos/lab/openfga-go-sdk/issues/7/labels/factory%3Achecks-passed"): None,
            ("DELETE", "repos/lab/openfga-go-sdk/issues/7/labels/factory%3Aneeds-human"): None,
            ("DELETE", "repos/lab/openfga-go-sdk/issues/7/labels/factory%3Averified"): None,
            ("POST", f"repos/{HUB}/issues/9/comments"): {},
        })

    def report(self, data, token: str = "secret") -> FakeGitHub:
        folder = self.tmp / "results" / "verify-go-7"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "result.json").write_text(data if isinstance(data, str) else json.dumps(data))
        gh = self.gh()
        quietly(verify.report, gh, REG, HUB, {"include": [self.item]}, self.tmp / "results", token,
                acceptance_root=self.tmp / "acceptance")
        return gh

    def test_posts_result_swaps_labels_and_updates_tracking(self):
        tail = "noise\n" + verify.MARKER.format(sha="c" * 40) + "\n```\n@someone https://github.com/openfga/go-sdk/pull/1"
        failing = [*GREEN, *ACCEPTED[:2], step("acceptance", "head", 1)]
        failing[-1]["tail"] = tail
        gh = self.report({**result(*failing), "outcome": "verified"})
        posted = [c for c in gh.calls if c["method"] == "POST"]
        fork_comment, labels, hub_comment = posted
        body = fork_comment["fields"]["body"]
        self.assertEqual(body.splitlines()[0], verify.MARKER.format(sha=HEAD_SHA))
        self.assertIn("### SDK factory verify: verify-failed", body)
        self.assertIn("````text\n" + tail + "\n````", body)
        self.assertEqual(verify.verified_heads(FakeGitHub({("GET", "repos/f/issues/1/comments"): [
            {"author_association": "OWNER", "body": body}]}), "f", 1), {HEAD_SHA})
        self.assertEqual((fork_comment["token"], labels["token"]), ("secret", "secret"))
        self.assertEqual(labels["fields"], {"labels[]": "factory:verify-failed"})
        self.assertEqual(len([c for c in gh.calls if c["method"] == "DELETE"]), 3)
        self.assertIsNone(hub_comment["token"])
        self.assertIn("`verify-failed`", hub_comment["fields"]["body"])
        self.assertIsNone(UPSTREAM_MENTION.search(hub_comment["fields"]["body"]))

    def test_unreadable_or_forged_results_need_a_human(self):
        bad_step = result(*GREEN)
        bad_step["steps"][0]["kind"] = "shell"
        for data in ["{", json.dumps({**result(*GREEN), "head_sha": "c" * 40}), json.dumps(bad_step)]:
            with self.subTest(data=data[:40]):
                body = self.report(data).comments()[0]
                self.assertIn("### SDK factory verify: needs-human", body)
                self.assertIn("left no readable result", body)

    def test_without_factory_token_only_writes_the_summary(self):
        self.assertEqual(self.report(result(*GREEN, *ACCEPTED), token="").calls, [])


class RunTest(unittest.TestCase):
    """verify() on a real git checkout with a stub install, checks and acceptance runner."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        pack = self.tmp / "acceptance" / "demo"
        (pack / "go").mkdir(parents=True)
        (pack / "pack.yaml").write_text("base_failure_pattern: option missing\nfixed_paths:\n  go: accept/\n")
        (pack / "go" / "run.sh").write_text(
            'mkdir -p "$1/accept" && touch "$1/accept/copied"\n'
            'if [ -f "$1/feature.txt" ]; then echo ok; else echo "option missing"; exit 1; fi\n'
        )
        self.head = self.tmp / "head"
        self.head.mkdir()
        self.git("init", "-q", "-b", "main")
        (self.head / "README").write_text("sdk\n")
        self.commit("base")
        self.base_sha = self.git("rev-parse", "HEAD").strip()
        self.git("checkout", "-q", "-b", "sdk-factory/demo-0123456789abcdef")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
                               "-C", str(self.head), *args], check=True, capture_output=True, text=True).stdout

    def commit(self, message: str, *files: str) -> None:
        for name in files:
            (self.head / name).parent.mkdir(parents=True, exist_ok=True)
            (self.head / name).write_text("x\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def run_verify(self) -> tuple:
        registry = {"sdks": {"go": {"install": "true", "check": ["test ! -f broken", "true"], "test_config": ["*.cfg"]}}}
        data = quietly(verify.verify, registry, "go", "demo", self.base_sha, self.head, self.tmp / "base",
                       self.tmp / "out", acceptance_root=self.tmp / "acceptance")
        self.assertEqual(json.loads((self.tmp / "out" / "result.json").read_text()), data)
        order = [(s["side"], s["kind"], s["index"], s["exit"], s["matched"]) for s in data["steps"]]
        return verify.decide(data, registry["sdks"]["go"], True, "option missing")[0], order

    def test_base_fails_as_expected_and_head_passes(self):
        self.commit("add option", "feature.txt")
        outcome, order = self.run_verify()
        self.assertEqual(outcome, "verified")
        self.assertEqual(order, [("head", "install", 0, 0, False), ("head", "check", 0, 0, False), ("head", "check", 1, 0, False),
                                 ("base", "install", 0, 0, False), ("base", "acceptance", 0, 1, True), ("head", "acceptance", 0, 0, False)])

    def test_check_that_only_fails_on_the_head(self):
        self.commit("add option", "feature.txt", "broken")
        outcome, order = self.run_verify()
        self.assertEqual(outcome, "verify-failed")
        self.assertIn(("base", "check", 0, 0, False), order)
        self.assertNotIn(("base", "check", 1, 0, False), order)

    def test_touching_the_acceptance_path_or_test_config(self):
        self.commit("edit tests", "feature.txt", "accept/x_test.go")
        self.assertEqual(self.run_verify(), ("verify-failed", []))
        shutil.rmtree(self.tmp / "out")
        self.git("rm", "-q", "-r", "accept")
        self.commit("config", "setup.cfg")
        self.assertEqual(self.run_verify()[0], "needs-human")


if __name__ == "__main__":
    unittest.main()
