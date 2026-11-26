#!/usr/bin/env python3
"""Verify the factory's pull requests in the SDK forks.

  find    List open factory PRs whose head has no verify result yet, as a job
          matrix. Runs no PR code.
  run     Run one PR's install, checks and acceptance pack on its head, and on
          its base where that tells us something. This runs the PR's code, so
          the job gets no secrets.
  report  Post each result to its PR and its tracking issue. Reads the run
          artifacts as untrusted data.

Results are advisory. The PR's code runs in the same job as the checks, so it
can change what that job reports. A maintainer still reviews every PR.
"""

from __future__ import annotations

import argparse
import fnmatch
import html
import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from factory_lib import (  # noqa: E402
    ACCEPTANCE_DIR,
    BRANCH_RE,
    CHANGE_ID_RE,
    CHANGE_TRAILER,
    SERVER,
    ApiError,
    FactoryError,
    GitHub,
    acceptance_runners,
    code,
    fork_name,
    load_pack,
    load_registry,
    normalize_source,
    sdk_for_repo,
    summary,
)

FACTORY_LABEL = "factory"
LABELS = {
    "verified": "factory:verified",
    "checks-passed": "factory:checks-passed",
    "needs-human": "factory:needs-human",
    "verify-failed": "factory:verify-failed",
}
HEAD_PREFIXES = ("sdk-factory/", "sync/factory-")
TRUSTED = {"OWNER", "MEMBER", "COLLABORATOR"}
MARKER = "<!-- factory:verify head={sha} -->"
MARKER_RE = re.compile(r"<!-- factory:verify head=([0-9a-f]{40}) -->")
FULL_SHA = re.compile(r"[0-9a-f]{40}")
CAP = 10
TIMEOUTS = {"install": 20 * 60, "check": 30 * 60, "acceptance": 20 * 60}
KINDS = tuple(TIMEOUTS)
SIDES = ("head", "base")
TAIL_LINES, TAIL_CHARS, LOG_LINES = 60, 4000, 400
LIST_CAP, PATH_CHARS = 20, 200
COMMENT_LIMIT = 60000
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


# find ------------------------------------------------------------------------


def own_head(pr: dict, fork: str) -> bool:
    """The PR's branch lives in the fork itself, so only the fork's owner wrote it."""
    repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name") or ""
    return repo.lower() == fork.lower()


def factory_pull(pr: dict, fork: str) -> bool:
    labels = {label.get("name") for label in pr.get("labels") or []}
    ref = str((pr.get("head") or {}).get("ref") or "")
    return pr.get("state") == "open" and own_head(pr, fork) and ref.startswith(HEAD_PREFIXES) and FACTORY_LABEL in labels


def change_id_of(pr: dict) -> str:
    """The change id from the PR body trailer, else from the factory branch name."""
    candidates = [found.group(1) for found in re.finditer(rf"^{CHANGE_TRAILER}\s*`?([^`\s]+)`?\s*$", pr.get("body") or "", re.M)]
    ref = str(pr["head"]["ref"])
    if ref.startswith("sync/factory-"):
        candidates.append(ref[len("sync/factory-"):])
    elif ref.startswith("sdk-factory/"):
        # gh-aw appends a random 16 hex salt to the branch the worker asked for.
        candidates.append(re.sub(r"-[0-9a-f]{16}$", "", ref[len("sdk-factory/"):]))
    return next((value for value in candidates if CHANGE_ID_RE.fullmatch(value)), "")


def tracking_of(pr: dict, hub: str) -> str:
    found = re.search(rf"^Tracking:\s*`?{re.escape(hub)}#([0-9]+)`?\s*$", pr.get("body") or "", re.M | re.I)
    return found.group(1) if found else ""


def verified_heads(gh, fork: str, number: int) -> set:
    try:
        comments = gh.api(f"repos/{fork}/issues/{number}/comments?per_page=100", paginate=True) or []
    except ApiError:
        return set()
    # Only a marker on the first line counts. A result comment quotes PR output further down.
    found = (MARKER_RE.match((comment.get("body") or "").lstrip()) for comment in comments if comment.get("author_association") in TRUSTED)
    return {match.group(1) for match in found if match}


