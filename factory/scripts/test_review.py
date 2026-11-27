"""Tests for factory_review.py. Run: python3 -m unittest discover -s factory/scripts"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import factory_review as review
from factory_lib import ROOT, ApiError, FactoryError, load_registry, load_yaml
from factory_route import API_VERSION, PORT_WORKFLOW
from factory_verify import MARKER
from test_factory import HUB, REGISTRY, FakeGitHub
from test_verify import BASE_SHA, SALTED

FORK = "lab/js-sdk"
HEAD, OLD = "c" * 40, "d" * 40
REG = copy.deepcopy(REGISTRY)
REG["sdks"] = {"js": REG["sdks"]["js"]}
REG["review"] = {
    "rounds": 2,
    "max_comments": 8,
    "reviewers": {"claude": "gpt-6.1-sol", "gpt": "claude-opus-5.5"},
    "default": "claude-opus-5.5",
}
BODY = "\n".join([
    "Ports the custom claims change.",
    "",
    f"Tracking: `{HUB}#9`",
    "SDK-Factory-Change: custom-claims",
    "SDK-Factory-Origin: openfga/go-sdk/pull/228",
    "SDK-Factory-Model: claude-opus-5.5",
])
PULL = f"repos/{FORK}/pulls/5"
LIST = f"repos/{FORK}/pulls"
REVIEWS = f"{PULL}/reviews"
FILES = f"{PULL}/files"
COPILOT = f"{PULL}/copilot-code-review"
COMMENTS = f"repos/{FORK}/issues/5/comments"
LABELS = f"repos/{FORK}/issues/5/labels"
HUB_REPO = f"repos/{HUB}"
RUNS = f"repos/{HUB}/actions/workflows/{review.REVIEW_WORKFLOW}/runs"
DISPATCH = f"repos/{HUB}/actions/workflows/{review.REVIEW_WORKFLOW}/dispatches"
PORT_DISPATCH = f"repos/{HUB}/actions/workflows/{PORT_WORKFLOW}/dispatches"
TRACK = f"repos/{HUB}/issues/9/comments"
SOURCE = "repos/openfga/go-sdk/pulls/228"
PATCH = "@@ -1,2 +1,4 @@\n a\n+b\n+c\n d"


def pull(head: str = HEAD, *, number: int = 5, body: str = BODY, ref: str = SALTED, state: str = "open",
         labels=("factory",), updated: str = "2026-11-26T10:00:00Z") -> dict:
    return {
        "number": number,
        "state": state,
        "title": "Add custom claims",
        "body": body,
        "updated_at": updated,
        "labels": [{"name": name} for name in labels],
        "head": {"ref": ref, "sha": head, "repo": {"full_name": FORK}},
        "base": {"ref": "main", "sha": BASE_SHA},
    }


def verified(head: str = HEAD, association: str = "OWNER") -> dict:
    return {"body": MARKER.format(sha=head) + "\n### Factory verify: passed", "author_association": association}


def mark(head: str, round_: int, verdict: str, id_: int = 1, association: str = "OWNER") -> dict:
    body = review.REVIEW_MARKER.format(sha=head, round=round_, verdict=verdict) + f"\n### Round {round_}"
    return {"id": id_, "body": body, "author_association": association}


def finding(severity: str = "major", title: str = "The token request drops the scope", path: str = "src/a.ts",
            line: int = 3, body: str = "Send `scope` with the request.") -> dict:
    return {"severity": severity, "title": title, "path": path, "line": line, "body": body}


def captured(fn, *args, **kwargs) -> tuple:
    """(return value, printed text)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        value = fn(*args, **kwargs)
    return value, out.getvalue()


@contextlib.contextmanager
def run_env(**values):
    """The reviewer run's inputs in the environment, without a job summary file."""
    env = {"SDK": "js", "TARGET_REPO": FORK, "PR": "5", "HEAD_SHA": HEAD, "ROUND": "1", "MODEL": "gpt-6.1-sol"}
    with mock.patch.dict(os.environ, {**env, **values}):
        os.environ.pop("GITHUB_STEP_SUMMARY", None)
        yield


def raw_or(value, text: str):
    """A FakeGitHub answer: the diff for a raw request, else the JSON value."""
    return lambda call: text if call["raw"] else copy.deepcopy(value)


def git(repo, *args: str) -> str:
    command = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", "-C", str(repo)]
    return subprocess.run([*command, *args], check=True, capture_output=True, text=True).stdout.strip()


class ReviewerTest(unittest.TestCase):
    def test_reviewer_comes_from_the_other_family(self):
        cases = {"claude-opus-5.5": "gpt-6.1-sol", "gpt-6.1-sol": "claude-opus-5.5", "gpt-6-luna": "claude-opus-5.5",
                 "": "claude-opus-5.5"}
        for author, reviewer in cases.items():
            with self.subTest(author=author):
                self.assertEqual(review.pick_reviewer(REG, author), (reviewer, ""))

    def test_a_reviewer_outside_the_list_or_in_the_same_family_is_skipped(self):
        reg = copy.deepcopy(REG)
        for reviewers in ({"claude": "gpt-9"}, {"claude": "claude-opus-5.5"}, {}):
            reg["review"]["reviewers"] = reviewers
            with self.subTest(reviewers=reviewers):
                self.assertEqual(review.pick_reviewer(reg, "claude-opus-5.5"), ("gpt-6.1-sol", ""))

    def test_one_family_reviews_itself_with_a_warning(self):
        reg = copy.deepcopy(REG)
        reg["models"]["allowed"] = ["claude-opus-5.5"]
        self.assertEqual(
            review.pick_reviewer(reg, "claude-opus-5.5"),
            ("claude-opus-5.5", "No allowed model comes from another family than `claude-opus-5.5`, so `claude-opus-5.5` reviews it."),
        )
        reg["models"]["allowed"] = []
        self.assertEqual(review.pick_reviewer(reg, "claude-opus-5.5"),
                         ("", "`models.allowed` in `sdks.yaml` has no model to review with."))

    def test_haiku_never_reviews(self):
        reg = copy.deepcopy(REG)
        reg["models"]["allowed"] = ["Haiku", "claude-opus-5.5"]
        reg["review"].update(reviewers={"claude": "Haiku"}, default="haiku")
        self.assertEqual(review.allowed_models(reg), ["claude-opus-5.5"])
        reviewer, warning = review.pick_reviewer(reg, "claude-opus-5.5")
        self.assertEqual(reviewer, "claude-opus-5.5")
        self.assertTrue(warning.endswith("reviews it."), warning)

    def test_settings_clamp_and_ignore_bad_values(self):
        self.assertEqual(review.settings({}), {"rounds": 2, "max_comments": 8, "reviewers": {}, "default": ""})
        self.assertEqual(review.settings({"review": "on"})["rounds"], 2)
        self.assertEqual(review.settings({"review": {"rounds": 0}})["rounds"], 1)
        conf = review.settings({"review": {"rounds": 9, "max_comments": -3, "reviewers": {"Claude": "gpt-6.1-sol"}}})
        self.assertEqual((conf["rounds"], conf["max_comments"], conf["reviewers"]), (5, 0, {"claude": "gpt-6.1-sol"}))
        conf = review.settings({"review": {"rounds": True, "max_comments": "4", "reviewers": ["gpt-6.1-sol"]}})
        self.assertEqual((conf["rounds"], conf["max_comments"], conf["reviewers"]), (2, 8, {}))

    def test_author_model_reads_the_last_trailer(self):
        self.assertEqual(review.author_model(pull()), "claude-opus-5.5")
        self.assertEqual(review.author_model(pull(body=BODY + "\nSDK-Factory-Model: `GPT-6.1-Sol`")), "gpt-6.1-sol")
        for body in ("", None, "SDK-Factory-Model: bad/model", "Model: claude-opus-5.5"):
            with self.subTest(body=body):
                self.assertEqual(review.author_model({"body": body}), "")

    def test_origin_keeps_published_changes_only(self):
        cases = {
            "openfga/go-sdk/pull/228": "openfga/go-sdk/pull/228",
            "openfga/go-sdk/commit/ABCDEF1": "openfga/go-sdk/commit/abcdef1",
            "openfga/go-sdk/issues/7": "openfga/go-sdk/issues/7",
            "openfga/go-sdk#228": "",
            "openfga/go-sdk/tree/main": "",
            "nonsense": "",
        }
        for value, path in cases.items():
            with self.subTest(value=value):
                self.assertEqual(review.origin_of({"body": f"SDK-Factory-Origin: {value}"}), path)
        self.assertEqual(review.origin_of({"body": "No trailer"}), "")

    def test_default_model_follows_the_override(self):
        self.assertEqual(review.default_model(REG), "gpt-6.1-sol")
        self.assertEqual(review.default_model(REG, '{"default_tier": "deep"}'), "claude-opus-5.5")
        self.assertEqual(review.default_model(REG, "not json"), "gpt-6.1-sol")


