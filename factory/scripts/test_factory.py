"""Tests for the SDK factory scripts. Run: python3 -m unittest discover -s factory/scripts"""

from __future__ import annotations

import copy
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import factory_route as route  # noqa: E402
from factory_lib import (  # noqa: E402
    KINDS,
    LANES,
    MODEL_TIERS,
    ROOT,
    ApiError,
    FactoryError,
    code,
    issue_form,
    load_registry,
    load_yaml,
    normalize_source,
)

HUB = "lab/sdk-generator"
REGISTRY = {
    "api": {"upstream": "openfga/api", "repo": "openfga-api"},
    "generator": {"upstream": "openfga/sdk-generator", "repo": "sdk-generator"},
    "sdks": {
        "js": {"upstream": "openfga/js-sdk", "repo": "js-sdk"},
        "go": {"upstream": "openfga/go-sdk", "repo": "openfga-go-sdk"},
        "python": {"upstream": "openfga/python-sdk", "repo": "openfga-python-sdk"},
        "java": {"upstream": "openfga/java-sdk", "repo": "java-sdk"},
        "dotnet": {"upstream": "openfga/dotnet-sdk", "repo": "dotnet-sdk"},
    },
    "models": {
        "allowed": ["claude-opus-5.5", "gpt-6.1-sol", "gpt-6-luna"],
        "tiers": {"deep": "claude-opus-5.5", "standard": "gpt-6.1-sol", "light": "gpt-6-luna"},
        "default_tier": "standard",
        "kinds": {"baseline": "deep", "core-api": "standard", "other-api": "standard", "fix": "standard"},
    },
}
UPSTREAM_MENTION = re.compile(r"https://github\.com/openfga/|openfga/[\w.-]+#\d")