def one_pull(gh, registry: dict, owner: str, raw: str) -> tuple:
    source = normalize_source(raw)
    sdk = sdk_for_repo(registry, owner, source.repo)
    fork = fork_name(registry, owner, sdk) if sdk else ""
    if source.kind not in ("pull", "short") or source.repo.lower() != fork.lower():
        example = f"{owner}/{next(iter(registry['sdks'].values()))['repo']}/pull/5"
        raise FactoryError(f"Give a pull request in one of the SDK forks, for example {code(example)}.")
    pr = gh.api(f"repos/{fork}/pulls/{source.ref}")
    if pr.get("state") != "open":
        raise FactoryError("The pull request is not open.")
    if not own_head(pr, fork):
        raise FactoryError("The pull request's branch is not in the fork, so the verify job will not run its code.")
    return sdk, fork, pr


def find(gh, registry: dict, hub: str, *, pr_path: str = "", force: bool = False, cap: int = CAP) -> list:
    """Matrix entries for the PRs to verify, most recently updated first."""
    owner = hub.split("/")[0]
    if pr_path:
        targets = [one_pull(gh, registry, owner, pr_path)]
    else:
        targets = []
        for sdk in registry["sdks"]:
            fork = fork_name(registry, owner, sdk)
            try:
                pulls = gh.api(f"repos/{fork}/pulls?state=open&per_page=100", paginate=True) or []
            except ApiError as err:
                print(f"::warning::Cannot list the pull requests of {fork} (HTTP {err.status or '?'}).")
                continue
            targets += [(sdk, fork, pr) for pr in pulls if factory_pull(pr, fork)]
    targets.sort(key=lambda target: target[2].get("updated_at") or "", reverse=True)

    entries, rows = [], []
    for sdk, fork, pr in targets:
        head, base = pr["head"], pr["base"]
        item = {
            "sdk": sdk,
            "repo": fork,
            "pr": int(pr["number"]),
            "head_sha": str(head.get("sha") or ""),
            "base_ref": str(base.get("ref") or ""),
            "base_sha": str(base.get("sha") or ""),
            "change_id": change_id_of(pr),
            "tracking": tracking_of(pr, hub),
        }
        if not (FULL_SHA.fullmatch(item["head_sha"]) and FULL_SHA.fullmatch(item["base_sha"]) and BRANCH_RE.fullmatch(item["base_ref"])):
            state = "skipped: unexpected head or base"
        elif not force and item["head_sha"] in verified_heads(gh, fork, item["pr"]):
            state = "already verified"
        elif len(entries) >= cap:
            state = "waits for the next run"
        else:
            entries.append(item)
            state = "queued"
        link = f"[{fork}#{item['pr']}]({SERVER}/{fork}/pull/{item['pr']})"
        rows.append(f"| {sdk} | {link} | {code(item['change_id']) or 'none'} | {code(item['head_sha'][:7])} | {state} |")

    if rows:
        summary("\n".join(["### Factory pull requests", "", "| SDK | Pull request | Change | Head | State |", "|---|---|---|---|---|", *rows]))
    else:
        summary("### Factory pull requests\n\nNo open factory pull requests.")
    return entries


# run -------------------------------------------------------------------------


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def tail(text: str) -> str:
    return "\n".join(text.rstrip().splitlines()[-TAIL_LINES:])[-TAIL_CHARS:]


def ok(step: dict | None) -> bool:
    return step is not None and step["exit"] == 0 and not step["timed_out"]


def config_file(path: str, patterns: list) -> bool:
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def acceptance_env(entry: dict) -> dict:
    return {key: str(value) for key, value in (entry.get("acceptance_env") or {}).items() if re.fullmatch(r"[A-Z_][A-Z0-9_]*", key)}


def pack_info(change_id: str, sdk: str, root: Path = ACCEPTANCE_DIR) -> tuple:
    """(runner path or None, base failure pattern, fixed test path) for one SDK."""
    if sdk not in (acceptance_runners(change_id, root) or []):
        return None, "", ""
    pack = load_pack(change_id, root) or {}
    fixed = str((pack.get("fixed_paths") or {}).get(sdk) or "").strip("/")
    return root / change_id / sdk / "run.sh", str(pack.get("base_failure_pattern") or ""), fixed