class FindingsTest(unittest.TestCase):
    ONE = {"severity": "major", "title": "Drops the scope", "path": "src/a.ts", "line": 3, "body": "Send it."}

    def test_reads_every_shape(self):
        text = json.dumps([self.ONE])
        shapes = {
            "array": text,
            "object": json.dumps({"findings": [self.ONE]}),
            "fenced": f"Findings:\n```json\n{text}\n```\nDone.",
            "trailing text": text + "\nThat is all.",
            "list": [self.ONE],
            "dict": {"findings": [self.ONE]},
        }
        for name, value in shapes.items():
            with self.subTest(shape=name):
                self.assertEqual(review.parse_findings(value), ([self.ONE], 0))

    def test_empty_and_invalid(self):
        for value in ("", "   ", None, "[]"):
            with self.subTest(value=value):
                self.assertEqual(review.parse_findings(value), ([], 0))
        for value in ("not json", "42", '{"findings": "none"}', '{"other": []}', "[1, 2"):
            with self.subTest(value=value):
                self.assertEqual(review.parse_findings(value), (None, 0))

    def test_repairs_escaped_template_characters_only(self):
        escaped = r'[{"severity": "nit", "title": "Use \{\{name\}\} and \#1", "body": "a\\{b\nc"}]'
        findings, dropped = review.parse_findings(escaped)
        self.assertEqual(dropped, 0)
        self.assertEqual(findings[0]["title"], "Use {{name}} and #1")
        self.assertEqual(findings[0]["body"], "a\\{b\nc")

    def test_caps_the_findings_and_counts_the_rest(self):
        bad = [{"severity": "critical", "title": "x"}, {"severity": "major", "title": " "}, "text"]
        good = [dict(self.ONE, title=f"Finding {n}") for n in range(25)]
        findings, dropped = review.parse_findings(json.dumps(bad + good))
        self.assertEqual((len(findings), dropped), (review.MAX_FINDINGS, 8))
        self.assertEqual(findings[-1]["title"], "Finding 19")
        self.assertEqual(review.parse_findings("[1, 2]"), ([], 2))

    def test_clean_finding(self):
        raw = {"severity": " MAJOR ", "title": "Two\n  lines", "path": "`./sdk/src/a.ts`", "line": " 12 ", "body": "  Fix.  "}
        self.assertEqual(review.clean_finding(raw),
                         {"severity": "major", "title": "Two lines", "path": "sdk/src/a.ts", "line": 12, "body": "Fix."})
        for line in (-1, 3.5, True, None, "x"):
            with self.subTest(line=line):
                self.assertEqual(review.clean_finding(dict(self.ONE, line=line))["line"], 0)
        self.assertEqual(review.clean_finding(dict(self.ONE, path="././a.py"))["path"], "a.py")
        self.assertEqual(review.clean_finding(dict(self.ONE, path="/abs/b.go"))["path"], "abs/b.go")
        long = review.clean_finding(dict(self.ONE, title="x" * 200, body="y" * 3000))
        self.assertEqual((len(long["title"]), len(long["body"])), (160, 2000))
        for raw in ("text", dict(self.ONE, severity="critical"), dict(self.ONE, title="  ")):
            with self.subTest(raw=raw):
                self.assertIsNone(review.clean_finding(raw))

    def test_decide_keeps_the_verdict_consistent(self):
        major, minor = finding("major"), finding("minor")
        cases = [
            ("ok", [], 0, "ok", []),
            ("OK ", [minor], 0, "ok", []),
            ("ok", [minor, major], 0, "changes",
             ["The reviewer said the PR looks good but listed a blocker or major finding, so the verdict is `changes`."]),
            ("changes", [], 0, "human", ["The reviewer asked for changes without listing any."]),
            ("maybe", [minor], 0, "human", ["The reviewer gave no valid verdict."]),
            ("changes", None, 0, "human", ["The reviewer's findings were not valid JSON, so this review lists none."]),
            ("changes", [major], 1, "changes", ["The review leaves out 1 finding that was incomplete or over the limit of 20."]),
            ("changes", [major], 3, "changes", ["The review leaves out 3 findings that were incomplete or over the limit of 20."]),
        ]
        for verdict, findings, dropped, want, notes in cases:
            with self.subTest(verdict=verdict, findings=findings, dropped=dropped):
                got = review.decide({"verdict": verdict}, copy.deepcopy(findings), dropped)
                self.assertEqual((got[0], got[2]), (want, notes))

    def test_decide_sorts_by_severity(self):
        findings = [finding("nit", "a"), finding("blocker", "b"), finding("minor", "c"), finding("major", "d"), finding("nit", "e")]
        _, ordered, _ = review.decide({"verdict": "changes"}, findings, 0)
        self.assertEqual([f["title"] for f in ordered], ["b", "d", "c", "a", "e"])


def hub_with(pulls=None, *, comments=None, reviews=None, runs=None, extra=None) -> FakeGitHub:
    """The hub and the js fork with one verified factory PR, #5 at HEAD, without reviews."""
    responses = {
        ("GET", LIST): [pull()] if pulls is None else pulls,
        ("GET", PULL): pull(),
        ("GET", COMMENTS): [verified()] if comments is None else comments,
        ("GET", REVIEWS): [] if reviews is None else reviews,
        ("GET", RUNS): {"workflow_runs": runs or []},
        ("GET", HUB_REPO): {"default_branch": "main"},
        ("POST", DISPATCH): {},
    }
    responses.update(extra or {})
    return FakeGitHub(responses)


