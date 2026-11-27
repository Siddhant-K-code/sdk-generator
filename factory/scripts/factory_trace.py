#!/usr/bin/env python3
"""Read each port worker run with agent-strace and post the evidence to its PRs.

  read    Read the worker run's `usage` artifact: gh-aw's session log and its
          usage numbers. Run agent-strace on the log and write trace.json next
          to agent-strace's own reports. The log is untrusted data, so this
          step holds no write token, and agent-strace gets no token at all.
  report  Post or update one comment on each factory PR the run opened. Reads
          trace.json as untrusted data, field by field. Writes only to an open
          factory PR in a registered fork whose body carries this run's gh-aw
          provenance marker. gh-aw strips that marker from agent text, so only
          the run's own safe outputs job can write it.

The evidence is advisory. It shows what the agent did inside its own session.
The verify job runs the independent checks.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from factory_lib import (  # noqa: E402
    NAME,
    SERVER,
    ApiError,
    FactoryError,
    GitHub,
    code,
    fork_name,
    load_registry,
    summary,
)
from factory_verify import TRUSTED, factory_pull, output  # noqa: E402

WORKER_PATH = ".github/workflows/factory-port.lock.yml"
WORKER_EVENT = "workflow_dispatch"
SESSION_FILE, USAGE_FILE, TRACE_FILE = "aw_session.jsonl", "agent_usage.json", "trace.json"
SESSION_BYTES, LINE_BYTES, SMALL_BYTES = 64 << 20, 8 << 20, 2 << 20
MARKER = "<!-- factory:trace run={run_id} -->"
RUN_ID_RE = re.compile(r"[1-9][0-9]{0,19}")
REPO_RE = re.compile(rf"{NAME}/{NAME}")
# agent-strace's own rule for session ids.
SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}")
VERSION_RE = re.compile(r"[0-9]{1,4}(?:\.[0-9]{1,6}){1,3}")
RULE_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
# gh-aw's safe outputs job reports each PR it opened in these records. The
# agent's own request has phase `agent` and no URL.
PULL_RECORDS = ("safe_output.result", "safe_output.execution", "safe_output.outcome")
PULL_PHASE = "safe_outputs"
FINDINGS = {
    "rationale": "Rationale",
    "scope": "Scope",
    "validation": "Validation",
    "stewardship": "Stewardship",
    "evidence": "Evidence health",
}
STATUSES = ("present", "partial", "missing", "inconclusive")
CONFIDENCES = ("high", "medium", "low")
HEALTH = ("healthy", "partial", "unknown", "invalid")
RESULTS = ("pass", "fail")
NOTES = {
    "no-session": "The run left no gh-aw session log, so there is nothing to read.",
    "too-large": "The session log is over 64 MB, so the trace did not read it.",
    "not-installed": "agent-strace is not installed, so this shows only gh-aw's own numbers.",
    "import-failed": "agent-strace could not import the session log, so this shows only gh-aw's own numbers.",
    "review-failed": "agent-strace review gave no result.",
    "health-failed": "agent-strace health gave no result.",
    "lint-failed": "agent-strace lint gave no result.",
}
TIMEOUT = 300
DETAIL_CHARS, PR_CAP, RULE_CAP = 200, 10, 5
COUNT_MAX = 10**12


def warn(text: str) -> None:
    print(f"::warning::{text}")


def plural(count: int, word: str) -> str:
    return f"{count:,} {word}{'' if count == 1 else 's'}"


def blank(run_id: str, run_repo: str) -> dict:
    return {
        "schema": 1,
        "run_id": run_id,
        "run_repo": run_repo,
        "worker": False,
        "found": False,
        "agent_strace": "",
        "session_id": "",
        "model": "",
        "ai_credits": None,
        "tokens_in": None,
        "tokens_cached": None,
        "tokens_out": None,
        "tools": 0,
        "failed_tools": 0,
        "result": "",
        "findings": [],
        "health": "",
        "lint": None,
        "prs": [],
        "other_prs": 0,
        "notes": [],
    }


# read ------------------------------------------------------------------------


def worker_run(gh, hub: str, run_id: str, run_repo: str) -> bool:
    """The run is this hub's port worker, so the PRs it opened are the factory's."""
    if run_repo.lower() != hub.lower():
        return False
    try:
        run = gh.api(f"repos/{hub}/actions/runs/{run_id}") or {}
    except ApiError:
        return False
    return run.get("path") == WORKER_PATH and run.get("event") == WORKER_EVENT


def pull_of(data: dict, url_re: re.Pattern) -> tuple | None:
    url = data.get("url")
    if isinstance(url, str):
        found = url_re.fullmatch(url.strip())
        if found:
            return found.group(1), int(found.group(2))
    repo, number = data.get("repo"), data.get("number")
    if isinstance(repo, str) and REPO_RE.fullmatch(repo) and type(number) is int and 0 < number < 10**10:
        return repo, number
    return None


def scan_session(path: Path, registry: dict, owner: str) -> dict:
    """The PRs the run opened and the agent's tool calls, from gh-aw's session log."""
    forks = {fork_name(registry, owner, sdk).lower(): sdk for sdk in registry["sdks"]}
    url_re = re.compile(re.escape(SERVER) + rf"/({NAME}/{NAME})/pull/([1-9][0-9]{{0,9}})/?")
    prs, others, seen = [], set(), set()
    tools = failed = 0
    with path.open("rb") as handle:
        for raw in handle:
            if len(raw) > LINE_BYTES:
                continue
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(record, dict) or not isinstance(record.get("data"), dict):
                continue
            kind, data = record.get("type"), record["data"]
            provenance = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
            if kind == "tool.execution_complete" and provenance.get("phase") in (None, "agent"):
                tools += 1
                failed += data.get("success") is False
            elif kind in PULL_RECORDS and provenance.get("phase") == PULL_PHASE and data.get("type") == "create_pull_request":
                if data.get("status") != "success" or data.get("staged") is True:
                    continue
                found = pull_of(data, url_re)
                if not found or (found[0].lower(), found[1]) in seen:
                    continue
                seen.add((found[0].lower(), found[1]))
                sdk = forks.get(found[0].lower())
                if sdk and len(prs) < PR_CAP:
                    prs.append({"sdk": sdk, "repo": fork_name(registry, owner, sdk), "number": found[1]})
                elif not sdk:
                    others.add(found)
    return {"prs": prs, "other_prs": len(others), "tools": tools, "failed_tools": failed}


def number_in(data: dict, key: str) -> int | None:
    value = data.get(key)
    return value if type(value) is int and 0 <= value < COUNT_MAX else None


def read_usage(path: Path) -> dict:
    """gh-aw's own usage numbers. Anything unexpected reads as unknown."""
    try:
        if not path.is_file() or path.stat().st_size > SMALL_BYTES:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    fields = {}
    credits = data.get("ai_credits")
    if type(credits) in (int, float) and math.isfinite(credits) and 0 <= credits < 1e7:
        fields["ai_credits"] = round(float(credits), 4)
    model = data.get("primary_model")
    if isinstance(model, str) and MODEL_RE.fullmatch(model):
        fields["model"] = model
    tokens_in = number_in(data, "input_tokens_inclusive")
    if tokens_in is None:
        parts = [number_in(data, key) for key in ("input_tokens", "cache_read_tokens", "cache_write_tokens")]
        tokens_in = sum(parts) if all(part is not None for part in parts) else None
    fields.update(
        tokens_in=tokens_in,
        tokens_cached=number_in(data, "cache_read_tokens"),
        tokens_out=number_in(data, "output_tokens"),
    )
    return fields