class Recorder:
    """Runs commands and saves the result after every step, so a cancelled job still leaves one."""

    def __init__(self, result: dict, out: Path):
        self.result, self.out = result, out
        self.logs = out / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.save()

    def save(self) -> None:
        (self.out / "result.json").write_text(json.dumps(self.result, indent=2) + "\n", encoding="utf-8")

    def get(self, kind: str, index: int, side: str) -> dict | None:
        return next((s for s in self.result["steps"] if (s["kind"], s["index"], s["side"]) == (kind, index, side)), None)

    def step(self, kind: str, index: int, side: str, command: str, cwd: Path, env=None, pattern: str = "") -> dict:
        log = self.logs / f"{side}-{kind}-{index}.log"
        started = time.monotonic()
        with open(log, "wb") as handle:
            proc = subprocess.Popen(
                ["bash", "-c", command],
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                env={**os.environ, **(env or {})},
                start_new_session=True,
            )
            try:
                exit_code, timed_out = proc.wait(timeout=TIMEOUTS[kind]), False
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                exit_code, timed_out = proc.wait(), True
        text = ANSI.sub("", log.read_text(encoding="utf-8", errors="replace"))
        step = {
            "kind": kind,
            "index": index,
            "side": side,
            "exit": exit_code,
            "timed_out": timed_out,
            "matched": bool(pattern) and pattern in text,
            "seconds": round(time.monotonic() - started),
            "tail": tail(text),
        }
        self.result["steps"].append(step)
        self.save()
        show(f"{side} {kind}: {command}", text, step)
        return step


def show(title: str, text: str, step: dict) -> None:
    """Print a command's output in a log group. Stops workflow commands inside it."""
    token = secrets.token_hex(16)
    lines = text.splitlines()
    print(f"::group::{title} [{cell(step)}]")
    print(f"::stop-commands::{token}")
    if len(lines) > LOG_LINES:
        print(f"[{len(lines) - LOG_LINES} earlier lines are in the artifact]")
    print("\n".join(lines[-LOG_LINES:]))
    print(f"::{token}::")
    print("::endgroup::", flush=True)


def verify(registry: dict, sdk: str, change_id: str, base_sha: str, head: Path, base: Path, out: Path,
           acceptance_root: Path = ACCEPTANCE_DIR) -> dict:
    """Run one PR checkout. `base` must not exist yet; it becomes a worktree when needed."""
    entry = registry["sdks"][sdk]
    head_sha = git(head, "rev-parse", "HEAD").strip()
    if subprocess.run(["git", "-C", str(head), "cat-file", "-e", f"{base_sha}^{{commit}}"], capture_output=True).returncode:
        git(head, "fetch", "--no-tags", "origin", base_sha)
    base_commit = git(head, "merge-base", base_sha, "HEAD").strip()
    changed = [path for path in git(head, "diff", "--name-only", "-z", base_commit, "HEAD").split("\0") if path]
    runner, pattern, fixed = pack_info(change_id, sdk, acceptance_root)

    def capped(paths: list) -> list:
        return [path[:PATH_CHARS] for path in paths[:LIST_CAP]]

    rec = Recorder(
        {
            "version": 1,
            "sdk": sdk,
            "change_id": change_id,
            "head_sha": head_sha,
            "base_commit": base_commit,
            "touched_fixed": capped([p for p in changed if fixed and (p == fixed or p.startswith(fixed + "/"))]),
            "touched_config": capped([p for p in changed if config_file(p, entry.get("test_config") or [])]),
            "steps": [],
        },
        out,
    )
    if rec.result["touched_fixed"]:
        return rec.result

    def base_ready() -> bool:
        if rec.get("install", 0, "base") is None:
            git(head, "worktree", "add", "--detach", str(base.resolve()), base_commit)
            rec.step("install", 0, "base", entry["install"], base)
        return ok(rec.get("install", 0, "base"))

    if not ok(rec.step("install", 0, "head", entry["install"], head)):
        base_ready()
        return rec.result
    # Checks run before the acceptance pack, because the pack copies its tests into the tree.
    failed = [i for i, command in enumerate(entry["check"]) if not ok(rec.step("check", i, "head", command, head))]
    if failed and base_ready():
        for index in failed:
            rec.step("check", index, "base", entry["check"][index], base)
    if runner and base_ready():
        env = acceptance_env(entry)
        for side, tree in (("base", base), ("head", head)):
            command = f"bash {shlex.quote(str(runner))} {shlex.quote(str(tree.resolve()))}"
            rec.step("acceptance", 0, side, command, tree, env, pattern)
    return rec.result


