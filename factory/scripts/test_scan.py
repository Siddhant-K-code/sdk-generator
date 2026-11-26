"""Tests for factory_scan.py. Run: python3 -m unittest discover -s factory/scripts"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import factory_route as route  # noqa: E402
import factory_scan as scan  # noqa: E402
from factory_lib import LANES, issue_form  # noqa: E402
from test_factory import HUB, REGISTRY, UPSTREAM_MENTION, FakeGitHub  # noqa: E402

NOW = dt.datetime(2026, 11, 26, 12, 0, tzinfo=dt.timezone.utc)
PIN, HEAD = "1" * 40, "2" * 40


def pull(number: int, title: str, merged: str | None = "2026-11-25T10:00:00Z", **extra) -> dict:
    item = {
        "number": number,
        "title": title,
        "body": "",
        "merged_at": merged,
        "merge_commit_sha": f"{number:07x}" + "f" * 33,
        "user": {"login": "someone", "type": "User"},
        "labels": [],
        "head": {"ref": f"feat/{number}"},
    }
    item.update(extra)
    return item


def quietly(fn, *args, **kwargs) -> tuple:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        status = fn(*args, **kwargs)
    return status, out.getvalue()


def posted(gh, suffix: str) -> list:
    return [c["fields"] for c in gh.calls if c["method"] == "POST" and c["path"].endswith(suffix)]


def patched(gh) -> list:
    return [(c["path"].rsplit("/", 1)[1], c["fields"]) for c in gh.calls if c["method"] == "PATCH"]


class WatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.acceptance = Path(self.tmp.name)
        pack = self.acceptance / "custom-claims" / "pack.yaml"
        pack.parent.mkdir(parents=True)
        pack.write_text("source: openfga/js-sdk/pull/228\n")

    def tearDown(self):
        self.tmp.cleanup()

    def responses(self, js_pulls: list, routed=()) -> dict:
        numbers = iter(range(31, 99))
        responses = {
            ("GET", f"repos/{HUB}/issues"): lambda call: list(routed) if scan.PROPAGATE_LABEL in call["path"] else [],
            ("POST", f"repos/{HUB}/issues"): lambda call: {"number": next(numbers)},
        }
        for entry in REGISTRY["sdks"].values():
            responses[("GET", f"repos/{entry['upstream']}/pulls")] = []
        responses[("GET", "repos/openfga/js-sdk/pulls")] = js_pulls
        return responses

    def watch(self, gh, **kwargs) -> tuple:
        return quietly(scan.watch, gh, REGISTRY, HUB, now=NOW, acceptance_root=self.acceptance, **kwargs)

    def test_opens_one_issue_per_new_feat_or_fix(self):
        seen = pull(239, "feat: add retries")
        pulls = [
            pull(240, "feat(client): add list users (#240)", merged="2026-11-25T11:00:00Z"),
            pull(228, "feat: support custom claims, thanks @someone", merged="2026-11-24T20:00:00Z"),
            seen,
            pull(241, "fix(deps): bump axios"),
            pull(242, "chore: release v1"),
            pull(243, "feat: bump everything", user={"login": "renovate[bot]", "type": "Bot"}),
            pull(244, "feat: too old", merged="2026-11-20T10:00:00Z"),
            pull(245, "feat: closed without merging", merged=None),
            pull(246, "feat: ported here", body="SDK-Factory-Origin: openfga/go-sdk/pull/9"),
            pull(247, "fix: regenerate", labels=[{"name": "sync-with-generator"}]),
        ]
        routed = [
            {"body": issue_form(LANES["B"], f"openfga/js-sdk/commit/{seen['merge_commit_sha']}")},
            {"body": "", "pull_request": {}},
        ]
        gh = FakeGitHub(self.responses(pulls, routed))

        status, out = self.watch(gh)

        self.assertEqual(status, 0)
        created = posted(gh, f"repos/{HUB}/issues")
        self.assertEqual(
            [fields["title"] for fields in created],
            ["factory: js: feat: support custom claims, thanks someone", "factory: js: feat(client): add list users"],
        )
        for fields in created:
            self.assertEqual(fields["labels[]"], scan.CANDIDATE_LABEL)
            self.assertIsNone(UPSTREAM_MENTION.search(fields["title"] + fields["body"]), fields["body"])
        first, second = (route.parse_request(fields["body"], REGISTRY) for fields in created)
        self.assertEqual((first.lane, first.source.path, first.change_id), ("B", "openfga/js-sdk/pull/228", "custom-claims"))
        self.assertEqual((second.source.path, second.change_id), ("openfga/js-sdk/pull/240", ""))
        self.assertIn("Opened [#31]", out)

    def test_dry_run_opens_nothing_and_caps_oldest_first(self):
        pulls = [pull(300 + n, f"fix: bug {n}", merged=f"2026-11-25T0{n}:00:00Z") for n in range(7)]
        gh = FakeGitHub(self.responses(pulls))

        status, out = self.watch(gh, dry_run=True)

        self.assertEqual(status, 0)
        self.assertEqual(posted(gh, "issues"), [])
        self.assertEqual(out.count("Would open"), 5)
        self.assertIn("`openfga/js-sdk/pull/300`", out)
        self.assertNotIn("`openfga/js-sdk/pull/306`", out)
        self.assertIn("2 more wait for the next run", out)


class DriftTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.makefile = Path(self.tmp.name) / "Makefile"
        self.makefile.write_text(f"# Main config\nOPEN_API_REF ?= {PIN}\nOPEN_API_URL = x\n")

    def tearDown(self):
        self.tmp.cleanup()

    def drift(self, head=HEAD, files=("docs/openapiv2/apidocs.swagger.json",), issues=()) -> tuple:
        responses = {
            ("GET", "repos/openfga/api/git/ref/heads/main"): {"object": {"sha": head}},
            ("GET", f"repos/openfga/api/compare/{PIN}...{head}"): {
                "ahead_by": 2,
                "files": [{"filename": name} for name in files],
                "commits": [
                    {"commit": {"message": "feat: add conditions to list users (#250)\n\nDetails"}},
                    {"commit": {"message": "docs: fix a typo"}},
                ],
            },
            ("GET", f"repos/{HUB}/issues"): list(issues),
            ("POST", f"repos/{HUB}/issues"): {"number": 40},
        }
        for number in (5, 6):
            responses[("PATCH", f"repos/{HUB}/issues/{number}")] = {}
            responses[("POST", f"repos/{HUB}/issues/{number}/comments")] = {}
        gh = FakeGitHub(responses)
        status, out = quietly(scan.drift, gh, REGISTRY, HUB, makefile=self.makefile)
        self.assertEqual(status, 0)
        return gh, out

    def test_opens_a_lane_a_issue_for_spec_changes(self):
        gh, _ = self.drift(files=("README.md", "openfga/v1/openfga.proto"))

        [fields] = posted(gh, f"repos/{HUB}/issues")
        self.assertEqual((fields["title"], fields["labels[]"]), (scan.DRIFT_TITLE, scan.DRIFT_LABEL))
        request = route.parse_request(fields["body"], REGISTRY)
        self.assertEqual((request.lane, request.source.path), ("A", f"openfga/api/commit/{HEAD}"))
        self.assertEqual(request.change_id, f"api-{HEAD[:7]}")
        self.assertIn("`openfga/v1/openfga.proto`", fields["body"])
        self.assertNotIn("README.md", fields["body"])
        self.assertIn("`feat: add conditions to list users (#250)`", fields["body"])
        self.assertIsNone(UPSTREAM_MENTION.search(fields["body"]))

    def test_keeps_one_issue_current_until_it_is_routed(self):
        subjects = ["feat: add conditions to list users (#250)", "docs: fix a typo"]
        current = scan.drift_body("openfga/api", PIN, HEAD, 2, ["docs/openapiv2/apidocs.swagger.json"], subjects)
        older = {"number": 5, "body": "old", "labels": [{"name": scan.DRIFT_LABEL}]}
        cases = {
            "stale body is updated": (older, True),
            "current body is left": ({**older, "body": current}, False),
            "routed issue is left": ({**older, "labels": [{"name": scan.PROPAGATE_LABEL}]}, False),
        }
        for name, (issue, expect_patch) in cases.items():
            with self.subTest(name):
                gh, _ = self.drift(issues=[issue])
                self.assertEqual(posted(gh, f"repos/{HUB}/issues"), [])
                self.assertEqual([number for number, _ in patched(gh)] == ["5"], expect_patch)

    def test_closes_when_the_pin_has_the_spec(self):
        open_issue = {"number": 5, "body": "x", "labels": [{"name": scan.DRIFT_LABEL}]}
        for name, head, files in (("pin is main", PIN, ()), ("no spec change", HEAD, ("README.md",))):
            with self.subTest(name):
                gh, out = self.drift(head=head, files=files, issues=[open_issue])
                self.assertEqual(patched(gh), [("5", {"state": "closed", "state_reason": "completed"})])
                self.assertEqual(len(posted(gh, "/comments")), 1)
                self.assertIn("No spec drift.", out)

    def test_routed_issue_closes_once_its_commit_is_pinned(self):
        landed = {
            "number": 6,
            "body": issue_form(LANES["A"], f"`openfga/api/commit/{PIN}`"),
            "labels": [{"name": scan.DRIFT_LABEL}, {"name": scan.PROPAGATE_LABEL}],
        }
        gh, _ = self.drift(issues=[landed])

        self.assertEqual(patched(gh), [("6", {"state": "closed", "state_reason": "completed"})])
        [fields] = posted(gh, f"repos/{HUB}/issues")
        self.assertIn(f"openfga/api/commit/{HEAD}", fields["body"])


class SyncTest(unittest.TestCase):
    def responses(self, behind: int) -> dict:
        upstreams = ["openfga/sdk-generator", "openfga/api"] + [e["upstream"] for e in REGISTRY["sdks"].values()]
        return {("GET", f"repos/{upstream}/compare/main...lab:main"): {"behind_by": behind} for upstream in upstreams}

    def test_without_a_token_only_reports(self):
        gh = FakeGitHub(self.responses(behind=3))

        status, out = quietly(scan.sync, gh, REGISTRY, HUB, "")

        self.assertEqual(status, 0)
        self.assertEqual(gh.merges(), [])
        self.assertIn("`FACTORY_TOKEN` is not set", out)
        self.assertEqual(out.count("3 commits behind"), 7)

    def test_a_failed_sync_fails_the_run(self):
        responses = self.responses(behind=1)
        responses[("POST", f"repos/{HUB}/merge-upstream")] = {}
        gh = FakeGitHub(responses)

        status, out = quietly(scan.sync, gh, REGISTRY, HUB, "ftok")

        self.assertEqual(status, 1)
        self.assertIn("synced 1 commit |", out)
        self.assertEqual(out.count("sync failed (HTTP 404)"), 6)
        self.assertEqual({call["token"] for call in gh.merges()}, {"ftok"})


if __name__ == "__main__":
    unittest.main()