def clean_env() -> dict:
    """The environment without tokens. agent-strace reads untrusted data."""
    return {
        key: value
        for key, value in os.environ.items()
        if "TOKEN" not in key.upper() and not key.upper().startswith("ACTIONS_")
    }


def strace(binary: str, *args: str, cwd: Path) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            [binary, *args],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=TIMEOUT,
            cwd=cwd,
            env=clean_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def json_report(done: subprocess.CompletedProcess | None) -> dict | None:
    """agent-strace's JSON report. Exit 1 only means a threshold was crossed."""
    if done is None or done.returncode not in (0, 1) or len(done.stdout) > SMALL_BYTES:
        return None
    try:
        data = json.loads(done.stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def keep(done: subprocess.CompletedProcess | None, path: Path) -> None:
    """Save agent-strace's own output for the trace artifact."""
    if done is not None and done.returncode in (0, 1) and done.stdout and len(done.stdout) <= SMALL_BYTES:
        path.write_text(done.stdout, encoding="utf-8")


def clip(text) -> str:
    value = re.sub(r"\s+", " ", text if isinstance(text, str) else "").strip()
    return value if len(value) <= DETAIL_CHARS else value[: DETAIL_CHARS - 3].rstrip() + "..."


def review_fields(data: dict | None) -> tuple:
    if not data or data.get("result") not in RESULTS:
        return "", []
    items = data.get("findings") if isinstance(data.get("findings"), list) else []
    findings = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name, status, confidence = item.get("name"), item.get("status"), item.get("confidence")
        if name in FINDINGS and name not in findings and status in STATUSES and confidence in CONFIDENCES:
            findings[name] = {"name": name, "status": status, "confidence": confidence, "detail": clip(item.get("detail"))}
    return data["result"], [findings[name] for name in FINDINGS if name in findings]


def lint_fields(data: dict | None) -> dict | None:
    if not data:
        return None
    counts = {key: number_in(data, key) for key in ("errors", "warnings", "infos")}
    if None in counts.values():
        return None
    items = data.get("findings") if isinstance(data.get("findings"), list) else []
    rules = sorted(
        {item["rule"] for item in items if isinstance(item, dict) and isinstance(item.get("rule"), str) and RULE_RE.fullmatch(item["rule"])}
    )
    return {**counts, "rules": rules[:RULE_CAP]}


def run_strace(binary: str, session: Path, out: Path) -> dict:
    """agent-strace's version, review, health and lint for the session, as trace fields."""
    fields = {"agent_strace": "", "session_id": "", "result": "", "findings": [], "health": "", "lint": None}
    notes = []
    out = out.resolve()
    with tempfile.TemporaryDirectory() as tmp:
        # An empty working directory, so no review config file applies.
        work = Path(tmp)
        store = ["--trace-dir", str(work / "traces")]
        version = strace(binary, "--version", cwd=work)
        if version is None or version.returncode != 0:
            warn("agent-strace is not installed.")
            return {**fields, "notes": ["not-installed"]}
        found = re.search(r"([0-9][0-9.]*)\s*$", version.stdout.strip())
        if found and VERSION_RE.fullmatch(found.group(1)):
            fields["agent_strace"] = found.group(1)
        imported = strace(binary, *store, "import", str(session.resolve()), cwd=work)
        found = imported and re.search(r"^Imported session (\S+)$", f"{imported.stderr}\n{imported.stdout}", re.M)
        if not found or imported.returncode != 0 or not SESSION_ID_RE.fullmatch(found.group(1)):
            warn("agent-strace could not import the session log.")
            return {**fields, "notes": ["import-failed"]}
        session_id = fields["session_id"] = found.group(1)

        review = strace(binary, *store, "review", session_id, "--format", "json", cwd=work)
        keep(review, out / "review.json")
        keep(strace(binary, *store, "review", session_id, "--format", "markdown", cwd=work), out / "review.md")
        fields["result"], fields["findings"] = review_fields(json_report(review))
        if not fields["result"]:
            notes.append("review-failed")

        health = strace(binary, *store, "health", session_id, "--format", "json", cwd=work)
        keep(health, out / "health.json")
        status = (json_report(health) or {}).get("status")
        fields["health"] = status if status in HEALTH else ""
        if not fields["health"]:
            notes.append("health-failed")

        lint = strace(binary, *store, "lint", session_id, "--format", "json", cwd=work)
        keep(lint, out / "lint.json")
        fields["lint"] = lint_fields(json_report(lint))
        if fields["lint"] is None:
            notes.append("lint-failed")

        shared = strace(binary, *store, "share", session_id, "-o", str(out / "evidence.html"), cwd=work)
        if shared is None or shared.returncode != 0:
            warn("agent-strace share did not write the evidence packet.")
    for note in notes:
        warn(NOTES[note])
    return {**fields, "notes": notes}


def read_summary(trace: dict) -> str:
    run_id = trace["run_id"]
    run = f"[Run {run_id}]({SERVER}/{trace['run_repo']}/actions/runs/{run_id})"
    prs = trace["prs"]
    if not trace["worker"]:
        lead = f"{run} is not a port worker run in this repository, so the trace posts no comments."
    elif prs:
        lead = f"{run} opened {plural(len(prs), 'factory PR')}. The report job comments on each."
    else:
        lead = f"{run} opened no factory PR, so the trace posts no comments."
    lines = ["### Agent trace", "", lead]
    if trace["worker"] and prs:
        lines += ["", "| SDK | Pull request |", "|---|---|"]
        lines += [f"| {pr['sdk']} | [{pr['repo']}#{pr['number']}]({SERVER}/{pr['repo']}/pull/{pr['number']}) |" for pr in prs]
    if trace["other_prs"]:
        lines += ["", f"The run also opened {plural(trace['other_prs'], 'pull request')} outside the SDK forks."]
    return "\n".join(lines)


def read(gh, registry: dict, hub: str, usage: Path, out: Path, run_id: str, run_repo: str, binary: str) -> int:
    out.mkdir(parents=True, exist_ok=True)
    trace = blank(run_id, run_repo)
    trace["worker"] = worker_run(gh, hub, run_id, run_repo)
    session = usage / SESSION_FILE
    if not session.is_file():
        trace["notes"].append("no-session")
    elif session.stat().st_size > SESSION_BYTES:
        trace["notes"].append("too-large")
    else:
        trace["found"] = True
        trace.update(scan_session(session, registry, hub.split("/")[0]))
        trace.update(read_usage(usage / USAGE_FILE))
        trace.update(run_strace(binary, session, out))
    (out / TRACE_FILE).write_text(json.dumps(trace, indent=2) + "\n", encoding="utf-8")
    summary(read_summary(trace))
    summary(render(trace, marker=False) if trace["found"] else NOTES[trace["notes"][0]])
    output("count", str(len(trace["prs"]) if trace["worker"] and trace["found"] else 0))
    return 0


# report ----------------------------------------------------------------------


def load_trace(path: Path, registry: dict, hub: str, run_id: str) -> dict:
    """trace.json from the read job, checked field by field."""
    try:
        if path.stat().st_size > SMALL_BYTES:
            raise ValueError("the file is too large")
        return checked(json.loads(path.read_text(encoding="utf-8")), registry, hub, run_id)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as err:
        raise FactoryError(f"The trace artifact is not valid: {err}") from None


def checked(data: dict, registry: dict, hub: str, run_id: str) -> dict:
    def need(ok: bool, field: str) -> None:
        if not ok:
            raise ValueError(f"unexpected {field}")

    def text(field: str, pattern: re.Pattern) -> str:
        value = data.get(field)
        need(value == "" or (isinstance(value, str) and pattern.fullmatch(value) is not None), field)
        return value

    def choice(field: str, options: tuple) -> str:
        value = data.get(field)
        need(value == "" or value in options, field)
        return value

    def count(value, field: str, optional: bool = False) -> int | None:
        if optional and value is None:
            return None
        need(type(value) is int and 0 <= value < COUNT_MAX, field)
        return value

    owner = hub.split("/")[0]
    need(data.get("schema") == 1, "schema")
    need(data.get("run_id") == run_id, "run_id")
    need(isinstance(data.get("run_repo"), str) and data["run_repo"].lower() == hub.lower(), "run_repo")
    need(data.get("worker") is True and data.get("found") is True, "worker")
    credits = data.get("ai_credits")
    need(credits is None or (type(credits) in (int, float) and math.isfinite(credits) and 0 <= credits < 1e7), "ai_credits")
    tools, failed = count(data.get("tools"), "tools"), count(data.get("failed_tools"), "failed_tools")
    need(failed <= tools, "failed_tools")

    findings = []
    need(isinstance(data.get("findings"), list) and len(data["findings"]) <= len(FINDINGS), "findings")
    for item in data["findings"]:
        need(isinstance(item, dict) and item.get("name") in FINDINGS, "findings")
        need(item["name"] not in [seen["name"] for seen in findings], "findings")
        need(item.get("status") in STATUSES and item.get("confidence") in CONFIDENCES, "findings")
        need(isinstance(item.get("detail"), str) and len(item["detail"]) <= DETAIL_CHARS, "findings")
        findings.append({key: item[key] for key in ("name", "status", "confidence", "detail")})

    lint = data.get("lint")
    if lint is not None:
        need(isinstance(lint, dict), "lint")
        rules = lint.get("rules")
        need(isinstance(rules, list) and len(rules) <= RULE_CAP, "lint")
        need(all(isinstance(rule, str) and RULE_RE.fullmatch(rule) for rule in rules), "lint")
        lint = {key: count(lint.get(key), "lint") for key in ("errors", "warnings", "infos")} | {"rules": list(rules)}

    prs = []
    need(isinstance(data.get("prs"), list) and 0 < len(data["prs"]) <= PR_CAP, "prs")
    for item in data["prs"]:
        need(isinstance(item, dict) and item.get("sdk") in registry["sdks"], "prs")
        fork = fork_name(registry, owner, item["sdk"])
        need(item.get("repo") == fork and type(item.get("number")) is int and 0 < item["number"] < 10**10, "prs")
        need((fork, item["number"]) not in [(pr["repo"], pr["number"]) for pr in prs], "prs")
        prs.append({"sdk": item["sdk"], "repo": fork, "number": item["number"]})

    notes = data.get("notes")
    need(isinstance(notes, list) and all(note in NOTES for note in notes) and len(set(notes)) == len(notes), "notes")
    return {
        "schema": 1,
        "run_id": run_id,
        "run_repo": hub,
        "worker": True,
        "found": True,
        "agent_strace": text("agent_strace", VERSION_RE),
        "session_id": text("session_id", SESSION_ID_RE),
        "model": text("model", MODEL_RE),
        "ai_credits": credits,
        "tokens_in": count(data.get("tokens_in"), "tokens_in", optional=True),
        "tokens_cached": count(data.get("tokens_cached"), "tokens_cached", optional=True),
        "tokens_out": count(data.get("tokens_out"), "tokens_out", optional=True),
        "tools": tools,
        "failed_tools": failed,
        "result": choice("result", RESULTS),
        "findings": findings,
        "health": choice("health", HEALTH),
        "lint": lint,
        "prs": prs,
        "other_prs": count(data.get("other_prs"), "other_prs"),
        "notes": list(notes),
    }


def credits_text(value) -> str:
    if value is None:
        return "unknown"
    dollars = value / 100
    return f"{value:,.2f}, " + (f"about ${dollars:,.2f}" if dollars >= 0.005 else "under $0.01")


def tokens_text(trace: dict) -> str:
    parts = []
    if trace["tokens_in"] is not None:
        cached = trace["tokens_cached"]
        parts.append(f"{trace['tokens_in']:,} in" + (f" ({cached:,} from cache)" if cached else ""))
    if trace["tokens_out"] is not None:
        parts.append(f"{trace['tokens_out']:,} out")
    return ", ".join(parts) or "unknown"


def lint_text(lint: dict) -> str:
    text = f"{plural(lint['errors'], 'error')}, {plural(lint['warnings'], 'warning')}"
    if lint["rules"]:
        text += " (" + ", ".join(code(rule) for rule in lint["rules"]) + ")"
    return text


def render(trace: dict, marker: bool = True) -> str:
    """The evidence comment. Every value is a count, an enum or text inside code."""
    run_id, run_repo = trace["run_id"], trace["run_repo"]
    version = trace["agent_strace"]
    lines = [MARKER.format(run_id=run_id)] if marker else []
    lines += [
        "### Agent evidence",
        "",
        f"What the agent did inside its own session in [run {run_id}]({SERVER}/{run_repo}/actions/runs/{run_id})"
        + (f", read with agent-strace {code(version)}." if version else "."),
        "",
        "| Measure | Value |",
        "|---|---|",
        f"| Model | {code(trace['model']) or 'unknown'} |",
        f"| AI credits (gh-aw) | {credits_text(trace['ai_credits'])} |",
        f"| Tokens | {tokens_text(trace)} |",
        f"| Tool calls | {trace['tools']:,}, {trace['failed_tools']:,} failed |",
    ]
    if trace["health"]:
        lines.append(f"| Session health | {trace['health']} |")
    if trace["lint"] is not None:
        lines.append(f"| Lint | {lint_text(trace['lint'])} |")
    if trace["findings"]:
        lines += ["", "| Review | Status | Confidence | Detail |", "|---|---|---|---|"]
        for item in trace["findings"]:
            lines.append(f"| {FINDINGS[item['name']]} | {item['status']} | {item['confidence']} | {code(item['detail'])} |")
    if trace["result"]:
        lines += [
            "",
            f"Review result: **{trace['result']}**. agent-strace's default threshold fails on a "
            "`missing` finding at medium or high confidence.",
        ]
    for note in trace["notes"]:
        lines += ["", NOTES[note]]
    lines += [
        "",
        "This is advisory. It shows the agent's own session, not CI, and sets no label. "
        "The verify comment reports the independent checks.",
    ]
    if trace["session_id"]:
        lines += [
            "",
            "<details><summary>Replay the session</summary>",
            "",
            "```sh",
            f"gh run download {run_id} --repo {run_repo} --name usage --dir usage",
            "agent-strace import usage/aw_session.jsonl",
            f"agent-strace explain {trace['session_id']}",
            "```",
            "",
            "Prompts and tool output stay in the run's `usage` artifact. This comment holds only counts, "
            "statuses and agent-strace's short review notes.",
            "",
            "</details>",
        ]
    return "\n".join(lines) + "\n"


def upsert(gh, fork: str, number: int, body: str, run_id: str, token: str) -> str:
    """Update this run's comment on the PR, or post it."""
    marker = MARKER.format(run_id=run_id)
    comments = gh.api(f"repos/{fork}/issues/{number}/comments?per_page=100", paginate=True) or []
    for comment in comments:
        first = (comment.get("body") or "").lstrip().split("\n", 1)[0].strip()
        if comment.get("author_association") in TRUSTED and first == marker and type(comment.get("id")) is int:
            gh.api(f"repos/{fork}/issues/comments/{comment['id']}", method="PATCH", fields=[("body", body)], token=token)
            return "updated"
    gh.api(f"repos/{fork}/issues/{number}/comments", method="POST", fields=[("body", body)], token=token)
    return "posted"


def from_run(body: str, hub: str, run_id: str) -> bool:
    """The PR body ends with gh-aw's provenance marker for this run."""
    url = re.escape(f"{SERVER}/{hub}/actions/runs/{run_id}")
    marker = re.compile(rf"<!--\s*gh-aw-agentic-workflow:[^\n]*?\brun:\s*{url}\s*-->", re.I)
    return marker.search(body) is not None


def report(gh, hub: str, trace: dict, factory_token: str) -> int:
    body = render(trace)
    problems, rows = 0, []
    for pr in trace["prs"]:
        fork, number = pr["repo"], pr["number"]
        link = f"[{fork}#{number}]({SERVER}/{fork}/pull/{number})"
        try:
            pull = gh.api(f"repos/{fork}/pulls/{number}") or {}
            if not factory_pull(pull, fork) or not from_run(pull.get("body") or "", hub, trace["run_id"]):
                state = "skipped, not an open factory PR from this run"
            elif not factory_token:
                state = "not posted, `FACTORY_TOKEN` is not set"
            else:
                state = upsert(gh, fork, number, body, trace["run_id"], factory_token)
        except ApiError as err:
            problems += 1
            state = f"failed (HTTP {err.status or '?'})"
        rows.append(f"| {pr['sdk']} | {link} | {state} |")
    summary("\n".join(["### Agent evidence comments", "", "| SDK | Pull request | Comment |", "|---|---|---|", *rows]))
    return 1 if problems else 0


# main ------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read a port worker run with agent-strace and post the evidence.")
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("read", help="Read the run's usage artifact and write trace.json")
    one.add_argument("--usage", type=Path, default=Path("usage"))
    one.add_argument("--out", type=Path, default=Path("out"))
    one.add_argument("--run-id", required=True)
    one.add_argument("--run-repo", default="", help="owner/name of the run. Empty means this repository")
    posted = sub.add_parser("report", help="Comment the evidence on the PRs the run opened")
    posted.add_argument("--trace", type=Path, default=Path("trace"))
    posted.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)

    hub = os.environ.get("GITHUB_REPOSITORY", "")
    try:
        run_repo = getattr(args, "run_repo", "") or hub
        for repo in (hub, run_repo):
            if not REPO_RE.fullmatch(repo) or not all(part.strip(".") for part in repo.split("/")):
                raise FactoryError("The repository must be owner/name. Set GITHUB_REPOSITORY.")
        if not RUN_ID_RE.fullmatch(args.run_id):
            raise FactoryError("The run id must be a number.")
        registry = load_registry()
        if args.command == "read":
            binary = os.environ.get("AGENT_STRACE") or "agent-strace"
            return read(GitHub(), registry, hub, args.usage, args.out, args.run_id, run_repo, binary)
        trace = load_trace(args.trace / TRACE_FILE, registry, hub, args.run_id)
        return report(GitHub(), hub, trace, os.environ.get("FACTORY_TOKEN", ""))
    except (FactoryError, ApiError, subprocess.CalledProcessError) as err:
        print(f"::error::{err}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