# decide and render -----------------------------------------------------------


def paths(items: list) -> str:
    return ", ".join(code(item) for item in items)


def fails(step: dict) -> str:
    return "times out" if step["timed_out"] else "fails"


def decide(result: dict | None, entry: dict, has_pack: bool, pattern: str = "") -> tuple:
    """(outcome, reasons). Every reason is built from trusted text or code spans."""
    if result is None:
        return "needs-human", ["The verify job left no readable result."]
    if result["touched_fixed"]:
        return "verify-failed", [f"Changes the acceptance test path: {paths(result['touched_fixed'])}."]
    steps = {(s["kind"], s["index"], s["side"]): s for s in result["steps"]}
    failed, unsure = [], []

    def compare(command: str, head: dict | None, base: dict | None) -> None:
        if head is None:
            unsure.append(f"{code(command)} did not run.")
        elif ok(head):
            return
        elif base is None:
            unsure.append(f"{code(command)} {fails(head)} on the head, and the base could not be checked.")
        elif ok(base):
            failed.append(f"{code(command)} {fails(head)} on the head but passes on the base.")
        else:
            unsure.append(f"{code(command)} {fails(head)} on the head and on the base.")

    compare(entry["install"], steps.get(("install", 0, "head")), steps.get(("install", 0, "base")))
    if ok(steps.get(("install", 0, "head"))):
        for index, command in enumerate(entry["check"]):
            compare(command, steps.get(("check", index, "head")), steps.get(("check", index, "base")))
        if has_pack:
            if result["touched_config"]:
                unsure.append(f"Changes test configuration: {paths(result['touched_config'])}.")
            base, head = steps.get(("acceptance", 0, "base")), steps.get(("acceptance", 0, "head"))
            if base is None or head is None:
                unsure.append("The acceptance tests did not run.")
            elif base["timed_out"] or base["exit"] == 0 or not base["matched"]:
                how = "time out" if base["timed_out"] else "pass" if base["exit"] == 0 else f"fail without {code(pattern)}"
                unsure.append(f"The acceptance tests {how} on the base, so they do not show this change.")
            elif not ok(head):
                failed.append(f"The acceptance tests {'time out' if head['timed_out'] else 'fail'} on the head.")
    if failed:
        return "verify-failed", failed + unsure
    if unsure:
        return "needs-human", unsure
    return ("verified" if has_pack else "checks-passed"), []


def cell(step: dict | None, expected_failure: bool = False) -> str:
    if step is None:
        return ""
    if step["timed_out"]:
        return "timed out"
    if step["exit"] == 0:
        return "pass"
    return "fail, as expected" if expected_failure and step["matched"] else f"fail ({step['exit']})"


