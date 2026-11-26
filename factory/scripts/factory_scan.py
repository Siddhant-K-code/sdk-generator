#!/usr/bin/env python3
"""Scheduled scans for the SDK factory. Nothing here calls a model.

watch  Open a `factory:candidate` issue for each recent feat or fix pull request
       merged in an upstream SDK that no factory issue covers yet.
drift  Keep one `factory:api-drift` issue open while the spec on API main differs
       from the generator's `OPEN_API_REF` pin.
sync   Bring the hub, the API fork and every SDK fork up to upstream main.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import fnmatch
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from factory_lib import (  # noqa: E402
    ACCEPTANCE_DIR,
    CHANGE_ID_RE,
    LANES,
    ROOT,
    SERVER,
    ApiError,
    FactoryError,
    GitHub,
    code,
    first_line,
    fork_name,
    fork_sync,
    issue_form,
    load_registry,
    load_yaml,
    normalize_source,
    parse_sections,
    summary,
)

CANDIDATE_LABEL = "factory:candidate"
PROPAGATE_LABEL = "factory:propagate"
DRIFT_LABEL = "factory:api-drift"
WATCH_CAP = 5
LISTED_COMMITS = 20
TITLE_RE = re.compile(r"(feat|fix)(\((?P<scope>[^)]*)\))?!?:\s*\S", re.I)
SKIP_LABELS = {"sync-with-generator", "dependencies"}
SPEC_FILES = ("docs/openapiv2/apidocs.swagger.json", "openfga/v1/*.proto")
PIN_RE = re.compile(r"^OPEN_API_REF\s*\??=\s*([0-9a-f]{40})\s*$", re.M)
DRIFT_TITLE = "factory: API spec changed since the generator pin"


@dataclasses.dataclass
class Candidate:
    sdk: str
    upstream: str
    number: int
    title: str
    merged_at: dt.datetime
    change_id: str = ""

    @property
    def path(self) -> str:
        return f"{self.upstream}/pull/{self.number}"


def parse_time(text) -> dt.datetime | None:
    if not text:
        return None
    return dt.datetime.fromisoformat(str(text).replace("Z", "+00:00"))


def source_keys(text: str) -> set:
    """Comparable keys for a source, so a PR and its merge commit both match."""
    try:
        source = normalize_source(text)
    except FactoryError:
        return set()
    repo = source.repo.lower()
    if source.kind in ("pull", "issues", "short"):
        return {f"{repo}#{source.ref}"}
    if source.kind == "commit":
        return {f"{repo}@{source.ref[:7].lower()}"}
    return {f"{repo}/tree/{source.ref}"}


def labels_of(item: dict) -> set:
    return {label.get("name") for label in item.get("labels") or []}


def hub_issues(gh, hub: str, label: str, state: str = "all") -> list:
    items = gh.api(f"repos/{hub}/issues?state={state}&labels={label}&per_page=100", paginate=True) or []
    return [item for item in items if not item.get("pull_request")]


def known_sources(gh, hub: str) -> set:
    seen = set()
    for label in (CANDIDATE_LABEL, PROPAGATE_LABEL):
        for issue in hub_issues(gh, hub, label):
            seen |= source_keys(parse_sections(issue.get("body") or "").get("source", ""))
    return seen


def pack_ids(root: Path = ACCEPTANCE_DIR) -> dict:
    """Map source keys to the change id of the acceptance pack written for them."""
    ids = {}
    for path in sorted(root.glob("*/pack.yaml")):
        if CHANGE_ID_RE.fullmatch(path.parent.name):
            for key in source_keys(str(load_yaml(path).get("source") or "")):
                ids[key] = path.parent.name
    return ids


def wanted(pr: dict, since: dt.datetime) -> bool:
    merged = parse_time(pr.get("merged_at"))
    if not merged or merged < since:
        return False
    title = TITLE_RE.match(pr.get("title") or "")
    if not title or (title.group("scope") or "").strip().lower().startswith("deps"):
        return False
    user = pr.get("user") or {}
    if user.get("type") == "Bot" or (user.get("login") or "").endswith("[bot]"):
        return False
    if labels_of(pr) & SKIP_LABELS or ((pr.get("head") or {}).get("ref") or "").startswith("sync/"):
        return False
    return "SDK-Factory-" not in (pr.get("body") or "")


def clean_title(text: str, limit: int = 100) -> str:
    """An issue title without links or mentions. Titles are not Markdown, so code spans do not help."""
    text = re.sub(r"https?://\S+", "", text or "")
    text = re.sub(r"\(\s*#\d+\s*\)|#\d+|[@`\r\n]", "", text)
    return re.sub(r"\s+", " ", text).strip()[:limit].rstrip()


def candidate_body(found: Candidate) -> str:
    pack = code(f"factory/acceptance/{found.change_id}/") if found.change_id else "None"
    lines = [
        f"The factory watcher found {code(found.path)}, a {found.sdk} change merged on "
        f"{found.merged_at:%Y-%m-%d}, and no factory issue for it yet.",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| SDK | {found.sdk} |",
        f"| Title | {code(found.title)} |",
        f"| Acceptance pack | {pack} |",
        "",
        "Read the change first. To port it to the other SDKs, check the form below and fill in the kind "
        "of change, then add the `factory:propagate` label. Close this issue if the change does not need porting.",
        "",
    ]
    return "\n".join(lines) + "\n" + issue_form(LANES["B"], code(found.path), found.change_id)


def watch(
    gh,
    registry: dict,
    hub: str,
    *,
    days: int = 2,
    dry_run: bool = False,
    now: dt.datetime | None = None,
    acceptance_root: Path = ACCEPTANCE_DIR,
    cap: int = WATCH_CAP,
) -> int:
    since = (now or dt.datetime.now(dt.timezone.utc)) - dt.timedelta(days=days)
    seen, ids = known_sources(gh, hub), pack_ids(acceptance_root)
    found, rows, failed = [], [], False
    for sdk, entry in registry["sdks"].items():
        upstream = entry["upstream"]
        try:
            pulls = gh.api(f"repos/{upstream}/pulls?state=closed&sort=updated&direction=desc&per_page=50") or []
        except ApiError as err:
            rows.append(f"| {sdk} | could not list pull requests (HTTP {err.status or '?'}) |")
            failed = True
            continue
        new = 0
        for pr in pulls:
            if not wanted(pr, since):
                continue
            keys = {f"{upstream.lower()}#{pr['number']}"}
            if pr.get("merge_commit_sha"):
                keys.add(f"{upstream.lower()}@{pr['merge_commit_sha'][:7].lower()}")
            if keys & seen:
                continue
            change_id = next((ids[key] for key in sorted(keys) if key in ids), "")
            found.append(Candidate(sdk, upstream, pr["number"], pr["title"], parse_time(pr["merged_at"]), change_id))
            new += 1
        rows.append(f"| {sdk} | {new} |")

    found.sort(key=lambda item: item.merged_at)
    now_open, later = found[:cap], found[cap:]
    lines = [
        "### SDK factory watcher",
        "",
        f"Changes merged since {since:%Y-%m-%d %H:%M} UTC. Dry run: {'yes' if dry_run else 'no'}.",
        "",
        "| SDK | New changes |",
        "|---|---|",
        *rows,
        "",
    ]
    for item in now_open:
        title = f"factory: {item.sdk}: {clean_title(item.title)}"
        if dry_run:
            lines.append(f"- Would open {code(title)} for {code(item.path)}")
            continue
        try:
            issue = gh.api(
                f"repos/{hub}/issues",
                method="POST",
                fields=[("title", title), ("body", candidate_body(item)), ("labels[]", CANDIDATE_LABEL)],
            )
            lines.append(f"- Opened [#{issue['number']}]({SERVER}/{hub}/issues/{issue['number']}) for {code(item.path)}")
        except ApiError as err:
            lines.append(f"- Could not open an issue for {code(item.path)} (HTTP {err.status or '?'})")
            failed = True
    if not found:
        lines.append("Nothing new to port.")
    if later:
        lines.append(f"- {len(later)} more wait for the next run (at most {cap} issues a run, oldest first).")
    summary("\n".join(lines))
    return 1 if failed else 0


def read_pin(makefile: Path) -> str:
    found = PIN_RE.search(makefile.read_text(encoding="utf-8"))
    if not found:
        raise FactoryError(f"No 40-character `OPEN_API_REF` pin in {code(makefile.name)}.")
    return found.group(1)


def is_spec_file(name: str) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in SPEC_FILES)


def drift_body(upstream: str, pin: str, head: str, ahead: int, files: list, commits: list) -> str:
    listed = [f"- {code(subject)}" for subject in commits[:LISTED_COMMITS]]
    if len(commits) > LISTED_COMMITS:
        listed.append(f"- and {len(commits) - LISTED_COMMITS} more")
    lines = [
        f"API main is {ahead} commit{'s' if ahead != 1 else ''} ahead of the `OPEN_API_REF` pin in the "
        "generator Makefile, and the spec changed.",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| API | {code(upstream)} |",
        f"| Pinned commit | {code(pin[:7])} |",
        f"| API main | {code(head[:7])} |",
        f"| Spec files changed | {', '.join(code(name) for name in files)} |",
        "",
        "Commits since the pin:",
        "",
        *listed,
        "",
        "To regenerate the SDKs against API main, read the changes and fill in the kind of change below, "
        "then add the `factory:propagate` label. "
        "After the sync PRs merge, set `OPEN_API_REF` in the Makefile to the same commit. The next scan then "
        "closes this issue.",
        "",
    ]
    return "\n".join(lines) + "\n" + issue_form(LANES["A"], code(f"{upstream}/commit/{head}"), f"api-{head[:7]}")


def routed_commit(issue: dict) -> str:
    try:
        source = normalize_source(parse_sections(issue.get("body") or "").get("source", ""))
    except FactoryError:
        return ""
    return source.ref.lower() if source.kind == "commit" else ""


def close(gh, hub: str, issue: dict, reason: str) -> None:
    number = issue["number"]
    gh.api(f"repos/{hub}/issues/{number}/comments", method="POST", fields=[("body", reason)])
    gh.api(f"repos/{hub}/issues/{number}", method="PATCH", fields=[("state", "closed"), ("state_reason", "completed")])


def drift(gh, registry: dict, hub: str, *, makefile: Path = ROOT / "Makefile") -> int:
    upstream = registry["api"]["upstream"]
    pin = read_pin(makefile)
    head = gh.api(f"repos/{upstream}/git/ref/heads/main")["object"]["sha"]
    files, commits, ahead = [], [], 0
    if head != pin:
        compare = gh.api(f"repos/{upstream}/compare/{pin}...{head}") or {}
        ahead = int(compare.get("ahead_by") or 0)
        files = sorted(item["filename"] for item in compare.get("files") or [] if is_spec_file(item["filename"]))
        commits = [first_line(item["commit"]["message"]) for item in compare.get("commits") or []]

    issues = hub_issues(gh, hub, DRIFT_LABEL, state="open")
    lines = ["### SDK factory API drift", "", f"Pin {code(pin[:7])}, API main {code(head[:7])}.", ""]
    if not files:
        for issue in issues:
            close(gh, hub, issue, f"The generator pin {code(pin[:7])} now has the current API spec. Closing.\n")
            lines.append(f"- Closed #{issue['number']}.")
        lines.append("No spec drift.")
        summary("\n".join(lines))
        return 0

    body = drift_body(upstream, pin, head, ahead, files, commits)
    current = None
    for issue in issues:
        routed = routed_commit(issue)
        if PROPAGATE_LABEL in labels_of(issue) and routed and pin.startswith(routed):
            close(gh, hub, issue, f"The generator pin now includes {code(routed[:7])}. A new issue tracks the rest.\n")
            lines.append(f"- Closed #{issue['number']}: its change is pinned.")
        elif current is None:
            current = issue
    if current is None:
        issue = gh.api(
            f"repos/{hub}/issues",
            method="POST",
            fields=[("title", DRIFT_TITLE), ("body", body), ("labels[]", DRIFT_LABEL)],
        )
        lines.append(f"- Opened #{issue['number']}.")
    elif PROPAGATE_LABEL in labels_of(current):
        lines.append(f"- Left #{current['number']} alone: the factory already started it.")
    elif (current.get("body") or "").strip() == body.strip():
        lines.append(f"- #{current['number']} is up to date.")
    else:
        gh.api(f"repos/{hub}/issues/{current['number']}", method="PATCH", fields=[("body", body)])
        lines.append(f"- Updated #{current['number']}.")
    summary("\n".join(lines))
    return 0


def sync(gh, registry: dict, hub: str, token: str) -> int:
    owner = hub.split("/")[0]
    pairs = [
        (registry["generator"]["upstream"], hub),
        (registry["api"]["upstream"], f"{owner}/{registry['api']['repo']}"),
    ]
    pairs += [(entry["upstream"], fork_name(registry, owner, key)) for key, entry in registry["sdks"].items()]
    rows, failed = [], False
    for upstream, fork in pairs:
        status, ok = fork_sync(gh, upstream, fork, token, apply=bool(token))
        rows.append(f"| [{fork}]({SERVER}/{fork}) | {code(upstream)} | {status} |")
        failed = failed or (bool(token) and not ok)
    lines = ["### SDK factory fork sync", ""]
    if not token:
        lines += ["`FACTORY_TOKEN` is not set, so this run only reports how far behind each fork is.", ""]
    lines += ["| Fork | Upstream | Status |", "|---|---|---|", *rows]
    summary("\n".join(lines))
    return 1 if failed else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Scheduled scans for the SDK factory.")
    commands = parser.add_subparsers(dest="command", required=True)
    watcher = commands.add_parser("watch", help="open candidate issues for new SDK changes")
    watcher.add_argument("--days", type=int, default=2)
    watcher.add_argument("--dry-run", default="false")
    commands.add_parser("drift", help="track API spec changes the generator pin does not have")
    commands.add_parser("sync", help="sync the hub and every fork with upstream")
    args = parser.parse_args(argv)

    gh, registry, hub = GitHub(), load_registry(), os.environ["GITHUB_REPOSITORY"]
    try:
        if args.command == "watch":
            if not 1 <= args.days <= 30:
                raise FactoryError("Use a window of 1 to 30 days.")
            return watch(gh, registry, hub, days=args.days, dry_run=args.dry_run.lower() == "true")
        if args.command == "drift":
            return drift(gh, registry, hub)
        return sync(gh, registry, hub, os.environ.get("FACTORY_TOKEN", ""))
    except FactoryError as err:
        for problem in err.problems:
            print(f"::error::{problem}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