class FakeGitHub:
    """Answers from responses, keyed by (method, path without the query). Comment posts get ids, and a
    PATCH of a posted comment replaces it unless responses has its own answer for that PATCH."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls = []
        self.posted = {}

    def api(self, path, *, method="GET", fields=None, headers=None, paginate=False, token=None):
        call = {"method": method, "path": path, "fields": dict(fields or []), "headers": headers or {}, "token": token}
        self.calls.append(call)
        key = (method, path.split("?")[0])
        edit = re.fullmatch(r"repos/[^/]+/[^/]+/issues/comments/(\d+)", key[1])
        if method == "PATCH" and edit and key not in self.responses and int(edit.group(1)) in self.posted:
            self.posted[int(edit.group(1))] = call["fields"]["body"]
            return {"id": int(edit.group(1))}
        if key not in self.responses:
            raise ApiError(path, "HTTP 404: Not Found")
        value = self.responses[key]
        if callable(value):
            value = value(call)
        if isinstance(value, Exception):
            raise value
        if method == "POST" and key[1].endswith("/comments") and isinstance(value, (dict, type(None))):
            comment_id = 1000 + len(self.posted)
            self.posted[comment_id] = call["fields"]["body"]
            value = {**(value or {}), "id": comment_id}
        return copy.deepcopy(value)

    def comments(self) -> list:
        """Bodies of the comments as they stand now, oldest first."""
        return list(self.posted.values())

    def dispatches(self) -> list:
        return [c for c in self.calls if c["path"].endswith("/dispatches")]

    def merges(self) -> list:
        return [c for c in self.calls if c["path"].endswith("/merge-upstream")]


def compare(upstream: str, behind: int = 0) -> tuple:
    return ("GET", f"repos/{upstream}/compare/main...lab:main"), {"behind_by": behind}


def base_responses(body: str, comments=None) -> dict:
    responses = {
        ("GET", f"repos/{HUB}"): {"default_branch": "main"},
        ("GET", f"repos/{HUB}/issues/7"): {"body": body, "user": {"login": "maintainer"}},
        ("GET", f"repos/{HUB}/collaborators/maintainer/permission"): {"permission": "write"},
        ("GET", f"repos/{HUB}/issues/7/comments"): comments or [],
        ("POST", f"repos/{HUB}/issues/7/comments"): {},
        ("POST", f"repos/{HUB}/actions/workflows/{route.PORT_WORKFLOW}/dispatches"): {
            "html_url": f"https://github.com/{HUB}/actions/runs/11"
        },
        ("POST", f"repos/{HUB}/actions/workflows/{route.SYNC_WORKFLOW}/dispatches"): {
            "html_url": f"https://github.com/{HUB}/actions/runs/12"
        },
    }
    for upstream in ["openfga/sdk-generator"] + [entry["upstream"] for entry in REGISTRY["sdks"].values()]:
        key, value = compare(upstream)
        responses[key] = value
    for entry in REGISTRY["sdks"].values():
        responses[("GET", f"repos/lab/{entry['repo']}/pulls")] = []
    return responses


def merged_pr(title: str, body: str = "", **extra) -> dict:
    pr = {
        "title": title,
        "body": body,
        "state": "closed",
        "merged_at": "2026-11-20T10:00:00Z",
        "merge_commit_sha": "a" * 40,
        "labels": [],
        "head": {"ref": "feat/custom-claims", "sha": "c" * 40, "repo": {"full_name": "someone/js-sdk"}},
    }
    pr.update(extra)
    return pr


class RouteCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.acceptance = Path(self.tmp.name)
        for sdk in ("dotnet", "go", "java", "python"):
            runner = self.acceptance / "custom-claims" / sdk / "run.sh"
            runner.parent.mkdir(parents=True)
            runner.write_text("#!/usr/bin/env bash\n")

    def tearDown(self):
        self.tmp.cleanup()

    def route(self, gh, **kwargs) -> int:
        options = {"factory_token": "ftok", "has_copilot": True, "acceptance_root": self.acceptance}
        options.update(kwargs)
        return route.run(gh, REGISTRY, HUB, 7, **options)

    def assertQuiet(self, text: str):
        self.assertIsNone(UPSTREAM_MENTION.search(text), text)


class NormalizeSourceTest(unittest.TestCase):
    def test_accepted_forms(self):
        cases = {
            "https://github.com/openfga/js-sdk/pull/228": ("openfga/js-sdk", "pull", "228"),
            "`openfga/js-sdk/pull/228/files`": ("openfga/js-sdk", "pull", "228"),
            "openfga/js-sdk/pulls/228": ("openfga/js-sdk", "pull", "228"),
            "github.com/openfga/js-sdk/commit/ABCDEF1": ("openfga/js-sdk", "commit", "abcdef1"),
            "<https://github.com/openfga/js-sdk/issues/12#issuecomment-1>": ("openfga/js-sdk", "issues", "12"),
            "openfga/js-sdk#228": ("openfga/js-sdk", "short", "228"),
            "openfga/sdk-generator/tree/feat/x": ("openfga/sdk-generator", "tree", "feat/x"),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                source = normalize_source(raw)
                self.assertEqual((source.repo, source.kind, source.ref), expected)

    def test_rejected_forms(self):
        for raw in ["", "openfga/js-sdk", "../x/pull/1", "openfga/js-sdk/tree/a/../b", "openfga/js-sdk/blob/main/x"]:
            with self.subTest(raw=raw), self.assertRaises(FactoryError):
                normalize_source(raw)

    def test_code_span_is_table_safe(self):
        self.assertEqual(code("a|b`c\nd"), "`a\\|b c d`")


class ParseRequestTest(unittest.TestCase):
    def test_form_body(self):
        body = issue_form(
            "B: port an SDK change", "openfga/js-sdk/pull/228", "custom-claims", "python, go", "Use custom_claims.",
            kind="Baseline behavior: auth, retries, rate limits, streaming, headers or raw requests",
        )
        body = body.replace("- [ ] Plan only", "- [X] Plan only")
        request = route.parse_request(body, REGISTRY)
        self.assertEqual(request.lane, "B")
        self.assertEqual(request.kind, "baseline")
        self.assertEqual(request.change_id, "custom-claims")
        self.assertEqual(request.targets, ["go", "python"])
        self.assertTrue(request.plan_only)
        self.assertFalse(request.port_after)
        self.assertEqual(request.notes, "Use custom_claims.")

    def test_problems_are_collected(self):
        body = issue_form("B: port an SDK change", "openfga/js-sdk/pull/228", "Custom Claims", "go, ruby")
        with self.assertRaises(FactoryError) as caught:
            route.parse_request(body, REGISTRY)
        self.assertEqual(len(caught.exception.problems), 2)
        self.assertIn("`ruby`", caught.exception.problems[1])


class LaneBTest(RouteCase):
    def test_ports_to_every_other_sdk_after_syncing_forks(self):
        body = issue_form("B: port an SDK change", "https://github.com/openfga/js-sdk/pull/228", "custom-claims")
        responses = base_responses(body)
        responses[("GET", "repos/openfga/js-sdk/pulls/228")] = merged_pr("feat: support custom claims (#228)")
        key, value = compare("openfga/python-sdk", behind=2)
        responses[key] = value
        responses[("POST", "repos/lab/openfga-python-sdk/merge-upstream")] = {"merge_type": "fast-forward"}
        gh = FakeGitHub(responses)

        self.assertEqual(self.route(gh), 0)

        dispatches = gh.dispatches()
        self.assertEqual([d["fields"]["inputs[sdk]"] for d in dispatches], ["go", "python", "java", "dotnet"])
        first = dispatches[0]
        self.assertEqual(first["fields"]["ref"], "main")
        self.assertEqual(first["fields"]["inputs[source]"], "openfga/js-sdk/pull/228")
        self.assertEqual(first["fields"]["inputs[target_repo]"], "lab/openfga-go-sdk")
        self.assertEqual(first["fields"]["inputs[base_branch]"], "main")
        self.assertEqual(first["fields"]["inputs[tracking_issue]"], "7")
        self.assertNotIn("inputs[notes]", first["fields"])
        self.assertEqual(first["headers"], {"X-GitHub-Api-Version": route.API_VERSION})
        self.assertEqual([(m["path"], m["token"]) for m in gh.merges()], [("repos/lab/openfga-python-sdk/merge-upstream", "ftok")])

        [comment] = gh.comments()
        self.assertIn(route.TRACKING_MARKER.format("dispatched"), comment)
        self.assertIn("synced 2 commits", comment)
        self.assertIn("runners for dotnet, go, java, python", comment)
        self.assertIn("adds a mention", comment)
        self.assertQuiet(comment)

    def test_plan_only_reads_but_changes_nothing(self):
        body = issue_form("B: port an SDK change", "openfga/js-sdk/pull/228", "custom-claims")
        body = body.replace("- [ ] Plan only", "- [x] Plan only")
        responses = base_responses(body)
        responses[("GET", "repos/openfga/js-sdk/pulls/228")] = merged_pr("feat: support custom claims")
        key, value = compare("openfga/go-sdk", behind=1)
        responses[key] = value
        gh = FakeGitHub(responses)

        self.assertEqual(self.route(gh), 0)

        self.assertEqual(gh.dispatches(), [])
        self.assertEqual(gh.merges(), [])
        [comment] = gh.comments()
        self.assertIn(route.TRACKING_MARKER.format("plan"), comment)
        self.assertIn("1 commit behind", comment)
        self.assertIn("Plan only", comment)
        self.assertNotIn("Setup needed", comment)

    def test_plan_only_still_lists_missing_setup(self):
        body = issue_form("B: port an SDK change", "openfga/js-sdk/pull/228", "custom-claims")
        body = body.replace("- [ ] Plan only", "- [x] Plan only")
        responses = base_responses(body)
        responses[("GET", "repos/openfga/js-sdk/pulls/228")] = merged_pr("feat: support custom claims")
        gh = FakeGitHub(responses)

        self.assertEqual(self.route(gh, factory_token="", has_copilot=False), 0)

        [comment] = gh.comments()
        self.assertIn("Plan only", comment)
        self.assertIn("Setup needed first", comment)
        self.assertIn("`FACTORY_TOKEN`", comment)
        self.assertIn("`COPILOT_GITHUB_TOKEN`", comment)

    def test_missing_copilot_token_falls_back_to_plan(self):
        body = issue_form("B: port an SDK change", "openfga/js-sdk/pull/228", "custom-claims")
        responses = base_responses(body)
        responses[("GET", "repos/openfga/js-sdk/pulls/228")] = merged_pr("feat: support custom claims")
        gh = FakeGitHub(responses)

        self.assertEqual(self.route(gh, has_copilot=False), 0)

        self.assertEqual(gh.dispatches(), [])
        [comment] = gh.comments()
        self.assertIn(route.TRACKING_MARKER.format("plan"), comment)
        self.assertIn("`COPILOT_GITHUB_TOKEN`", comment)

    def test_factory_change_is_refused(self):
        body = issue_form("B: port an SDK change", "lab/openfga-go-sdk/pull/3")
        responses = base_responses(body)
        responses[("GET", "repos/lab/openfga-go-sdk/pulls/3")] = merged_pr(
            "feat: custom claims", body="SDK-Factory-Origin: `openfga/js-sdk/pull/228`"
        )
        gh = FakeGitHub(responses)

        self.assertEqual(self.route(gh), 1)

        self.assertEqual(gh.dispatches(), [])
        [comment] = gh.comments()
        self.assertIn(route.ERROR_MARKER, comment)
        self.assertIn("itself a factory change", comment)

    def test_already_started_needs_force(self):
        body = issue_form("B: port an SDK change", "openfga/js-sdk/pull/228", "custom-claims", "go")
        earlier = [{"user": {"login": "github-actions[bot]"}, "body": route.TRACKING_MARKER.format("dispatched")}]
        responses = base_responses(body, comments=earlier)
        responses[("GET", "repos/openfga/js-sdk/pulls/228")] = merged_pr("feat: support custom claims")

        gh = FakeGitHub(responses)
        self.assertEqual(self.route(gh), 0)
        self.assertEqual(gh.dispatches(), [])
        self.assertIn("already started", gh.comments()[0])

        gh = FakeGitHub(responses)
        self.assertEqual(self.route(gh, force=True), 0)
        self.assertEqual(len(gh.dispatches()), 1)

    def test_sync_pr_targets_its_own_sdk_and_stacks_on_it(self):
        body = issue_form("B: port an SDK change", "lab/js-sdk/pull/5")
        responses = base_responses(body)
        responses[("GET", "repos/lab/js-sdk/pulls/5")] = {
            "title": "chore(sync): sync with generator",
            "body": "",
            "state": "open",
            "labels": [{"name": "factory"}],
            "head": {"ref": "sync/factory-api-pr-250", "sha": "d" * 40, "repo": {"full_name": "lab/js-sdk"}},
        }
        gh = FakeGitHub(responses)

        self.assertEqual(self.route(gh), 0)

        [only] = gh.dispatches()
        self.assertEqual(only["fields"]["inputs[sdk]"], "js")
        self.assertEqual(only["fields"]["inputs[base_branch]"], "sync/factory-api-pr-250")
        self.assertEqual(only["fields"]["inputs[change_id]"], "js-sdk-pr-5")
        self.assertNotIn("adds a mention", gh.comments()[0])

    def test_fork_that_cannot_sync_is_skipped(self):
        body = issue_form("B: port an SDK change", "openfga/js-sdk/pull/228", "custom-claims", "go, java")
        responses = base_responses(body)
        responses[("GET", "repos/openfga/js-sdk/pulls/228")] = merged_pr("feat: support custom claims")
        key, value = compare("openfga/java-sdk", behind=4)
        responses[key] = value
        responses[("POST", "repos/lab/java-sdk/merge-upstream")] = ApiError("x", "HTTP 409: conflict")
        gh = FakeGitHub(responses)

        self.assertEqual(self.route(gh), 0)

        self.assertEqual([d["fields"]["inputs[sdk]"] for d in gh.dispatches()], ["go"])
        self.assertIn("sync failed (HTTP 409)", gh.comments()[0])
        self.assertIn("skipped: fork not in sync", gh.comments()[0])


class RetryTest(RouteCase):
    """The router can run twice for one issue, stop halfway, or meet a port that is already open."""

    def lane_b(self, targets: str = "go") -> dict:
        body = issue_form("B: port an SDK change", "openfga/js-sdk/pull/228", "custom-claims", targets)
        responses = base_responses(body)
        responses[("GET", "repos/openfga/js-sdk/pulls/228")] = merged_pr("feat: support custom claims")
        return responses

    def test_placeholder_is_posted_before_dispatch_then_replaced(self):
        gh = FakeGitHub(self.lane_b())

        self.assertEqual(self.route(gh), 0)

        posts = [i for i, c in enumerate(gh.calls) if c["method"] == "POST" and c["path"].endswith("/comments")]
        [dispatch] = [i for i, c in enumerate(gh.calls) if c["path"].endswith("/dispatches")]
        [edit] = [i for i, c in enumerate(gh.calls) if c["method"] == "PATCH"]
        self.assertEqual(len(posts), 1)
        self.assertLess(posts[0], dispatch)
        self.assertLess(dispatch, edit)
        placeholder = gh.calls[posts[0]]["fields"]["body"]
        self.assertIn(route.TRACKING_MARKER.format("dispatched"), placeholder)
        self.assertIn("Starting.", placeholder)
        self.assertEqual(gh.calls[edit]["path"], f"repos/{HUB}/issues/comments/1000")
        [comment] = gh.comments()
        self.assertIn("[started]", comment)
        self.assertNotIn("Starting.", comment)

    def test_failed_edit_posts_the_plan_as_a_new_comment(self):
        responses = self.lane_b()
        responses[("PATCH", f"repos/{HUB}/issues/comments/1000")] = ApiError("x", "HTTP 502: Bad Gateway")
        gh = FakeGitHub(responses)

        self.assertEqual(self.route(gh), 0)

        placeholder, comment = gh.comments()
        self.assertIn("Starting.", placeholder)
        self.assertIn("[started]", comment)

    def test_run_that_starts_nothing_can_be_retried_without_force(self):
        responses = self.lane_b("go, python")
        responses[("POST", f"repos/{HUB}/actions/workflows/{route.PORT_WORKFLOW}/dispatches")] = ApiError("x", "HTTP 500: boom")
        gh = FakeGitHub(responses)

        self.assertEqual(self.route(gh), 1)

        [comment] = gh.comments()
        self.assertIn(route.TRACKING_MARKER.format("stopped"), comment)
        self.assertNotIn(route.TRACKING_MARKER.format("dispatched"), comment)
        self.assertEqual(comment.count("dispatch failed (HTTP 500)"), 2)
        self.assertIn("Nothing started.", comment)

        retry = self.lane_b("go, python")
        retry[("GET", f"repos/{HUB}/issues/7/comments")] = [{"user": {"login": "github-actions[bot]"}, "body": comment}]
        gh = FakeGitHub(retry)

        self.assertEqual(self.route(gh), 0)
        self.assertEqual(len(gh.dispatches()), 2)

    def test_open_port_pr_blocks_a_second_worker_even_with_force(self):
        responses = self.lane_b("go, python, java")
        go_prs = "https://github.com/lab/openfga-go-sdk/pull"
        responses[("GET", "repos/lab/openfga-go-sdk/pulls")] = [
            {"head": {"ref": "sdk-factory/custom-claims-extra", "repo": {"full_name": "lab/openfga-go-sdk"}}, "html_url": f"{go_prs}/2"},
            {"head": {"ref": "sdk-factory/custom-claims-0123456789abcdef", "repo": {"full_name": "lab/openfga-go-sdk"}}, "html_url": f"{go_prs}/3"},
        ]
        responses[("GET", "repos/lab/openfga-python-sdk/pulls")] = [
            {"head": {"ref": "sdk-factory/custom-claims", "repo": {"full_name": "someone/openfga-python-sdk"}}, "html_url": "x/9"},
        ]
        responses[("GET", "repos/lab/java-sdk/pulls")] = ApiError("x", "HTTP 502: Bad Gateway")
        gh = FakeGitHub(responses)

        self.assertEqual(self.route(gh, force=True), 1)

        self.assertEqual([d["fields"]["inputs[sdk]"] for d in gh.dispatches()], ["python"])
        [comment] = gh.comments()
        self.assertIn(route.TRACKING_MARKER.format("dispatched"), comment)
        self.assertIn(f"skipped: [open PR]({go_prs}/3)", comment)
        self.assertNotIn(f"{go_prs}/2", comment)
        self.assertIn("skipped: could not list open PRs (HTTP 502)", comment)

    def test_issue_author_needs_write_access(self):
        cases = [
            ("drive-by", {"permission": "read"}),
            ("drive-by", ApiError("x", "HTTP 404: Not Found")),
            ("", None),
        ]
        for login, permission in cases:
            with self.subTest(login=login, permission=permission):
                responses = self.lane_b()
                responses[("GET", f"repos/{HUB}/issues/7")]["user"] = {"login": login}
                if login:
                    responses[("GET", f"repos/{HUB}/collaborators/{login}/permission")] = permission
                gh = FakeGitHub(responses)

                self.assertEqual(self.route(gh), 1)

                self.assertEqual((gh.dispatches(), gh.merges()), ([], []))
                [comment] = gh.comments()
                self.assertIn(route.ERROR_MARKER, comment)
                self.assertIn("does not have write access to this repository", comment)
                self.assertNotIn("@drive-by", comment)

    def test_bot_authored_candidate_skips_the_permission_check(self):
        responses = self.lane_b()
        responses[("GET", f"repos/{HUB}/issues/7")]["user"] = {"login": "github-actions[bot]"}
        gh = FakeGitHub(responses)

        self.assertEqual(self.route(gh), 0)

        self.assertEqual(len(gh.dispatches()), 1)
        self.assertEqual([c for c in gh.calls if "/collaborators/" in c["path"]], [])


class ReachTest(RouteCase):
    REACH = '{"other-api": ["go", "js", "python"]}'

    def lane_b(self, targets: str = "") -> FakeGitHub:
        body = issue_form("B: port an SDK change", "openfga/js-sdk/pull/228", "custom-claims", targets, kind="Other API wrapper")
        responses = base_responses(body)
        responses[("GET", "repos/openfga/js-sdk/pulls/228")] = merged_pr("feat: support custom claims (#228)")
        return FakeGitHub(responses)

    def test_lane_b_ports_only_within_reach_unless_picked(self):
        gh = self.lane_b()
        self.assertEqual(self.route(gh, reach_override=self.REACH), 0)
        self.assertEqual([d["fields"]["inputs[sdk]"] for d in gh.dispatches()], ["go", "python"])
        [comment] = gh.comments()
        self.assertIn("| Kind of change | Other API wrapper |", comment)
        self.assertIn("The Other API wrapper reach leaves out `java` and `dotnet`. Pick them under **Target SDKs**", comment)

        gh = self.lane_b(targets="java")
        self.assertEqual(self.route(gh, reach_override=self.REACH), 0)
        self.assertEqual([d["fields"]["inputs[sdk]"] for d in gh.dispatches()], ["java"])
        [comment] = gh.comments()
        self.assertIn("reach leaves out `java`, but this change picked them, so they run.", comment)

    def test_lane_a_still_syncs_every_sdk_but_ports_only_within_reach(self):
        body = issue_form("A: API spec bump", "openfga/api/pull/250", kind="Other API wrapper")
        body = body.replace("- [ ] Lanes A and C", "- [X] Lanes A and C")
        responses = base_responses(body)
        responses[("GET", "repos/openfga/api/pulls/250")] = merged_pr("feat: add store filters (#250)")
        gh = FakeGitHub(responses)

        self.assertEqual(self.route(gh, reach_override=self.REACH), 0)

        ports = {d["fields"]["inputs[sdk]"]: d["fields"]["inputs[port_after]"] for d in gh.dispatches()}
        self.assertEqual(ports, {"js": "true", "go": "true", "python": "true", "java": "false", "dotnet": "false"})
        [comment] = gh.comments()
        self.assertIn("so their sync PRs get no port worker", comment)
        self.assertEqual(comment.count(", no port worker |"), 2)

    def test_bad_reach_variable_is_ignored_with_a_warning(self):
        gh = self.lane_b()
        self.assertEqual(self.route(gh, reach_override='{"other-api": "go"'), 0)
        self.assertEqual(len(gh.dispatches()), 4)
        [comment] = gh.comments()
        self.assertIn("`FACTORY_REACH` variable is not a JSON object", comment)

    def test_bad_reach_for_one_kind_falls_back_to_the_registry(self):
        registry = copy.deepcopy(REGISTRY)
        registry["reach"] = {"other-api": ["go"], "fix": "all"}

        reach, warnings = route.load_reach(registry, '{"other-api": "js", "fix": ["python"]}')

        self.assertEqual(reach, {"other-api": ["go"], "fix": ["python"]})
        [warning] = warnings
        self.assertIn("The reach for `other-api` in `FACTORY_REACH` is not `all` or a list", warning)


class ModelTest(RouteCase):
    DEEP = "Deep: strongest model, for baseline behavior and hard ports"

    def lane_b(self, kind: str = "", model: str = "") -> FakeGitHub:
        body = issue_form(
            "B: port an SDK change", "openfga/js-sdk/pull/228", "custom-claims", "go, python", kind=kind, model=model
        )
        responses = base_responses(body)
        responses[("GET", "repos/openfga/js-sdk/pulls/228")] = merged_pr("feat: support custom claims (#228)")
        return FakeGitHub(responses)

    def models(self, gh) -> list:
        return [d["fields"].get("inputs[model]") for d in gh.dispatches()]

    def test_tier_comes_from_the_issue_then_the_kind_then_the_default(self):
        cases = [
            ("Baseline behavior", "", "claude-opus-5.5", "Deep tier, from the kind of change"),
            ("Core API wrapper", "Light: fast, low-cost model", "gpt-6-luna", "Light tier, picked in the issue"),
            ("", "Auto: pick from the kind of change", "gpt-6.1-sol", "Standard tier, the default"),
        ]
        for kind, model, expected, why in cases:
            with self.subTest(kind=kind, model=model):
                gh = self.lane_b(kind, model)

                self.assertEqual(self.route(gh), 0)

                self.assertEqual(self.models(gh), [expected, expected])
                [comment] = gh.comments()
                self.assertIn(f"| Worker model | `{expected}` ({why}) |", comment)

    def test_models_variable_overrides_tiers_but_cannot_add_models(self):
        gh = self.lane_b("Baseline behavior")
        self.assertEqual(self.route(gh, models_override='{"tiers": {"deep": "gpt-6-luna"}}'), 0)
        self.assertEqual(self.models(gh), ["gpt-6-luna", "gpt-6-luna"])

        gh = self.lane_b("Baseline behavior")
        override = '{"tiers": {"deep": "claude-haiku-5.5"}, "allowed": ["claude-haiku-5.5"]}'
        self.assertEqual(self.route(gh, models_override=override), 0)
        self.assertEqual(self.models(gh), ["claude-opus-5.5", "claude-opus-5.5"])
        [comment] = gh.comments()
        self.assertIn("The deep model in `FACTORY_MODELS` is `claude-haiku-5.5`, not a model in `models.allowed`", comment)
        self.assertIn("`FACTORY_MODELS` can only set `tiers`, `kinds` and `default_tier`, so the factory ignored `allowed`.", comment)

    def test_haiku_in_the_registry_never_runs(self):
        registry = copy.deepcopy(REGISTRY)
        registry["models"]["allowed"].append("claude-haiku-5.5")
        registry["models"]["tiers"]["light"] = "claude-haiku-5.5"

        models, warnings = route.load_models(registry)

        self.assertNotIn("light", models["tiers"])
        self.assertIn("`models.allowed` in `sdks.yaml` lists a Haiku model. Haiku models never run.", warnings)
        plan = route.Plan(lane="B", source=normalize_source("openfga/js-sdk/pull/228"), kind="fix")
        request = route.Request(lane="B", source=plan.source, raw_source="", model_tier="light")
        route.choose_model(plan, request, models)
        self.assertEqual((plan.model, plan.model_tier), ("gpt-6.1-sol", "standard"))
        self.assertIn("No model is set for the Light tier, so the worker uses the Standard tier.", plan.warnings)

    def test_lanes_a_and_c_pass_the_model_only_to_port_workers(self):
        body = issue_form("A: API spec bump", "openfga/api/pull/250", kind="Core API wrapper", model=self.DEEP)
        responses = base_responses(body)
        responses[("GET", "repos/openfga/api/pulls/250")] = merged_pr("feat: add store filters (#250)")
        gh = FakeGitHub(responses)

        self.assertEqual(self.route(gh), 0)

        [only] = gh.dispatches()
        self.assertNotIn("inputs[model]", only["fields"])
        [comment] = gh.comments()
        self.assertIn("The model only applies to port workers, and this run starts none.", comment)
        self.assertNotIn("Worker model", comment)

        responses[("GET", f"repos/{HUB}/issues/7")]["body"] = body.replace("- [ ] Lanes A and C", "- [X] Lanes A and C")
        gh = FakeGitHub(responses)

        self.assertEqual(self.route(gh), 0)

        [only] = gh.dispatches()
        self.assertEqual((only["fields"]["inputs[port_after]"], only["fields"]["inputs[model]"]), ("true", "claude-opus-5.5"))
        self.assertIn("| Worker model | `claude-opus-5.5` (Deep tier, picked in the issue) |", gh.comments()[0])

    def test_unknown_model_answer_is_a_problem(self):
        body = issue_form("B: port an SDK change", "openfga/js-sdk/pull/228", model="Huge")
        with self.assertRaises(FactoryError) as caught:
            route.parse_request(body, REGISTRY)
        self.assertEqual(caught.exception.problems, ["Pick a model: Auto, Deep, Standard, Light."])


class LaneACTest(RouteCase):
    def test_api_bump_starts_one_sync_run_for_all_sdks(self):
        body = issue_form("A: API spec bump", "openfga/api/pull/250")
        responses = base_responses(body)
        responses[("GET", "repos/openfga/api/pulls/250")] = merged_pr("feat: add conditions to list users (#250)")
        gh = FakeGitHub(responses)

        self.assertEqual(self.route(gh), 0)

        [only] = gh.dispatches()
        self.assertTrue(only["path"].endswith(f"/{route.SYNC_WORKFLOW}/dispatches"))
        self.assertEqual(only["fields"]["inputs[sdk]"], "all")
        self.assertEqual(only["fields"]["inputs[openapi_ref]"], "a" * 40)
        self.assertEqual(only["fields"]["inputs[change_id]"], "api-pr-250")
        self.assertEqual(only["fields"]["inputs[port_after]"], "false")
        [comment] = gh.comments()
        self.assertIn("(derived from the source)", comment)
        self.assertQuiet(comment)

    def test_open_generator_pr_is_pinned_to_its_head(self):
        body = issue_form("C: generator template change", "openfga/sdk-generator/pull/700", targets="go")
        body = body.replace("- [ ] Lanes A and C", "- [X] Lanes A and C")
        responses = base_responses(body)
        responses[("GET", "repos/openfga/sdk-generator/pulls/700")] = {
            "title": "fix(go): retry on 429",
            "state": "open",
            "head": {"ref": "fix/retry", "sha": "b" * 40},
        }

        def old_api(call):
            if call["headers"]:
                return ApiError("x", "HTTP 400: unknown version")
            return None

        responses[("POST", f"repos/{HUB}/actions/workflows/{route.SYNC_WORKFLOW}/dispatches")] = old_api
        gh = FakeGitHub(responses)

        self.assertEqual(self.route(gh), 0)

        attempts = gh.dispatches()
        self.assertEqual(len(attempts), 2)
        fields = attempts[1]["fields"]
        self.assertEqual(fields["inputs[sdk]"], "go")
        self.assertEqual(fields["inputs[generator_repo]"], "openfga/sdk-generator")
        self.assertEqual(fields["inputs[generator_ref]"], "b" * 40)
        self.assertEqual(fields["inputs[change_id]"], "gen-pr-700")
        self.assertEqual(fields["inputs[port_after]"], "true")
        [comment] = gh.comments()
        self.assertIn(f"actions/workflows/{route.SYNC_WORKFLOW}", comment)
        self.assertIn("| Generator | `openfga/sdk-generator` at `bbbbbbb`, the pull request head when the router ran |", comment)
        self.assertIn("The generator pull request is still open, so the SDKs are generated from its head commit.", comment)
        self.assertIn("only run the factory on changes you have read", comment)

    def test_generator_commit_must_be_on_main_to_skip_the_warning(self):
        sha = "d" * 40
        body = issue_form("C: generator template change", f"openfga/sdk-generator/commit/{sha}", targets="go")
        for status, warned in (("ahead", False), ("identical", False), ("diverged", True), (None, True)):
            with self.subTest(status=status):
                responses = base_responses(body)
                responses[("GET", f"repos/openfga/sdk-generator/commits/{sha}")] = {
                    "sha": sha,
                    "commit": {"message": "fix(go): retry on 429"},
                }
                if status:
                    responses[("GET", f"repos/openfga/sdk-generator/compare/{sha}...main")] = {"status": status}
                gh = FakeGitHub(responses)

                self.assertEqual(self.route(gh), 0)

                [comment] = gh.comments()
                self.assertEqual("is not on `main`" in comment, warned, comment)

    def test_lane_a_rejects_a_source_outside_the_api(self):
        body = issue_form("A: API spec bump", "openfga/js-sdk/pull/228")
        gh = FakeGitHub(base_responses(body))

        self.assertEqual(self.route(gh), 1)

        self.assertIn("Lane A needs a source in", gh.comments()[0])


class RepositoryFilesTest(unittest.TestCase):
    """GitHub needs some SDK and option lists spelled out. They must match the registry and the parser."""

    @classmethod
    def setUpClass(cls):
        cls.registry = load_registry()
        form = load_yaml(ROOT / ".github/ISSUE_TEMPLATE/factory-propagate.yaml")
        cls.form = {field.get("id"): field.get("attributes") or {} for field in form["body"]}

    def test_sdk_lists_match_the_registry(self):
        sdks = set(self.registry["sdks"])
        worker = load_yaml(ROOT / ".github/workflows/factory-port.lock.yml")
        sync = load_yaml(ROOT / ".github/workflows/sync-sdks.yaml")

        self.assertEqual(set(self.form["targets"]["options"]), sdks)
        self.assertEqual(set(worker["on"]["workflow_dispatch"]["inputs"]["sdk"]["options"]), sdks)
        self.assertEqual(set(sync["on"]["workflow_dispatch"]["inputs"]["sdk"]["options"]) - {"all"}, sdks)
        self.assertEqual(set(route.SYNC_SDKS), sdks)

    def test_sync_matrix_uses_the_registry_make_targets(self):
        text = (ROOT / ".github/workflows/sync-sdks.yaml").read_text()
        matrix = json.loads(re.search(r"ALL='(\[.*?\])'", text, re.S).group(1))

        self.assertEqual(
            {row["language"]: row["make_target"] for row in matrix},
            {key: sdk["generator_target"] for key, sdk in self.registry["sdks"].items()},
        )

    def test_form_options_parse(self):
        self.assertEqual(set(self.form["lane"]["options"]), set(LANES.values()))
        self.assertEqual(sorted(route.match_kind(option) for option in self.form["kind"]["options"]), sorted(KINDS))
        self.assertEqual(sorted(route.match_tier(option) for option in self.form["model"]["options"]), sorted(MODEL_TIERS))
        self.assertEqual(route.load_reach(self.registry)[1], [])

    def test_model_lists_match_the_registry(self):
        models = self.registry["models"]
        worker = load_yaml(ROOT / ".github/workflows/factory-port.lock.yml")
        model_input = worker["on"]["workflow_dispatch"]["inputs"]["model"]

        self.assertEqual(model_input["options"], models["allowed"])
        self.assertEqual(model_input["default"], models["tiers"][models["default_tier"]])
        loaded, warnings = route.load_models(self.registry)
        self.assertEqual(warnings, [])
        self.assertEqual(set(loaded["tiers"]), set(MODEL_TIERS) - {"auto"})
        self.assertEqual(set(loaded["kinds"]), set(KINDS))

    def test_haiku_never_runs(self):
        workflows = ROOT / ".github/workflows"
        self.assertIn('"blocked":["*haiku*"]', (workflows / "factory-port.lock.yml").read_text())
        haiku = re.compile(r"(?i)\b[\w.]+-haiku\b|\bhaiku-[\d.]+")
        files = [ROOT / "factory/sdks.yaml", ROOT / ".github/ISSUE_TEMPLATE/factory-propagate.yaml", workflows / "sync-sdks.yaml"]
        files += sorted(workflows.glob("factory-*.md"))
        files += [path for path in sorted(workflows.glob("factory-*.yml")) if not path.name.endswith(".lock.yml")]
        for path in files:
            with self.subTest(path=path.name):
                self.assertIsNone(haiku.search(path.read_text()))


if __name__ == "__main__":
    unittest.main()