def fence(text: str) -> str:
    """A code block that the text cannot close."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    bar = "`" * max(3, longest + 1)
    return f"{bar}text\n{text}\n{bar}"


def render(item: dict, result: dict | None, entry: dict, has_pack: bool, outcome: str, reasons: list,
           run_url: str = "", marker: bool = True) -> str:
    steps = {(s["kind"], s["index"], s["side"]): s for s in (result or {}).get("steps", [])}
    base = (result or {}).get("base_commit") or item["base_sha"]
    commands = {("install", 0): entry["install"], **{("check", i): command for i, command in enumerate(entry["check"])}}
    rows = [("install", 0, f"Install {code(entry['install'])}")]
    rows += [("check", index, code(command)) for index, command in enumerate(entry["check"])]
    if has_pack:
        commands[("acceptance", 0)] = f"factory/acceptance/{item['change_id']}/{item['sdk']}/run.sh"
        rows.append(("acceptance", 0, f"Acceptance pack {code(item['change_id'])}"))
    lines = [MARKER.format(sha=item["head_sha"])] if marker else []
    lines += [
        f"### SDK factory verify: {outcome}",
        "",
        f"Head {code(item['head_sha'][:7])}, base {code(base[:7])}, change {code(item['change_id']) or 'none'}.",
        "",
        "| Step | Head | Base |",
        "|---|---|---|",
    ]
    for kind, index, label in rows:
        head, base_step = steps.get((kind, index, "head")), steps.get((kind, index, "base"))
        lines.append(f"| {label} | {cell(head)} | {cell(base_step, kind == 'acceptance')} |")
    notes = [f"- {reason}" for reason in reasons]
    if result and result["touched_config"] and not has_pack:
        notes.append(f"- Note: changes test configuration: {paths(result['touched_config'])}.")
    if notes:
        lines += ["", *notes]
    footer = "_Advisory: the pull request's own code ran in the job that produced this result, so review the diff as usual._"
    if run_url:
        footer += f" [Run]({run_url})"

    budget = COMMENT_LIMIT - len("\n".join(lines)) - len(footer) - 200
    blocks, dropped = [], 0
    for (kind, index, side), step in steps.items():
        if ok(step) or (kind, index) not in commands:
            continue
        block = "\n".join(
            [
                f"<details><summary>{side}: <code>{html.escape(commands[(kind, index)])}</code>, last lines</summary>",
                "",
                fence(step["tail"]),
                "",
                "</details>",
            ]
        )
        if len(block) + 2 > budget:
            dropped += 1
            continue
        blocks.append(block)
        budget -= len(block) + 2
    if blocks:
        lines += ["", "\n\n".join(blocks)]
    if dropped:
        lines += ["", f"{dropped} more failed step logs did not fit. They are in the run artifact."]
    return "\n".join(lines + ["", footer])


# report ----------------------------------------------------------------------


def strings(value) -> list:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError("expected a list of strings")
    return [item[:PATH_CHARS] for item in value[:LIST_CAP]]


def load_result(path: Path, item: dict, entry: dict) -> dict | None:
    """A run artifact, validated field by field. Anything unexpected reads as no result."""
    try:
        if path.stat().st_size > 2_000_000:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != 1 or data.get("sdk") != item["sdk"] or data.get("head_sha") != item["head_sha"]:
            return None
        base_commit = data.get("base_commit")
        steps = []
        for raw in data["steps"][:50]:
            kind, index, side = raw["kind"], raw["index"], raw["side"]
            limit = len(entry["check"]) if kind == "check" else 1
            if kind not in KINDS or side not in SIDES or type(index) is not int or not 0 <= index < limit:
                return None
            if type(raw["exit"]) is not int or type(raw["timed_out"]) is not bool:
                return None
            steps.append(
                {
                    "kind": kind,
                    "index": index,
                    "side": side,
                    "exit": raw["exit"],
                    "timed_out": raw["timed_out"],
                    "matched": raw.get("matched") is True,
                    "tail": str(raw.get("tail") or "")[-TAIL_CHARS:],
                }
            )
        return {
            "head_sha": item["head_sha"],
            "base_commit": base_commit if isinstance(base_commit, str) and FULL_SHA.fullmatch(base_commit) else "",
            "touched_fixed": strings(data["touched_fixed"]),
            "touched_config": strings(data["touched_config"]),
            "steps": steps,
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def set_label(gh, fork: str, number: int, outcome: str, token: str) -> None:
    keep = LABELS[outcome]
    gh.api(f"repos/{fork}/issues/{number}/labels", method="POST", fields=[("labels[]", keep)], token=token)
    for name in LABELS.values():
        if name == keep:
            continue
        try:
            gh.api(f"repos/{fork}/issues/{number}/labels/{urllib.parse.quote(name, safe='')}", method="DELETE", token=token)
        except ApiError as err:
            if err.status != 404:
                raise


def report(gh, registry: dict, hub: str, matrix: dict, results: Path, factory_token: str, *,
           acceptance_root: Path = ACCEPTANCE_DIR, run_url: str = "") -> int:
    problems, rows, bodies = 0, [], []
    for item in matrix.get("include") or []:
        entry = registry["sdks"][item["sdk"]]
        runner, pattern, _ = pack_info(item["change_id"], item["sdk"], acceptance_root)
        result = load_result(results / f"verify-{item['sdk']}-{item['pr']}" / "result.json", item, entry)
        outcome, reasons = decide(result, entry, runner is not None, pattern)
        body = render(item, result, entry, runner is not None, outcome, reasons, run_url)
        fork, number = item["repo"], item["pr"]
        link = f"[{fork}#{number}]({SERVER}/{fork}/pull/{number})"
        state = "not posted, `FACTORY_TOKEN` is not set"
        if factory_token:
            try:
                gh.api(f"repos/{fork}/issues/{number}/comments", method="POST", fields=[("body", body)], token=factory_token)
                set_label(gh, fork, number, outcome, factory_token)
                state = "posted"
            except ApiError as err:
                problems += 1
                state = f"posting failed (HTTP {err.status or '?'})"
            if item["tracking"]:
                line = f"**{item['sdk']}** verify: `{outcome}` for {link} at {code(item['head_sha'][:7])}."
                if reasons:
                    line += f" {reasons[0]}"
                try:
                    gh.api(f"repos/{hub}/issues/{item['tracking']}/comments", method="POST", fields=[("body", line)])
                except ApiError as err:
                    problems += 1
                    state += f", tracking comment failed (HTTP {err.status or '?'})"
        rows.append(f"| {item['sdk']} | {link} | {outcome} | {state} |")
        bodies.append(render(item, result, entry, runner is not None, outcome, reasons, marker=False))
    summary("\n".join(["### Verify results", "", "| SDK | Pull request | Outcome | Comment |", "|---|---|---|---|", *rows]))
    for body in bodies:
        summary(body)
    return 1 if problems else 0


# main ------------------------------------------------------------------------


def output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")
    else:
        print(f"{name}={value}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Verify the factory's pull requests in the SDK forks.")
    sub = parser.add_subparsers(dest="command", required=True)
    found = sub.add_parser("find", help="List the PRs to verify and write the matrix output")
    found.add_argument("--pr", default="", help="Only this PR, as a path like owner/js-sdk/pull/5")
    found.add_argument("--force", choices=["true", "false"], default="false", help="Verify heads that already have a result")
    one = sub.add_parser("run", help="Verify one PR checkout")
    one.add_argument("--sdk", required=True)
    one.add_argument("--change-id", default="")
    one.add_argument("--base-sha", required=True)
    one.add_argument("--head", type=Path, default=Path("head"))
    one.add_argument("--base", type=Path, default=Path("base"))
    one.add_argument("--out", type=Path, default=Path("out"))
    posted = sub.add_parser("report", help="Post the results. Reads the matrix from MATRIX")
    posted.add_argument("--results", type=Path, default=Path("results"))
    args = parser.parse_args(argv)

    hub = os.environ.get("GITHUB_REPOSITORY", "")
    try:
        registry = load_registry()
        if args.command == "find":
            entries = find(GitHub(), registry, hub, pr_path=args.pr.strip(), force=args.force == "true")
            output("matrix", json.dumps({"include": entries}, separators=(",", ":")))
            output("count", str(len(entries)))
            return 0
        if args.command == "run":
            if args.sdk not in registry["sdks"] or not FULL_SHA.fullmatch(args.base_sha):
                raise FactoryError("Unknown SDK or base commit.")
            change_id = args.change_id if CHANGE_ID_RE.fullmatch(args.change_id) else ""
            entry = registry["sdks"][args.sdk]
            result = verify(registry, args.sdk, change_id, args.base_sha, args.head, args.base, args.out)
            runner, pattern, _ = pack_info(change_id, args.sdk)
            outcome, reasons = decide(result, entry, runner is not None, pattern)
            item = {"sdk": args.sdk, "change_id": change_id, "head_sha": result["head_sha"], "base_sha": args.base_sha}
            summary(render(item, result, entry, runner is not None, outcome, reasons, marker=False))
            return 1 if outcome == "verify-failed" else 0
        run_id = os.environ.get("GITHUB_RUN_ID", "")
        run_url = f"{SERVER}/{hub}/actions/runs/{run_id}" if run_id else ""
        matrix = json.loads(os.environ.get("MATRIX") or "{}")
        return report(GitHub(), registry, hub, matrix, args.results, os.environ.get("FACTORY_TOKEN", ""), run_url=run_url)
    except (FactoryError, ApiError, subprocess.CalledProcessError) as err:
        print(f"::error::{err}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