class TextTest(unittest.TestCase):
    INFO = {"fork": FORK, "head": HEAD, "round": 1, "rounds": 2, "model": "gpt-6.1-sol", "author": "claude-opus-5.5"}

    def test_defang_puts_mentions_and_other_owners_links_in_code(self):
        cases = {
            "Ask @octocat first.": "Ask `@octocat` first.",
            "Ping @openfga/dx now": "Ping `@openfga/dx` now",
            "See openfga/go-sdk#228.": "See `openfga/go-sdk#228`.",
            "Pinned at openfga/js-sdk@abcdef1.": "Pinned at `openfga/js-sdk@abcdef1`.",
            "Read https://github.com/openfga/js-sdk/pull/228 now.": "Read `https://github.com/openfga/js-sdk/pull/228` now.",
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(review.defang(text, "lab"), want)
        for text in ("Our lab/js-sdk#5 and https://github.com/LAB/js-sdk/pull/5 stay.", "Mail a@example.com.",
                     "The path src/foo/bar#1 stays.", "Code `@octocat` and ```\n@team\n``` stay.", ""):
            with self.subTest(text=text):
                self.assertEqual(review.defang(text, "lab"), text)

    def test_right_lines_follow_the_hunks(self):
        patch = "@@ -1,3 +1,4 @@\n a\n-b\n+c\n+d\n e\n@@ -10,2 +20,2 @@\n f\n\\ No newline at end of file\n+g"
        self.assertEqual(review.right_lines("diff --git a/x b/x\n" + patch), {1, 2, 3, 4, 20, 21})
        self.assertEqual(review.right_lines("@@ -0,0 +1 @@\n+only"), {1})
        self.assertEqual(review.right_lines(""), set())

    def test_small_helpers(self):
        self.assertEqual((review.plural(1, "finding"), review.plural(0, "finding")), ("1 finding", "0 findings"))
        self.assertEqual(review.cut("abc", 3, "diff"), "abc")
        self.assertEqual(review.cut("abcd", 3, "diff"), "abc\n\n[The diff is longer than 3 characters. The rest is cut.]\n")
        self.assertEqual(review.close_fences("a\n```py\nx"), "a\n```py\nx\n```")
        self.assertEqual(review.close_fences("a\n```\nx\n```"), "a\n```\nx\n```")
        self.assertEqual(review.where(finding(path="", line=0)), "general")
        self.assertEqual(review.where(finding(line=0)), "`src/a.ts`")
        self.assertEqual(review.where(finding()), "`src/a.ts:3`")
        self.assertEqual(review.run_title(FORK, 5, HEAD, 2), f"Factory review lab/js-sdk/pull/5 at {HEAD} round 2")

    def test_anchor_picks_diff_lines_up_to_the_limit(self):
        findings = [finding(path="sdk/src/a.ts"), finding(path="src/b.ts", line=9), finding(path="", line=0),
                    finding(line=2), finding(line=4)]
        files = {"src/a.ts": {2, 3, 4}, "src/b.ts": {1}}
        self.assertEqual(review.anchor(findings, files, 2), {0, 3})
        self.assertEqual(findings[0]["path"], "src/a.ts")
        self.assertEqual(review.anchor(findings, files, 0), set())
        self.assertEqual(review.anchor(findings, {}, 8), set())

    def test_comment_body_defangs_and_closes_fences(self):
        item = finding("major", "Ask @octocat", body="See openfga/go-sdk#228.\n```py\nx = 1")
        self.assertEqual(review.comment_body(item, 2, "lab", "`src/a.ts:3`"),
                         "**2. major: Ask `@octocat`** (`src/a.ts:3`)\n\nSee `openfga/go-sdk#228`.\n```py\nx = 1\n```")
        self.assertEqual(review.comment_body(finding(body=""), 1, "lab"), "**1. major: The token request drops the scope**")

    def test_render_review(self):
        findings = [finding("major", "Drops the scope", body="Send it."),
                    finding("minor", "Name | pipe", path="", line=0, body="Rename it @octocat."),
                    finding("nit", "Typo", body="")]
        body = review.render_review(self.INFO, "changes", "Summary for @octocat.", findings, {0}, ["A note."],
                                    "Next step.", "https://example.com/run")
        self.assertEqual(body.splitlines()[:2], [review.REVIEW_MARKER.format(sha=HEAD, round=1, verdict="changes"),
                                                 "### SDK factory review, round 1 of 2: changes requested"])
        for part in ("Reviewer `gpt-6.1-sol`, author `claude-opus-5.5`, head `ccccccc`.",
                     "Summary for `@octocat`.",
                     "| 1 | major | Drops the scope | `src/a.ts:3`, inline |",
                     "| 2 | minor | Name \\| pipe | general |",
                     "| 3 | nit | Typo | `src/a.ts:3` |",
                     "**2. minor: Name | pipe** (general)\n\nRename it `@octocat`.",
                     "- A note.\n\n**Next:** Next step."):
            with self.subTest(part=part):
                self.assertIn(part, body)
        self.assertNotIn("**1. major", body)
        self.assertNotIn("**3. nit", body)
        self.assertTrue(body.endswith(review.FOOTER + " [Run](https://example.com/run)\n"))

    def test_render_review_stays_under_the_limit(self):
        big = [finding("minor", f"Finding {n}", body="x" * 1999) for n in range(40)]
        body = review.render_review(dict(self.INFO, author=""), "human", "y" * 9000, big, set(), [], "Next.")
        self.assertLessEqual(len(body), review.REVIEW_LIMIT)
        self.assertIn("author unknown", body)
        self.assertIn("[The summary is longer than 8000 characters. The rest is cut.]", body)
        self.assertRegex(body, r"The details of \d+ more findings did not fit\.")
        self.assertTrue(body.endswith(review.FOOTER + "\n"))


class DispatchTest(unittest.TestCase):
    def row(self, state: str, round_="1", reviewer="`gpt-6.1-sol`", head=HEAD) -> str:
        return f"| js | {review.pull_link(FORK, 5)} | `{head[:7]}` | {round_} | {reviewer} | {state} |"

    def test_states(self):
        title = review.run_title(FORK, 5, HEAD, 1)
        one_family, no_models = copy.deepcopy(REG), copy.deepcopy(REG)
        one_family["models"]["allowed"], no_models["models"]["allowed"] = ["claude-opus-5.5"], []
        cases = [
            ("not verified", dict(comments=[]), {}, self.row("waits for verify", "")),
            ("verified by an outsider", dict(comments=[verified(association="NONE")]), {}, self.row("waits for verify", "")),
            ("verified an older head", dict(comments=[verified(OLD)]), {}, self.row("waits for verify", "")),
            ("unexpected head", dict(pulls=[pull("abc")]), {}, self.row("skipped: unexpected head", "", head="abc")),
            ("reviews unreadable", dict(extra={("GET", REVIEWS): ApiError(REVIEWS, "HTTP 500: boom")}), {},
             self.row("skipped: cannot read the reviews (HTTP 500)", "")),
            ("already reviewed", dict(reviews=[mark(OLD, 1, "changes"), mark(HEAD, 2, "ok", 2)]), {},
             self.row("already reviewed", "2")),
            ("maintainer", dict(reviews=[mark(OLD, 1, "human")]), {}, self.row("waits for a maintainer")),
            ("rounds used", dict(reviews=[mark(OLD, 1, "changes"), mark("e" * 40, 2, "changes", 2)]), {},
             self.row("all 2 rounds used", "2")),
            ("outsider review ignored", dict(reviews=[mark(HEAD, 1, "ok", association="NONE")]), {}, self.row("started")),
            ("running", dict(runs=[{"display_title": title, "status": "queued"}]), {}, self.row("running")),
            ("tried twice", dict(runs=[{"display_title": title, "status": "completed"}] * 2), {},
             self.row("stopped after 2 runs without a review. Start it again with the `pr` input")),
            ("tried once", dict(runs=[{"display_title": title, "status": "completed"}]), {}, self.row("started")),
            ("no reviewer", {}, dict(registry=no_models), self.row("no reviewer model", reviewer="none")),
            ("capped", {}, dict(cap=0), self.row("waits for the next run")),
            ("no secrets", {}, dict(ready=False),
             self.row("not started: set the `FACTORY_TOKEN` and `COPILOT_GITHUB_TOKEN` secrets")),
            ("one family", {}, dict(registry=one_family), self.row(
                "started. No allowed model comes from another family than `claude-opus-5.5`, so `claude-opus-5.5` reviews it.",
                reviewer="`claude-opus-5.5`")),
        ]
        for name, fixture, options, want in cases:
            with self.subTest(name):
                gh, options = hub_with(**fixture), dict(options)
                failed, out = captured(review.dispatch_reviews, gh, options.pop("registry", REG), HUB, **options)
                self.assertEqual(failed, 0)
                self.assertIn(want, out)
                self.assertEqual(len(gh.dispatches()), int(" | started" in want))

    def test_starts_the_next_round_on_the_default_branch(self):
        gh = hub_with(reviews=[mark(OLD, 1, "changes")], extra={("GET", HUB_REPO): {"default_branch": "dev"}})
        self.assertEqual(captured(review.dispatch_reviews, gh, REG, HUB)[0], 0)
        [call] = gh.dispatches()
        self.assertEqual(call["fields"], {"ref": "dev", "inputs[sdk]": "js", "inputs[target_repo]": FORK, "inputs[pr]": "5",
                                          "inputs[head_sha]": HEAD, "inputs[round]": "2", "inputs[model]": "gpt-6.1-sol"})
        self.assertEqual(call["headers"], {"X-GitHub-Api-Version": API_VERSION})

    def test_newest_first_and_capped(self):
        heads = {n: str(n) * 40 for n in (5, 6, 7)}
        responses = {("GET", LIST): [pull(heads[n], number=n, updated=f"2026-11-2{n}T00:00:00Z") for n in heads],
                     ("GET", RUNS): {"workflow_runs": []}, ("GET", HUB_REPO): {"default_branch": "main"},
                     ("POST", DISPATCH): {}}
        for n, head in heads.items():
            responses[("GET", f"repos/{FORK}/issues/{n}/comments")] = [verified(head)]
            responses[("GET", f"repos/{FORK}/pulls/{n}/reviews")] = []
        gh = FakeGitHub(responses)
        failed, out = captured(review.dispatch_reviews, gh, REG, HUB, cap=2)
        self.assertEqual(failed, 0)
        self.assertEqual([call["fields"]["inputs[pr]"] for call in gh.dispatches()], ["7", "6"])
        self.assertIn(self.row("waits for the next run", head=heads[5]), out)
        self.assertEqual(sum(call["path"] == HUB_REPO for call in gh.calls), 1)

    def test_one_pull_ignores_the_try_limit_but_needs_a_factory_pull(self):
        tried = [{"display_title": review.run_title(FORK, 5, HEAD, 1), "status": "completed"}] * 2
        gh = hub_with(runs=tried)
        self.assertEqual(captured(review.dispatch_reviews, gh, REG, HUB, pr_path="lab/js-sdk/pull/5")[0], 0)
        self.assertEqual(len(gh.dispatches()), 1)
        self.assertFalse(any("state=open" in call["path"] for call in gh.calls))
        gh = hub_with(extra={("GET", PULL): pull(labels=())})
        _, out = captured(review.dispatch_reviews, gh, REG, HUB, pr_path="lab/js-sdk/pull/5")
        self.assertIn(self.row("skipped: not a factory pull request", ""), out)
        self.assertEqual(gh.dispatches(), [])

    def test_failures(self):
        gh = hub_with(extra={("POST", DISPATCH): ApiError(DISPATCH, "HTTP 500: boom")})
        failed, out = captured(review.dispatch_reviews, gh, REG, HUB)
        self.assertEqual(failed, 1)
        self.assertIn(self.row("start failed (HTTP 500)"), out)
        gh = hub_with(extra={("GET", RUNS): ApiError(RUNS, "HTTP 404: Not Found")})
        self.assertEqual(captured(review.dispatch_reviews, gh, REG, HUB)[0], 0)
        self.assertEqual(len(gh.dispatches()), 1)
        gh = hub_with(extra={("GET", RUNS): ApiError(RUNS, "HTTP 500: boom")})
        with self.assertRaises(ApiError):
            captured(review.dispatch_reviews, gh, REG, HUB)

    def test_no_targets(self):
        gh = hub_with(pulls=[pull(labels=()), pull(ref="feature/x")])
        failed, out = captured(review.dispatch_reviews, gh, REG, HUB)
        self.assertEqual((failed, out.strip()), (0, "### Factory reviews\n\nNo open factory pull requests."))
        self.assertFalse(any(call["path"].startswith(RUNS) for call in gh.calls))

    def test_main_starts_reviews_only_with_both_secrets(self):
        for factory, copilot, started in (("true", "true", 1), ("true", "", 0), ("", "true", 0)):
            with self.subTest(factory=factory, copilot=copilot):
                gh = hub_with()
                with run_env(GITHUB_REPOSITORY=HUB, HAS_FACTORY_TOKEN=factory, HAS_COPILOT_TOKEN=copilot), \
                        mock.patch.object(review, "GitHub", return_value=gh), \
                        mock.patch.object(review, "load_registry", return_value=REG):
                    status, _ = captured(review.main, ["dispatch"])
                self.assertEqual((status, len(gh.dispatches())), (0, started))


class PostTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def write(self, data) -> str:
        path = self.tmp / "agent.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)

    def output(self, verdict: str = "changes", text: str = "The port misses the scope.", findings=None) -> str:
        findings = [finding()] if findings is None else findings
        return self.write({"items": [{"type": "post_review", "verdict": verdict, "summary": text,
                                      "findings": json.dumps(findings)}]})

    def hub(self, pr=None, reviews=None, extra=None) -> FakeGitHub:
        responses = {
            ("GET", PULL): pr or pull(),
            ("GET", REVIEWS): reviews or [],
            ("POST", REVIEWS): {},
            ("GET", FILES): [{"filename": "src/a.ts", "patch": PATCH}],
            ("POST", LABELS): {},
            ("POST", TRACK): None,
            ("GET", HUB_REPO): {"default_branch": "main"},
            ("POST", PORT_DISPATCH): {},
            ("POST", COPILOT): {},
        }
        responses.update(extra or {})
        return FakeGitHub(responses)

    def run_post(self, gh, output: str, token: str = "tok", env=None, **options) -> tuple:
        with run_env(**(env or {})):
            return captured(review.post, gh, REG, HUB, output, token, **options)

    @staticmethod
    def reviews(gh) -> list:
        return [call for call in gh.calls if call["method"] == "POST" and call["path"] == REVIEWS]

    def test_without_a_post_review_call_nothing_is_posted(self):
        gh = self.hub()
        for output in (str(self.tmp / "missing.json"), self.write({"items": [{"type": "noop"}]})):
            status, out = self.run_post(gh, output)
            self.assertEqual(status, 0)
            self.assertIn("The reviewer did not call `post_review`, so nothing was posted.", out)
        self.assertEqual(gh.calls, [])

    def test_a_stale_run_posts_nothing(self):
        cases = [
            ("closed", dict(pr=pull(state="closed")), {},
             "The pull request is closed or is no longer a factory pull request."),
            ("newer head", dict(pr=pull("e" * 40)), {},
             "The pull request has a newer head. Its next verify run starts a review of it."),
            ("reviewed", dict(reviews=[mark(HEAD, 1, "ok")]), {}, "This head already has a factory review."),
            ("skipped round", {}, {"ROUND": "2"}, "Round 2 is not the next review round."),
            ("past the last round", dict(reviews=[mark(OLD, 1, "changes"), mark("e" * 40, 2, "changes", 2)]),
             {"ROUND": "3"}, "Round 3 is not the next review round."),
        ]
        for name, fixture, env, why in cases:
            with self.subTest(name):
                gh = self.hub(**fixture)
                status, out = self.run_post(gh, self.output(), env=env)
                self.assertEqual(status, 0)
                self.assertIn(f"Not posted. {why}", out)
                self.assertEqual([call for call in gh.calls if call["method"] != "GET"], [])

    def test_ok_posts_a_comment_review_a_label_and_a_tracking_line(self):
        gh = self.hub()
        status, out = self.run_post(gh, self.output("ok", "Looks right. Thanks @octocat.", []),
                                    run_url="https://example.com/run")
        self.assertEqual(status, 0)
        [call] = self.reviews(gh)
        body = call["json"]["body"]
        self.assertEqual((call["json"], call["token"]),
                         ({"commit_id": HEAD, "event": "COMMENT", "body": body, "comments": []}, "tok"))
        self.assertTrue(body.startswith(review.REVIEW_MARKER.format(sha=HEAD, round=1, verdict="ok")
                                        + "\n### SDK factory review, round 1 of 2: looks good"))
        self.assertIn("Looks right. Thanks `@octocat`.", body)
        self.assertIn("**Next:** A maintainer reviews, approves and merges.", body)
        label = next(call for call in gh.calls if call["method"] == "POST" and call["path"] == LABELS)
        self.assertEqual((label["fields"], label["token"]), ({"labels[]": "factory:review-ok"}, "tok"))
        track = next(call for call in gh.calls if call["path"] == TRACK)
        self.assertIsNone(track["token"])
        self.assertEqual(gh.comments(), [f"**js** review round 1 of 2: `ok` for {review.pull_link(FORK, 5)} at `ccccccc`. "
                                         "A maintainer reviews, approves and merges."])
        self.assertEqual(gh.dispatches(), [])
        self.assertIn(f"| js | {review.pull_link(FORK, 5)} | 1 of 2 | ok | posted | `factory:review-ok` | none | off |", out)

    def test_changes_comment_inline_and_start_one_revision(self):
        gh = self.hub()
        findings = [finding("minor", "Name the option", line=9, body="Use `customClaims`."), finding()]
        status, out = self.run_post(gh, self.output("changes", "Two problems.", findings))
        self.assertEqual(status, 0)
        [call] = self.reviews(gh)
        self.assertEqual(call["json"]["comments"], [{
            "path": "src/a.ts", "line": 3, "side": "RIGHT",
            "body": "**1. major: The token request drops the scope**\n\nSend `scope` with the request.",
        }])
        for part in ("| 1 | major | The token request drops the scope | `src/a.ts:3`, inline |",
                     "| 2 | minor | Name the option | `src/a.ts:9` |",
                     "**2. minor: Name the option** (`src/a.ts:9`)\n\nUse `customClaims`.",
                     "**Next:** The port worker revises this PR once. Round 2 then reviews the new head."):
            with self.subTest(part=part):
                self.assertIn(part, call["json"]["body"])
        [port] = gh.dispatches()
        self.assertEqual(port["path"], PORT_DISPATCH)
        self.assertEqual(port["fields"], {
            "ref": "main", "inputs[sdk]": "js", "inputs[target_repo]": FORK, "inputs[source]": "openfga/go-sdk/pull/228",
            "inputs[change_id]": "custom-claims", "inputs[base_branch]": "main", "inputs[tracking_issue]": "9",
            "inputs[notes]": "", "inputs[revise_pr]": "5", "inputs[model]": "claude-opus-5.5",
        })
        self.assertIn("| changes | posted | `factory:review-changes` | started with `claude-opus-5.5` | off |", out)

    def test_the_revision_keeps_the_author_model_when_it_is_allowed(self):
        other = BODY.replace("SDK-Factory-Model: claude-opus-5.5", "SDK-Factory-Model: claude-sonnet-9")
        for body, override, model in ((BODY, "", "claude-opus-5.5"), (other, "", "gpt-6.1-sol"),
                                      (other, '{"default_tier": "deep"}', "claude-opus-5.5")):
            with self.subTest(body=body[-20:], override=override):
                gh = self.hub(pr=pull(body=body))
                self.run_post(gh, self.output(), models_override=override)
                self.assertEqual(gh.dispatches()[0]["fields"]["inputs[model]"], model)

    def test_next_steps_without_a_revision(self):
        cases = [
            ("last round", dict(reviews=[mark(OLD, 1, "changes")]), {"ROUND": "2"}, "changes",
             "That was the last of 2 rounds. A maintainer takes over."),
            ("sync", dict(pr=pull(ref="sync/factory-custom-claims", body="Generator sync.")), {}, "changes",
             "The factory does not revise sync PRs. A maintainer fixes the generator change or this PR."),
            ("no origin", dict(pr=pull(body=BODY.replace("SDK-Factory-Origin", "Origin"))), {}, "changes",
             "The PR has no factory origin or change id, so the factory cannot revise it. A maintainer takes over."),
            ("human", {}, {}, "human", "A maintainer takes over. The factory does not review this PR again."),
        ]
        for name, fixture, env, verdict, next_step in cases:
            with self.subTest(name):
                gh = self.hub(**fixture)
                status, _ = self.run_post(gh, self.output(verdict), env=env)
                self.assertEqual(status, 0)
                self.assertIn(f"**Next:** {next_step}", self.reviews(gh)[0]["json"]["body"])
                self.assertEqual(gh.dispatches(), [])
                label = next(call for call in gh.calls if call["path"] == LABELS)
                self.assertEqual(label["fields"], {"labels[]": review.REVIEW_LABELS[verdict]})

    def test_a_rejected_inline_comment_posts_the_review_without_comments(self):
        def answer(call):
            return ApiError(REVIEWS, "HTTP 422: Unprocessable Entity") if call["json"].get("comments") else {}

        gh = self.hub(extra={("POST", REVIEWS): answer})
        status, out = self.run_post(gh, self.output())
        self.assertEqual(status, 0)
        first, second = self.reviews(gh)
        self.assertEqual(len(first["json"]["comments"]), 1)
        self.assertNotIn("comments", second["json"])
        self.assertIn("| 1 | major | The token request drops the scope | `src/a.ts:3` |", second["json"]["body"])
        self.assertIn("**1. major: The token request drops the scope** (`src/a.ts:3`)", second["json"]["body"])
        self.assertIn("| posted without inline comments |", out)

    def test_without_the_token_nothing_is_written(self):
        gh = self.hub()
        status, out = self.run_post(gh, self.output(), token="")
        self.assertEqual(status, 0)
        self.assertEqual([call for call in gh.calls if call["method"] != "GET"], [])
        self.assertIn("| changes | not posted, `FACTORY_TOKEN` is not set | none | none | off |", out)

    def test_failures_return_one(self):
        cases = [
            ("review", (REVIEWS, "POST"), "| posting failed (HTTP 500) | none | none | off |"),
            ("label", (LABELS, "POST"), "| posted | failed (HTTP 500) | started with `claude-opus-5.5` |"),
            ("tracking", (TRACK, "POST"), "| posted, tracking comment failed (HTTP 500) | `factory:review-changes` |"),
            ("revision", (PORT_DISPATCH, "POST"), "| `factory:review-changes` | start failed (HTTP 500) | off |"),
        ]
        for name, (path, method), want in cases:
            with self.subTest(name):
                gh = self.hub(extra={(method, path): ApiError(path, "HTTP 500: boom")})
                status, out = self.run_post(gh, self.output())
                self.assertEqual(status, 1)
                self.assertIn(want, out)

    def test_copilot_review_on_the_first_round_only(self):
        for answer, state in (({}, "requested"), (ApiError(COPILOT, "HTTP 409: Conflict"), "already pending"),
                              (ApiError(COPILOT, "HTTP 403: Forbidden"), "not available (HTTP 403)")):
            with self.subTest(state):
                gh = self.hub(extra={("POST", COPILOT): answer})
                status, out = self.run_post(gh, self.output("ok", findings=[]), copilot=True)
                self.assertEqual(status, 0)
                self.assertIn(f"| `factory:review-ok` | none | {state} |", out)
                [call] = [call for call in gh.calls if call["path"] == COPILOT]
                self.assertEqual((call["json"], call["headers"], call["token"]),
                                 ({"review_effort": "balanced"}, {"X-GitHub-Api-Version": API_VERSION}, "tok"))
        gh = self.hub(reviews=[mark(OLD, 1, "changes")])
        _, out = self.run_post(gh, self.output("ok", findings=[]), env={"ROUND": "2"}, copilot=True)
        self.assertFalse(any(call["path"] == COPILOT for call in gh.calls))
        self.assertIn("| `factory:review-ok` | none | off |", out)

    def test_main_reads_the_run_from_the_environment(self):
        gh = self.hub()
        env = {"GITHUB_REPOSITORY": HUB, "GH_AW_AGENT_OUTPUT": self.output("ok", findings=[]), "FACTORY_TOKEN": "tok",
               "FACTORY_COPILOT_REVIEW": "on", "GITHUB_RUN_ID": "77", "FACTORY_MODELS": ""}
        with run_env(**env), mock.patch.object(review, "GitHub", return_value=gh), \
                mock.patch.object(review, "load_registry", return_value=REG):
            status, _ = captured(review.main, ["post"])
            self.assertEqual(status, 0)
            with mock.patch.dict(os.environ, {"ROUND": "0"}):
                status, out = captured(review.main, ["post"])
        self.assertEqual((status, out.strip()), (1, "::error::The round must be a number from 1 to 9."))
        body = self.reviews(gh)[0]["json"]["body"]
        self.assertTrue(body.endswith(f"{review.FOOTER} [Run]({review.SERVER}/{HUB}/actions/runs/77)\n"))
        self.assertTrue(any(call["path"] == COPILOT for call in gh.calls))


def make_repo(tmp: Path) -> tuple:
    """A fork with `main` and the port branch, cloned to tmp/sdk. Returns (clone, main SHA, branch SHA)."""
    seed = tmp / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    (seed / "a.ts").write_text("a\nd\n", encoding="utf-8")
    git(seed, "add", "a.ts")
    git(seed, "commit", "-q", "-m", "base")
    base = git(seed, "rev-parse", "HEAD")
    git(seed, "switch", "-q", "-c", SALTED)
    (seed / "a.ts").write_text("a\nb\nc\nd\n", encoding="utf-8")
    git(seed, "commit", "-q", "-am", "Add custom claims")
    head = git(seed, "rev-parse", "HEAD")
    git(tmp, "init", "-q", "--bare", "-b", "main", "origin.git")
    git(seed, "push", "-q", str(tmp / "origin.git"), "main", SALTED)
    git(tmp, "clone", "-q", "origin.git", "sdk")
    return tmp / "sdk", base, head


class PrepareTest(unittest.TestCase):
    ENV = {"SDK": "js", "TARGET_REPO": FORK, "CHANGE_ID": "custom-claims", "BASE_BRANCH": "main", "REVISE_PR": "5"}
    NOTES = [{"path": "src/a.ts", "line": 3, "body": "Send `scope` with the request."}]
    ASK = "The latest factory review does not ask for a revision of the pull request's head."

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.out = self.tmp / "out"

    def hub(self, head: str = HEAD, *, pr=None, reviews=None, notes=None, comments=None) -> FakeGitHub:
        return FakeGitHub({
            ("GET", PULL): pr or pull(head),
            ("GET", REVIEWS): [mark(head, 1, "changes", 7)] if reviews is None else reviews,
            ("GET", f"{REVIEWS}/7/comments"): self.NOTES if notes is None else notes,
            ("GET", COMMENTS): [verified(head)] if comments is None else comments,
        })

    def run_prepare(self, gh, sdk_dir=None, **env) -> str:
        """prepare() with the port worker's inputs. A missing checkout makes any git call fail."""
        with mock.patch.dict(os.environ, {**self.ENV, **env}):
            os.environ.pop("GITHUB_STEP_SUMMARY", None)
            _, out = captured(review.prepare, gh, REG, HUB, self.out, sdk_dir or self.tmp / "missing")
        return out

    def test_only_an_open_port_pr_with_changes_asked_at_its_head_is_revised(self):
        mismatch = "The SDK and the target repository do not match `factory/sdks.yaml`."
        invalid = "The change id, base branch or pull request number is not valid."
        closed = "The pull request is closed or is not a factory pull request."
        cases = [
            ("other sdk", {}, {"SDK": "go"}, mismatch),
            ("other fork", {}, {"TARGET_REPO": "lab/other-sdk"}, mismatch),
            ("change id", {}, {"CHANGE_ID": "Custom_Claims"}, invalid),
            ("base branch", {}, {"BASE_BRANCH": "main;true"}, invalid),
            ("pr number", {}, {"REVISE_PR": "5a"}, invalid),
            ("closed", dict(pr=pull(state="closed")), {}, closed),
            ("no label", dict(pr=pull(labels=())), {}, closed),
            ("sync branch", dict(pr=pull(ref="sync/factory-custom-claims")), {},
             "The pull request's branch `sync/factory-custom-claims` is not the port branch of `custom-claims`."),
            ("other change", dict(pr=pull(ref="sdk-factory/other-change")), {},
             "The pull request's branch `sdk-factory/other-change` is not the port branch of `custom-claims`."),
            ("other base", {}, {"BASE_BRANCH": "dev"}, "The pull request's base is `main`, not `dev`."),
            ("no review", dict(reviews=[]), {}, self.ASK),
            ("approved", dict(reviews=[mark(HEAD, 1, "ok")]), {}, self.ASK),
            ("older head", dict(reviews=[mark(OLD, 1, "changes")]), {}, self.ASK),
            ("last round", dict(reviews=[mark(OLD, 1, "changes"), mark(HEAD, 2, "changes", 2)]), {}, self.ASK),
            ("untrusted", dict(reviews=[mark(HEAD, 1, "changes", association="NONE")]), {}, self.ASK),
        ]
        for name, hub, env, message in cases:
            with self.subTest(name):
                with self.assertRaises(FactoryError) as caught:
                    self.run_prepare(self.hub(**hub), **env)
                self.assertEqual(str(caught.exception), message)
                self.assertFalse(self.out.exists())

    def test_the_revision_starts_on_the_reviewed_head(self):
        sdk, _, head = make_repo(self.tmp)
        out = self.run_prepare(self.hub(head), sdk)
        self.assertEqual((git(sdk, "branch", "--show-current"), git(sdk, "rev-parse", "HEAD")), (SALTED, head))
        expected = {"pr": 5, "head_ref": SALTED, "head_sha": head, "round": 1, "next_round": 2, "rounds": 2}
        self.assertEqual((self.out / "revise.json").read_text(), json.dumps(expected, indent=2) + "\n")
        self.assertEqual((self.out / "review.md").read_text(), "\n".join([
            "# Factory review, round 1 of 2", "", mark(head, 1, "changes")["body"], "", "## Inline comments", "",
            "### `src/a.ts:3`", "", "Send `scope` with the request.", ""]))
        self.assertEqual((self.out / "verify.md").read_text(), verified(head)["body"])
        self.assertIn(f"Revising {review.pull_link(FORK, 5)} on `{SALTED}` at `{head[:7]}` after review round 1 of 2.", out)

    def test_missing_comments_verify_result_and_remote_are_tolerated(self):
        sdk, _, head = make_repo(self.tmp)
        shutil.rmtree(self.tmp / "origin.git")
        gh = self.hub(head, notes=ApiError(f"{REVIEWS}/7/comments", "HTTP 500: boom"), comments=[])
        self.run_prepare(gh, sdk)
        self.assertEqual(git(sdk, "rev-parse", "HEAD"), head)
        self.assertEqual((self.out / "review.md").read_text(),
                         f"# Factory review, round 1 of 2\n\n{mark(head, 1, 'changes')['body']}\n")
        self.assertEqual((self.out / "verify.md").read_text(), "No verify result for this head.\n")
        git(sdk, "update-ref", "-d", f"refs/remotes/origin/{SALTED}")
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_prepare(gh, sdk)

    def test_a_moved_branch_is_not_revised(self):
        sdk, base, _ = make_repo(self.tmp)
        with self.assertRaises(FactoryError) as caught:
            self.run_prepare(self.hub(base), sdk)
        self.assertEqual(str(caught.exception),
                         "The pull request's branch moved after the review. Its next review covers the new head.")
        self.assertFalse(self.out.exists())

    def test_main_reports_problems(self):
        env = {**self.ENV, "GITHUB_REPOSITORY": HUB, "SDK": "go"}
        with mock.patch.dict(os.environ, env), mock.patch.object(review, "GitHub", return_value=self.hub()), \
                mock.patch.object(review, "load_registry", return_value=REG):
            os.environ.pop("GITHUB_STEP_SUMMARY", None)
            status, out = captured(review.main, ["prepare"])
        self.assertEqual((status, out.strip()),
                         (1, "::error::The SDK and the target repository do not match `factory/sdks.yaml`."))


DIFF = f"diff --git a/src/a.ts b/src/a.ts\n--- a/src/a.ts\n+++ b/src/a.ts\n{PATCH}\n"
SOURCE_PR = {"title": "Add custom claims to client credentials", "body": "Adds `customClaims`.", "state": "closed",
             "merged_at": "2026-11-20T09:00:00Z"}
SOURCE_DIFF = "diff --git a/credentials/credentials.go b/credentials/credentials.go\n"


class GatherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.out = self.tmp / "out"

    def hub(self, pr=None, extra=None) -> FakeGitHub:
        responses = {
            ("GET", PULL): raw_or(pr or pull(), DIFF),
            ("GET", FILES): [{"filename": "src/a.ts", "status": "modified", "additions": 2, "deletions": 0, "patch": PATCH}],
            ("GET", SOURCE): raw_or(SOURCE_PR, SOURCE_DIFF),
            ("GET", COMMENTS): [verified()],
            ("GET", REVIEWS): [mark(OLD, 1, "changes", 7)],
            ("GET", f"{REVIEWS}/7/comments"): [{"id": 70, "path": "src/a.ts", "line": 3, "body": "Send `scope`."}],
        }
        responses.update(extra or {})
        return FakeGitHub(responses)

    def run_gather(self, gh, sdk_dir=None, **env) -> str:
        """gather() for round 2. A missing checkout sends the diff request to the API."""
        with run_env(**{"ROUND": "2", **env}):
            _, out = captured(review.gather, gh, REG, HUB, self.out, sdk_dir or self.tmp / "missing")
        return out

    def read(self, name: str):
        text = (self.out / name).read_text(encoding="utf-8")
        return json.loads(text) if name.endswith(".json") else text

    def test_the_context_comes_from_the_api_without_a_checkout(self):
        gh = self.hub()
        out = self.run_gather(gh)
        self.assertEqual(self.read("sdk.json"), REG["sdks"]["js"])
        self.assertEqual(self.read("pr.json"), {
            "repo": FORK, "number": 5, "title": "Add custom claims", "body": BODY, "head_ref": SALTED, "head_sha": HEAD,
            "base_ref": "main", "base_sha": BASE_SHA, "sync": False, "author_model": "claude-opus-5.5",
            "reviewer_model": "gpt-6.1-sol", "change_id": "custom-claims", "origin": "openfga/go-sdk/pull/228",
            "tracking": "9", "round": 2, "rounds": 2,
        })
        self.assertEqual(self.read("pr.diff"), DIFF)
        [diff_call] = [call for call in gh.calls if call["raw"] and call["path"] == PULL]
        self.assertEqual(diff_call["headers"], {"Accept": "application/vnd.github.diff"})
        self.assertEqual(self.read("files.json"), [{"name": "src/a.ts", "status": "modified", "additions": 2, "deletions": 0}])
        self.assertEqual(self.read("source.json"), {"path": "openfga/go-sdk/pull/228", "title": SOURCE_PR["title"],
                                                    "body": SOURCE_PR["body"], "state": "closed", "merged": True})
        self.assertNotIn("github.com", (self.out / "source.json").read_text())
        self.assertEqual(self.read("source.diff"), SOURCE_DIFF)
        self.assertEqual(self.read("verify.md"), verified()["body"])
        self.assertEqual(self.read("reviews.json"), [{
            "round": 1, "head": OLD, "verdict": "changes", "body": mark(OLD, 1, "changes")["body"],
            "comments": [{"path": "src/a.ts", "line": 3, "body": "Send `scope`."}],
        }])
        self.assertIn("\n".join([
            "### Review context",
            "",
            f"{review.pull_link(FORK, 5)} at `ccccccc`, round 2 of 2.",
            "Author `claude-opus-5.5`, reviewer `gpt-6.1-sol`.",
            f"Diff of {len(DIFF)} characters in 1 file. Earlier factory reviews: 1.",
        ]), out)

    def test_the_diff_comes_from_the_checkout(self):
        sdk, base, head = make_repo(self.tmp)
        pr = pull(head)
        pr["base"]["sha"] = base
        gh = self.hub(pr)
        self.run_gather(gh, sdk, HEAD_SHA=head)
        self.assertEqual(self.read("pr.diff"), git(sdk, "diff", f"{base}...{head}") + "\n")
        self.assertIn("+b\n+c\n", self.read("pr.diff"))
        self.assertFalse(any(call["raw"] and call["path"] == PULL for call in gh.calls))

    def test_missing_diff_files_and_reviews_are_named(self):
        gh = self.hub(extra={
            ("GET", PULL): lambda call: ApiError(PULL, "HTTP 406: Not Acceptable") if call["raw"] else pull(),
            ("GET", FILES): ApiError(FILES, "HTTP 500: boom"),
            ("GET", COMMENTS): ApiError(COMMENTS, "HTTP 500: boom"),
            ("GET", f"{REVIEWS}/7/comments"): ApiError(REVIEWS, "HTTP 500: boom"),
        })
        out = self.run_gather(gh)
        self.assertEqual(self.read("pr.diff"), "[GitHub did not return the diff. Use git in ./sdk.]\n")
        self.assertEqual(self.read("files.json"), [])
        self.assertEqual(self.read("verify.md"), "No verify result for this head.\n")
        self.assertEqual(self.read("reviews.json")[0]["comments"], [])
        self.assertIn("Diff of 0 characters in 0 files. Earlier factory reviews: 1.", out)

    def test_a_sync_pr_has_no_source_or_author(self):
        body = f"Regenerates the SDK from the generator.\n\nTracking: `{HUB}#9`"
        gh = self.hub(pull(ref="sync/factory-custom-claims", body=body), extra={("GET", REVIEWS): []})
        out = self.run_gather(gh, ROUND="1")
        info = self.read("pr.json")
        self.assertEqual((info["sync"], info["author_model"], info["origin"], info["change_id"]),
                         (True, "", "", "custom-claims"))
        self.assertEqual(self.read("source.json"), {"path": "", "note": (
            "The PR has no SDK-Factory-Origin trailer. A generator sync PR names its generator commit in the PR body.")})
        self.assertEqual(self.read("source.diff"), "")
        self.assertFalse(any(call["path"].startswith("repos/openfga/") for call in gh.calls))
        self.assertIn("Author unknown, reviewer `gpt-6.1-sol`.", out)

    def test_the_source_is_read_by_kind(self):
        commit, issue = "repos/openfga/go-sdk/commits/abcdef1", "repos/openfga/go-sdk/issues/12"
        message = "Add custom claims\n\nThe token request sends them."
        too_big = "The source diff is too large to fetch. Read the files in the source instead."
        cases = [
            ("commit", "openfga/go-sdk/commit/abcdef1",
             {("GET", commit): raw_or({"commit": {"message": message}}, SOURCE_DIFF)},
             {"title": "Add custom claims", "body": message}, SOURCE_DIFF),
            ("issue", "openfga/go-sdk/issues/12",
             {("GET", issue): {"title": "Custom claims", "body": "Please add them.", "state": "open"}},
             {"title": "Custom claims", "body": "Please add them.", "state": "open", "merged": False}, ""),
            ("missing", "openfga/go-sdk/pull/228", {}, {"note": "GitHub did not return the source (HTTP 404)."}, ""),
            ("no status", "openfga/go-sdk/pull/228", {("GET", SOURCE): ApiError(SOURCE, "timed out")},
             {"note": "GitHub did not return the source (HTTP ?)."}, ""),
            ("diff too big", "openfga/go-sdk/pull/228",
             {("GET", SOURCE): lambda call: ApiError(SOURCE, "HTTP 406: Not Acceptable") if call["raw"] else SOURCE_PR},
             {"title": SOURCE_PR["title"], "body": SOURCE_PR["body"], "state": "closed", "merged": True, "note": too_big}, ""),
        ]
        self.out.mkdir()
        for name, origin, responses, info, diff in cases:
            with self.subTest(name):
                gh = FakeGitHub(responses)
                review.gather_source(gh, origin, self.out)
                self.assertEqual(self.read("source.json"), {"path": origin, **info})
                self.assertEqual(self.read("source.diff"), diff)
                self.assertLessEqual(len(gh.calls), 2)

    def test_bad_inputs_are_all_named_before_any_call(self):
        cases = [
            ({"SDK": "x", "PR": "5a", "HEAD_SHA": "abc", "ROUND": "0", "MODEL": "gpt-9"},
             ["Unknown SDK `x`.", "The pull request must be a number.", "The head must be a full commit SHA.",
              "The round must be a number from 1 to 9.", "The model `gpt-9` is not in `models.allowed`."]),
            ({"TARGET_REPO": "lab/go-sdk", "HEAD_SHA": HEAD.upper(), "ROUND": "10"},
             ["The target repository must be `lab/js-sdk`.", "The head must be a full commit SHA.",
              "The round must be a number from 1 to 9."]),
        ]
        for env, problems in cases:
            with self.subTest(problems[0]):
                gh = self.hub()
                with self.assertRaises(FactoryError) as caught:
                    self.run_gather(gh, **env)
                self.assertEqual(caught.exception.problems, problems)
                self.assertEqual(gh.calls, [])
                self.assertFalse(self.out.exists())

    def test_a_stale_run_writes_nothing(self):
        for pr, message in ((pull(state="closed"), "The pull request is closed or is not a factory pull request."),
                            (pull("e" * 40), "The pull request has a newer head. Its next verify run starts a review of it.")):
            with self.subTest(message):
                with self.assertRaises(FactoryError) as caught:
                    self.run_gather(self.hub(pr))
                self.assertEqual(str(caught.exception), message)
                self.assertFalse(self.out.exists())

    def test_main_reports_bad_inputs(self):
        with run_env(GITHUB_REPOSITORY=HUB, MODEL="gpt-9"), mock.patch.object(review, "GitHub", return_value=self.hub()), \
                mock.patch.object(review, "load_registry", return_value=REG):
            status, out = captured(review.main, ["gather"])
        self.assertEqual((status, out.strip()), (1, "::error::The model `gpt-9` is not in `models.allowed`."))


class ReviewFilesTest(unittest.TestCase):
    """The reviewer lock, the port worker's revision step and the verify job must match the registry and the script."""

    @classmethod
    def setUpClass(cls):
        workflows = ROOT / ".github/workflows"
        cls.registry = load_registry()
        cls.lock_text = (workflows / "factory-review.lock.yml").read_text()
        cls.lock = load_yaml(workflows / "factory-review.lock.yml")
        cls.port = load_yaml(workflows / "factory-port.lock.yml")
        cls.verify = load_yaml(workflows / "factory-verify.yml")

    def test_reviewer_inputs_match_the_registry(self):
        inputs = self.lock["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(set(inputs["sdk"]["options"]), set(self.registry["sdks"]))
        self.assertEqual(inputs["model"]["options"], review.allowed_models(self.registry))
        self.assertEqual(inputs["model"]["default"], review.settings(self.registry)["default"])
        self.assertEqual(self.lock["run-name"], review.run_title(
            "${{ inputs.target_repo }}", "${{ inputs.pr }}", "${{ inputs.head_sha }}", "${{ inputs.round }}"))

    def test_registry_reviewers_are_allowed_and_cross_family(self):
        conf, allowed = review.settings(self.registry), review.allowed_models(self.registry)
        self.assertIn(conf["default"], allowed)
        for author_family, model in conf["reviewers"].items():
            with self.subTest(author_family):
                self.assertIn(model, allowed)
                self.assertNotEqual(review.family(model), author_family)

    def test_the_reviewer_runs_only_when_allowed_and_never_holds_the_token(self):
        gate = self.lock["jobs"]["activation"]["if"]
        for part in ("vars.FACTORY_PAUSED != 'true'", "vars.FACTORY_REVIEW != 'off'",
                     "needs.pre_activation.outputs.fresh_result == 'success'"):
            self.assertIn(part, gate)
        [fresh] = [step for step in self.lock["jobs"]["pre_activation"]["steps"] if step.get("id") == "fresh"]
        self.assertIs(fresh["continue-on-error"], True)
        holders = [name for name, job in self.lock["jobs"].items() if "secrets.FACTORY_TOKEN" in json.dumps(job)]
        self.assertEqual(holders, ["post_review"])
        self.assertNotIn("write", self.lock["jobs"]["agent"]["permissions"].values())
        self.assertIn('"blocked":["*haiku*"]', self.lock_text)

    def test_the_port_worker_prepares_revisions(self):
        self.assertEqual(self.port["on"]["workflow_dispatch"]["inputs"]["revise_pr"]["default"], "")
        steps = [step for job in self.port["jobs"].values() for step in job.get("steps") or []]
        [step] = [step for step in steps if step.get("name") == "Prepare the revision"]
        self.assertEqual(step["if"], "github.event.inputs.revise_pr != ''")
        self.assertEqual(step["run"], "python3 factory/scripts/factory_review.py prepare")
        self.assertEqual(set(step["env"]) - {"GH_TOKEN"}, {"SDK", "TARGET_REPO", "CHANGE_ID", "BASE_BRANCH", "REVISE_PR"})

    def test_verify_starts_the_reviews(self):
        job = self.verify["jobs"]["review"]
        self.assertEqual(job["needs"], ["find", "report"])
        self.assertIn("vars.FACTORY_REVIEW != 'off'", job["if"])
        [step] = [step for step in job["steps"] if step.get("name") == "Start reviews"]
        self.assertEqual(step["run"], 'python3 factory/scripts/factory_review.py dispatch --pr "$PR"')
        self.assertEqual((step["env"]["HAS_FACTORY_TOKEN"], step["env"]["HAS_COPILOT_TOKEN"]),
                         ("${{ secrets.FACTORY_TOKEN != '' }}", "${{ secrets.COPILOT_GITHUB_TOKEN != '' }}"))
        self.assertNotIn("write", {value for key, value in job["permissions"].items() if key != "actions"})
