#!/usr/bin/env python3
"""Route one `factory:propagate` issue.

Reads the issue form, resolves the source, syncs the target forks, starts the
lane's workflows in this repository and posts one tracking comment. Nothing
here calls a model. The only agentic step is the port worker it may start.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from factory_lib import (  # noqa: E402
    ACCEPTANCE_DIR,
    BOT_LOGIN,
    CHANGE_ID_RE,
    KINDS,
    LANES,
    ORIGIN_TRAILER,
    SERVER,
    SHA_RE,
    ApiError,
    FactoryError,
    GitHub,
    Source,
    acceptance_runners,
    code,
    first_line,
    fork_name,
    fork_sync,
    load_registry,
    normalize_source,
    parse_sections,
    sdk_for_repo,
    slug,
)

PORT_WORKFLOW = "factory-port.lock.yml"
SYNC_WORKFLOW = "sync-sdks.yaml"
API_VERSION = "2026-03-10"
TRACKING_MARKER = "<!-- factory:tracking mode={} -->"
ERROR_MARKER = "<!-- factory:route-error -->"
NOTES_LIMIT = 4000
LANE_KINDS = {
    "A": ("pull", "commit", "tree"),
    "B": ("pull", "commit", "issues"),
    "C": ("pull", "commit", "tree"),
}
LANE_SOURCES = {
    "A": "an API pull request, commit or `tree/<ref>` path",
    "B": "an SDK pull request, commit or issue",
    "C": "a generator pull request, commit or `tree/<ref>` path",
}
KIND_WORDS = {"pull": "a pull request", "commit": "a commit", "issues": "an issue", "tree": "a `tree/<ref>` path"}
# The SDK choices of sync-sdks.yaml. A run with sdk=all covers exactly these.
SYNC_SDKS = ("js", "go", "dotnet", "python", "java")
RETRY = "Edit the issue, then remove and re-add the `factory:propagate` label."
# Unmerged sources are untrusted input to jobs that hold FACTORY_TOKEN.
UNREVIEWED = {
    "A": "Its spec reaches generated code and the SDK tests, so only run the factory on changes you have read.",
    "C": "The sync job runs its templates and Makefile while `FACTORY_TOKEN` is available, so only run the factory on changes you have read.",
}


@dataclasses.dataclass
class Request:
    lane: str
    source: Source
    raw_source: str
    kind: str = ""
    change_id: str = ""
    targets: list = dataclasses.field(default_factory=list)
    plan_only: bool = False
    port_after: bool = False
    notes: str = ""


@dataclasses.dataclass
class Plan:
    lane: str
    source: Source
    kind: str = ""
    title: str = ""
    source_sdk: str | None = None
    sha: str = ""
    change_id: str = ""
    derived_id: bool = False
    targets: list = dataclasses.field(default_factory=list)
    openapi_ref: str = ""
    generator_repo: str = ""
    generator_ref: str = ""
    generator_label: str = ""
    sync_pr: bool = False
    head_repo: str = ""
    head_ref: str = ""
    port_after: bool = False
    # Lanes A and C: the SDKs whose sync PR gets a port worker. None means all of them.
    port_sdks: list | None = None
    # SDKs the reach left out of the default targets or the port workers.
    left_out: list = dataclasses.field(default_factory=list)
    warnings: list = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Row:
    sdk: str
    fork: str
    base: str = "main"
    sync: str = "not checked"
    ready: bool = True
    run: str = "not started"


def checked(text: str, label: str) -> bool:
    return bool(re.search(rf"^\s*-\s*\[[xX]\]\s*{re.escape(label)}", text or "", re.M))


def match_kind(text: str) -> str:
    """The KINDS key for a form answer, or "" when it names no kind."""
    text = text.strip().strip("`").strip().lower()
    for key, label in KINDS.items():
        if text == key or text.startswith(label.lower()):
            return key
    return ""


def parse_request(body: str, registry: dict) -> Request:
    sections = parse_sections(body)
    problems = []
    lane = sections.get("lane", "")[:1].upper()
    if lane not in LANES:
        problems.append("Pick a lane: A, B or C.")
    kind_text = sections.get("kind of change", "")
    kind = match_kind(kind_text)
    if kind_text and not kind:
        problems.append(f"Pick a kind of change: {', '.join(KINDS.values())}.")
    raw_source = sections.get("source", "")
    source = None
    try:
        source = normalize_source(raw_source)
    except FactoryError as err:
        problems += err.problems
    change_id = sections.get("change id", "").strip().strip("`").strip()
    if change_id and not CHANGE_ID_RE.fullmatch(change_id):
        problems.append(
            f"The change id {code(change_id)} must be kebab-case: lowercase letters, digits and dashes, 2 to 49 characters."
        )
    wanted, unknown = set(), []
    for item in re.split(r"[,\s]+", sections.get("target sdks", "")):
        item = item.strip().lower()
        if not item:
            continue
        if item in registry["sdks"]:
            wanted.add(item)
        else:
            unknown.append(item)
    if unknown:
        known = ", ".join(code(key) for key in registry["sdks"])
        problems.append(f"Unknown target SDKs: {', '.join(code(item) for item in unknown)}. Use {known}.")
    notes = sections.get("notes", "")
    if len(notes) > NOTES_LIMIT:
        problems.append(f"Keep the notes under {NOTES_LIMIT} characters.")
    if problems:
        raise FactoryError(*problems)
    options = sections.get("options", "")
    return Request(
        lane=lane,
        source=source,
        raw_source=raw_source,
        kind=kind,
        change_id=change_id,
        targets=[key for key in registry["sdks"] if key in wanted],
        plan_only=checked(options, "Plan only"),
        port_after=checked(options, "Lanes A and C"),
        notes=notes,
    )


def read(gh, path: str):
    try:
        return gh.api(path)
    except ApiError as err:
        raise FactoryError(f"Could not read {code(path)} (HTTP {err.status or '?'}).") from err


def resolve_short(gh, source: Source) -> Source:
    if source.kind != "short":
        return source
    issue = read(gh, f"repos/{source.repo}/issues/{source.ref}")
    return Source(source.repo, "pull" if issue.get("pull_request") else "issues", source.ref)


def resolve_commit(gh, plan: Plan) -> str:
    commit = read(gh, f"repos/{plan.source.repo}/commits/{plan.source.ref}")
    plan.sha = commit["sha"]
    plan.title = plan.title or first_line(commit["commit"]["message"])
    return commit["commit"]["message"] or ""


def on_main(gh, repo: str, sha: str) -> bool:
    try:
        data = gh.api(f"repos/{repo}/compare/{sha}...main?per_page=1")
    except ApiError:
        return False
    return (data or {}).get("status") in ("ahead", "identical")


def check_commit_on_main(gh, plan: Plan) -> None:
    if not on_main(gh, plan.source.repo, plan.sha):
        plan.warnings.append(
            f"Commit {code(plan.sha[:7])} is not on `main` of {code(plan.source.repo)}. {UNREVIEWED[plan.lane]}"
        )


def resolve_api(gh, registry: dict, owner: str, plan: Plan) -> None:
    upstream, fork = registry["api"]["upstream"], f"{owner}/{registry['api']['repo']}"
    if plan.source.repo.lower() not in (upstream.lower(), fork.lower()):
        raise FactoryError(f"Lane A needs a source in {code(upstream)} or {code(fork)}, not {code(plan.source.repo)}.")
    if plan.source.kind == "pull":
        pr = read(gh, f"repos/{plan.source.repo}/pulls/{plan.source.ref}")
        plan.title = pr["title"]
        if pr.get("merged_at"):
            plan.sha = plan.openapi_ref = pr["merge_commit_sha"]
        elif pr["state"] == "open":
            plan.sha = plan.openapi_ref = pr["head"]["sha"]
            plan.warnings.append(
                f"The API pull request is still open, so the SDKs are generated from its current head commit. {UNREVIEWED['A']}"
            )
        else:
            raise FactoryError("The API pull request was closed without merging.")
    else:
        resolve_commit(gh, plan)
        plan.openapi_ref = plan.sha
        check_commit_on_main(gh, plan)


def resolve_generator(gh, registry: dict, hub: str, plan: Plan) -> None:
    upstream = registry["generator"]["upstream"]
    if plan.source.repo.lower() not in (upstream.lower(), hub.lower()):
        raise FactoryError(f"Lane C needs a source in {code(upstream)} or {code(hub)}, not {code(plan.source.repo)}.")
    plan.generator_repo = plan.source.repo
    if plan.source.kind == "pull":
        pr = read(gh, f"repos/{plan.source.repo}/pulls/{plan.source.ref}")
        plan.title = pr["title"]
        if pr.get("merged_at"):
            plan.sha = plan.generator_ref = pr["merge_commit_sha"]
        elif pr["state"] == "open":
            plan.sha = pr["head"]["sha"]
            plan.generator_ref = f"refs/pull/{plan.source.ref}/head"
            plan.generator_label = f"refs/pull/{plan.source.ref}/head at {plan.sha[:7]}"
            plan.warnings.append(f"The generator pull request is still open. {UNREVIEWED['C']}")
        else:
            raise FactoryError("The generator pull request was closed without merging.")
    else:
        resolve_commit(gh, plan)
        plan.generator_ref = plan.sha
        check_commit_on_main(gh, plan)


def resolve_sdk_change(gh, registry: dict, owner: str, plan: Plan) -> None:
    source = plan.source
    plan.source_sdk = sdk_for_repo(registry, owner, source.repo)
    body = ""
    if source.kind == "issues":
        issue = read(gh, f"repos/{source.repo}/issues/{source.ref}")
        if issue.get("pull_request"):
            source = plan.source = Source(source.repo, "pull", source.ref)
        else:
            plan.title, body = issue["title"], issue.get("body") or ""
    if source.kind == "pull":
        pr = read(gh, f"repos/{source.repo}/pulls/{source.ref}")
        plan.title, body = pr["title"], pr.get("body") or ""
        labels = {label["name"] for label in pr.get("labels") or []}
        head_ref = pr["head"]["ref"]
        plan.sync_pr = (
            plan.title.startswith("chore(sync)") or "sync-with-generator" in labels or head_ref.startswith("sync/")
        )
        if pr["state"] == "open":
            plan.head_repo = ((pr["head"].get("repo") or {}).get("full_name")) or ""
            plan.head_ref = head_ref
        elif not pr.get("merged_at"):
            plan.warnings.append("The source pull request was closed without merging.")
    elif source.kind == "commit":
        body = resolve_commit(gh, plan)
        plan.source = Source(source.repo, "commit", plan.sha)
    if ORIGIN_TRAILER in body:
        raise FactoryError("The source is itself a factory change. Use the original change as the source.")
    if plan.source_sdk is None:
        plan.warnings.append(f"{code(source.repo)} is not an SDK in the registry, so every SDK is a target.")


def derive_change_id(plan: Plan, number: int) -> str:
    source = plan.source
    prefix = {"A": "api", "C": "gen"}.get(plan.lane, source.repo.split("/")[1])
    if source.kind == "pull":
        tail = f"pr-{source.ref}"
    elif source.kind == "issues":
        tail = f"issue-{source.ref}"
    elif source.kind == "tree" and source.ref not in ("main", "master") and not SHA_RE.fullmatch(source.ref):
        tail = source.ref
    else:
        tail = plan.sha[:7]
    change_id = slug(f"{prefix}-{tail}")
    return change_id if CHANGE_ID_RE.fullmatch(change_id) else f"factory-{number}"


def pick_targets(registry: dict, req: Request, plan: Plan) -> list:
    every = list(registry["sdks"])
    if req.targets:
        return req.targets
    if plan.lane in ("A", "C"):
        return every
    if plan.sync_pr and plan.source_sdk:
        return [plan.source_sdk]
    if plan.source.kind == "issues" or not plan.source_sdk:
        return every
    return [key for key in every if key != plan.source_sdk]


def load_reach(registry: dict, override: str = "") -> tuple:
    """Kind of change -> SDK keys in registry order, plus warnings.

    `reach` in sdks.yaml is the default. The FACTORY_REACH variable, a JSON
    object in the same shape, wins per kind. An invalid value for a kind falls
    back to sdks.yaml. Kinds without a valid entry reach every SDK."""
    every = list(registry["sdks"])
    default, extra, warnings = registry.get("reach") or {}, {}, []
    if override.strip():
        try:
            extra = json.loads(override)
        except ValueError:
            extra = None
        if not isinstance(extra, dict):
            warnings.append("The `FACTORY_REACH` variable is not a JSON object, so the factory ignored it.")
            extra = {}
    reach = {}
    for kind in dict.fromkeys([*default, *extra]):
        if kind not in KINDS:
            warnings.append(f"The reach names an unknown kind of change, {code(kind)}, so the factory ignored it.")
            continue
        for source, values in (("`FACTORY_REACH`", extra), ("`sdks.yaml`", default)):
            if kind in values:
                keys = reach_keys(kind, values[kind], every, source, warnings)
                if keys is not None:
                    reach[kind] = keys
                    break
    return reach, warnings


def reach_keys(kind: str, value, every: list, source: str, warnings: list) -> list | None:
    """SDK keys in registry order for one reach value, or None when the value is invalid."""
    if value == "all":
        return every
    if not isinstance(value, list):
        warnings.append(f"The reach for {code(kind)} in {source} is not `all` or a list of SDKs, so the factory ignored it.")
        return None
    unknown = [str(item) for item in value if item not in every]
    if unknown:
        warnings.append(f"The reach for {code(kind)} names unknown SDKs: {', '.join(code(item) for item in unknown)}.")
    return [key for key in every if key in value]


def names_text(keys: list) -> str:
    items = [code(key) for key in keys]
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def apply_reach(plan: Plan, req: Request, reach: dict, every: list) -> None:
    """Narrow the default targets to the reach of the kind of change.

    Lane B drops SDKs outside the reach. Lanes A and C still sync every target,
    because generated code covers all APIs, and only skip the port worker.
    SDKs picked under Target SDKs, or by a sync PR source, always run."""
    narrowed = any(len(sdks) < len(every) for sdks in reach.values())
    picked = bool(req.targets) or bool(plan.sync_pr and plan.source_sdk)
    if not plan.kind:
        if narrowed and not picked and (plan.lane == "B" or plan.port_after):
            plan.warnings.append("No kind of change was picked, so the reach was not applied.")
        return
    allowed = reach.get(plan.kind, every)
    outside = [key for key in plan.targets if key not in allowed]
    if not outside:
        return
    label, names = KINDS[plan.kind], names_text(outside)
    if picked:
        plan.warnings.append(f"The {label} reach leaves out {names}, but this change picked them, so they run.")
        return
    if plan.lane == "B":
        plan.targets = [key for key in plan.targets if key in allowed]
        if not plan.targets:
            raise FactoryError(
                f"The {label} reach covers no SDK besides the source. Pick Target SDKs to run it anyway."
            )
        plan.left_out = outside
    elif plan.port_after:
        plan.port_sdks = [key for key in plan.targets if key in allowed]
        plan.left_out = outside


def resolve(gh, registry: dict, hub: str, req: Request, number: int, reach: dict | None = None) -> Plan:
    owner = hub.split("/")[0]
    plan = Plan(lane=req.lane, source=resolve_short(gh, req.source), kind=req.kind, port_after=req.port_after)
    if plan.source.kind not in LANE_KINDS[req.lane]:
        raise FactoryError(
            f"Lane {req.lane} needs {LANE_SOURCES[req.lane]} as the source, not {KIND_WORDS[plan.source.kind]}."
        )
    external = plan.source.repo.split("/")[0].lower() != owner.lower()
    if external and (req.source.kind == "short" or re.search(r"github\.com/", req.raw_source, re.I)):
        plan.warnings.append(
            "The source was written as a link or an `owner/repo#N` reference, which adds a mention to that thread. "
            "Write it as a path, like `openfga/js-sdk/pull/228`, next time."
        )
    if req.lane == "A":
        resolve_api(gh, registry, owner, plan)
    elif req.lane == "C":
        resolve_generator(gh, registry, hub, plan)
    else:
        resolve_sdk_change(gh, registry, owner, plan)
    plan.change_id = req.change_id or derive_change_id(plan, number)
    plan.derived_id = not req.change_id
    plan.targets = pick_targets(registry, req, plan)
    apply_reach(plan, req, reach or {}, list(registry["sdks"]))
    return plan


def dispatch(gh, hub: str, workflow: str, ref: str, inputs: dict) -> str:
    path = f"repos/{hub}/actions/workflows/{workflow}/dispatches"
    fields = [("ref", ref)] + [(f"inputs[{key}]", value) for key, value in inputs.items()]
    try:
        data = gh.api(path, method="POST", fields=fields, headers={"X-GitHub-Api-Version": API_VERSION})
    except ApiError as err:
        if err.status != 400:
            raise
        data = gh.api(path, method="POST", fields=fields)
    return (data or {}).get("html_url") or f"{SERVER}/{hub}/actions/workflows/{workflow}"


def acceptance_text(change_id: str, acceptance_root: Path) -> str:
    runners = acceptance_runners(change_id, acceptance_root)
    if runners is None:
        return f"None. Add `factory/acceptance/{change_id}/` to give the verify job a fail-before, pass-after test."
    return f"`factory/acceptance/{change_id}/` with runners for {', '.join(runners) or 'no SDKs'}"


def render(plan: Plan, mode: str, rows: list, hub_status: str, pack: str, setup: list, plan_only: bool) -> str:
    lines = [TRACKING_MARKER.format(mode), f"### SDK factory {'plan' if mode == 'plan' else 'run'}", ""]
    source_text = code(plan.source.path) + (f" ({plan.source_sdk})" if plan.source_sdk else "")
    change_text = code(plan.change_id) + (" (derived from the source)" if plan.derived_id else "")
    fields = [
        ("Lane", LANES[plan.lane]),
        ("Kind of change", KINDS[plan.kind] if plan.kind else "Not set"),
        ("Source", source_text),
        ("Title", code(plan.title) or "None"),
        ("Change id", change_text),
    ]
    if plan.lane == "A":
        fields.append(("API ref", code(plan.openapi_ref)))
    if plan.lane == "C":
        fields.append(("Generator", f"{code(plan.generator_repo)} at {code(plan.generator_label or plan.generator_ref)}"))
    if plan.lane in ("A", "C"):
        fields.append(("Port worker after sync", "yes" if plan.port_after else "no"))
    fields.append(("Acceptance pack", pack))
    if hub_status:
        fields.append(("Hub sync", hub_status))
    lines += ["| Field | Value |", "|---|---|"] + [f"| {key} | {value} |" for key, value in fields] + [""]

    if plan.lane == "B":
        lines += ["| SDK | Fork | Base | Fork sync | Worker |", "|---|---|---|---|---|"]
        lines += [
            f"| {row.sdk} | [{row.fork}]({SERVER}/{row.fork}) | {code(row.base)} | {row.sync} | {row.run} |"
            for row in rows
        ]
    else:
        lines += ["| SDK | Fork | Fork sync | Sync run |", "|---|---|---|---|"]
        lines += [f"| {row.sdk} | [{row.fork}]({SERVER}/{row.fork}) | {row.sync} | {row.run} |" for row in rows]
    lines.append("")
    if plan.left_out and (plan.lane == "B" or plan.port_after):
        label, names = KINDS[plan.kind], names_text(plan.left_out)
        if plan.lane == "B":
            lines.append(f"The {label} reach leaves out {names}. Pick them under **Target SDKs** to include them.")
        else:
            lines.append(f"The {label} reach leaves out {names}, so their sync PRs get no port worker.")
        lines.append("")

    if mode == "plan" and plan_only:
        lines.append(
            "Plan only: no forks were synced and nothing was started. To run it, clear **Plan only**, then remove and re-add the `factory:propagate` label."
        )
        if setup:
            lines += ["", "Setup needed first:"] + [f"- {item}" for item in setup]
    elif mode == "plan":
        lines.append("Setup needed before the factory can run:")
        lines += [f"- {item}" for item in setup]
        lines += ["", "Then remove and re-add the `factory:propagate` label."]
    elif plan.lane == "B":
        lines.append(
            "Each worker opens a draft PR labelled `factory` in its fork and reports here. The verify job then runs the SDK checks and the acceptance pack on that PR."
        )
    else:
        text = f"Each sync run opens a draft PR from `sync/factory-{plan.change_id}` in its fork."
        if plan.port_after:
            text += " A port worker then adds the hand-written layer in a stacked draft PR."
        lines.append(text + " The verify job runs the SDK checks on every factory PR.")
    if plan.warnings:
        lines += ["", "**Warnings**", ""] + [f"- {item}" for item in plan.warnings]
    return "\n".join(lines) + "\n"


def render_error(problems: list) -> str:
    lines = [ERROR_MARKER, "### SDK factory could not start", ""]
    lines += [f"- {problem}" for problem in problems]
    lines += ["", RETRY]
    return "\n".join(lines) + "\n"


def post(gh, hub: str, number: int, body: str) -> None:
    gh.api(f"repos/{hub}/issues/{number}/comments", method="POST", fields=[("body", body)])
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(body + "\n")


def already_dispatched(gh, hub: str, number: int) -> bool:
    marker = TRACKING_MARKER.format("dispatched")
    comments = gh.api(f"repos/{hub}/issues/{number}/comments?per_page=100", paginate=True) or []
    return any(
        (comment.get("user") or {}).get("login") == BOT_LOGIN and marker in (comment.get("body") or "")
        for comment in comments
    )


def run(
    gh,
    registry: dict,
    hub: str,
    number: int,
    *,
    factory_token: str = "",
    has_copilot: bool = False,
    force: bool = False,
    acceptance_root: Path = ACCEPTANCE_DIR,
    reach_override: str = "",
) -> int:
    issue = gh.api(f"repos/{hub}/issues/{number}")
    reach, reach_warnings = load_reach(registry, reach_override)
    try:
        req = parse_request(issue.get("body") or "", registry)
        plan = resolve(gh, registry, hub, req, number, reach)
    except FactoryError as err:
        post(gh, hub, number, render_error(err.problems))
        return 1
    plan.warnings += reach_warnings

    setup = []
    if not factory_token:
        setup.append("Add the `FACTORY_TOKEN` secret so the factory can sync forks and push branches.")
    if not has_copilot:
        if plan.lane == "B":
            setup.append("Add the `COPILOT_GITHUB_TOKEN` secret so the port worker can run.")
        elif plan.port_after:
            plan.port_after = False
            plan.warnings.append("Port workers need the `COPILOT_GITHUB_TOKEN` secret, so this run only opens sync PRs.")
    mode = "plan" if req.plan_only or setup else "dispatched"

    if mode == "dispatched" and not force and already_dispatched(gh, hub, number):
        post(
            gh,
            hub,
            number,
            "The factory already started this change. To start it again, run the `SDK factory router` workflow with this issue number and **force** set.\n",
        )
        return 0

    # In plan mode this only reads how far behind each fork is.
    owner, apply = hub.split("/")[0], mode == "dispatched"
    hub_status, _ = fork_sync(gh, registry["generator"]["upstream"], hub, factory_token, apply=apply)
    rows = [Row(sdk=key, fork=fork_name(registry, owner, key)) for key in plan.targets]
    for row in rows:
        upstream = registry["sdks"][row.sdk]["upstream"]
        row.sync, row.ready = fork_sync(gh, upstream, row.fork, factory_token, apply=apply)
        if plan.lane == "B" and plan.head_ref and plan.head_repo.lower() == row.fork.lower():
            row.base = plan.head_ref

    failed = False
    if mode == "dispatched":
        ref = (gh.api(f"repos/{hub}") or {}).get("default_branch") or "main"
        failed = start(gh, hub, ref, number, plan, req, rows)

    pack = acceptance_text(plan.change_id, acceptance_root)
    post(gh, hub, number, render(plan, mode, rows, hub_status, pack, setup, req.plan_only))
    return 1 if failed else 0


def start(gh, hub: str, ref: str, number: int, plan: Plan, req: Request, rows: list) -> bool:
    """Dispatch the lane's workflows. Returns True when a dispatch failed."""
    failed = False
    for row in rows:
        if not row.ready:
            row.run = "skipped: fork not in sync"
    ready = [row for row in rows if row.ready]
    if plan.lane == "B":
        for row in ready:
            inputs = {
                "sdk": row.sdk,
                "target_repo": row.fork,
                "source": plan.source.path,
                "change_id": plan.change_id,
                "base_branch": row.base,
                "tracking_issue": str(number),
            }
            if req.notes:
                inputs["notes"] = req.notes
            try:
                row.run = f"[started]({dispatch(gh, hub, PORT_WORKFLOW, ref, inputs)})"
            except ApiError as err:
                row.run, failed = f"dispatch failed (HTTP {err.status or '?'})", True
        return failed

    inputs = {"change_id": plan.change_id, "tracking_issue": str(number)}
    if plan.lane == "A":
        inputs["openapi_ref"] = plan.openapi_ref
    else:
        inputs["generator_repo"] = plan.generator_repo
        inputs["generator_ref"] = plan.generator_ref
    # One sync run covers every SDK only when they all agree on the port worker.
    ports = {row.sdk: plan.port_after and (plan.port_sdks is None or row.sdk in plan.port_sdks) for row in rows}
    every = (
        len(ready) == len(rows)
        and {row.sdk for row in rows} == set(SYNC_SDKS)
        and len(set(ports.values())) == 1
    )
    groups = [("all", ready)] if every else [(row.sdk, [row]) for row in ready]
    for sdk, members in groups:
        port = ports[members[0].sdk]
        run_inputs = {"sdk": sdk, **inputs, "port_after": "true" if port else "false"}
        try:
            link = f"[started]({dispatch(gh, hub, SYNC_WORKFLOW, ref, run_inputs)})"
        except ApiError as err:
            link, failed = f"dispatch failed (HTTP {err.status or '?'})", True
        for row in members:
            row.run = link if port or not plan.port_after else f"{link}, no port worker"
    return failed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Route one factory:propagate issue.")
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("--force", default="false")
    args = parser.parse_args(argv)
    return run(
        GitHub(),
        load_registry(),
        os.environ["GITHUB_REPOSITORY"],
        args.issue,
        factory_token=os.environ.get("FACTORY_TOKEN", ""),
        has_copilot=os.environ.get("HAS_COPILOT_TOKEN", "") == "true",
        force=args.force.lower() == "true",
        reach_override=os.environ.get("FACTORY_REACH", ""),
    )


if __name__ == "__main__":
    sys.exit(main())
