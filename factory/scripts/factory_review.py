#!/usr/bin/env python3
"""First-pass model review of the factory's pull requests in the SDK forks.

  dispatch  Start the reviewer workflow for each open factory PR whose head
            has a verify result but no review yet. The reviewer comes from
            the other model family: a GPT model reviews Claude's work and
            Claude reviews GPT's.
  gather    Write the files the reviewer reads before its agent starts: the
            PR, its diff, the source change, the verify result and the
            factory's earlier reviews.
  post      Post the reviewer's verdict and findings as one pull request
            review with inline comments and set the review label. On
            `changes`, start one revision of the PR by the port worker.
  prepare   In a revision run of the port worker, check out the PR branch
            and write the review the worker must answer.

The reviewer only comments. A maintainer reviews, approves and merges every PR.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from factory_lib import (  # noqa: E402
    BRANCH_RE,
    CHANGE_ID_RE,
    ORIGIN_TRAILER,
    SERVER,
    ApiError,
    FactoryError,
    GitHub,
    code,
    fork_name,
    load_registry,
    normalize_source,
    summary,
)
from factory_route import API_VERSION, PORT_WORKFLOW, dispatch, load_models  # noqa: E402
from factory_verify import (  # noqa: E402
    FULL_SHA,
    MARKER_RE,
    TRUSTED,
    change_id_of,
    factory_pull,
    factory_pulls,
    set_label,
    tracking_of,
    verified_heads,
)

REVIEW_WORKFLOW = "factory-review.lock.yml"
REVIEW_LABELS = {"ok": "factory:review-ok", "changes": "factory:review-changes", "human": "factory:review-human"}
VERDICTS = tuple(REVIEW_LABELS)
VERDICT_WORDS = {"ok": "looks good", "changes": "changes requested", "human": "needs a maintainer"}
REVIEW_MARKER = "<!-- factory:review head={sha} round={round} verdict={verdict} -->"
REVIEW_MARKER_RE = re.compile(r"<!-- factory:review head=([0-9a-f]{40}) round=([0-9]{1,2}) verdict=(ok|changes|human) -->")
MODEL_TRAILER = "SDK-Factory-Model:"
MODEL_RE = re.compile(r"[a-z0-9][a-z0-9.-]{0,60}")
SEVERITIES = ("blocker", "major", "minor", "nit")
SERIOUS = ("blocker", "major")
MAX_FINDINGS = 20
DISPATCH_CAP = 5
TRIES = 2
DIFF_LIMIT = 400_000
BODY_LIMIT = 20_000
FILES_CAP = 300
REVIEW_LIMIT = 60_000
OUT = Path("/tmp/gh-aw/factory")
RUNNING = {"queued", "in_progress", "waiting", "requested", "pending"}
FOOTER = "_First-pass model review. It only comments: a maintainer reviews, approves and merges._"

# gh-aw escapes template delimiters in agent output, so `{{` arrives as `\{\{`.
TEMPLATE_ESCAPE = re.compile(r"\\(.)", re.S)
FENCED = re.compile(r"```[A-Za-z]*[ \t]*\n(.*?)\n?```", re.S)
CODE_RE = re.compile(r"(```.*?(?:```|\Z)|`[^`\n]+`)", re.S)
LINKY_RE = re.compile(
    r"(?P<url>(?:https?://)?(?:www\.)?github\.com/(?P<url_owner>[A-Za-z0-9-]+)[^\s)\]>`'\"]*)"
    r"|(?P<ref>(?<![\w./-])(?P<ref_owner>[A-Za-z0-9-]+)/[A-Za-z0-9_.-]+(?:#[0-9]+|@[0-9a-f]{7,40}\b))"
    r"|(?P<mention>(?<![\w`])@[A-Za-z0-9][A-Za-z0-9-]*(?:/[A-Za-z0-9_.-]+)?)",
    re.I,
)
HUNK_RE = re.compile(r"@@ -[0-9]+(?:,[0-9]+)? \+([0-9]+)(?:,[0-9]+)? @@")


# settings ---------------------------------------------------------------------


def settings(registry: dict) -> dict:
    """`review` from sdks.yaml, with each number clamped to a safe range."""
    raw = registry.get("review")
    raw = raw if isinstance(raw, dict) else {}

    def number(key: str, default: int, low: int, high: int) -> int:
        value = raw.get(key, default)
        return max(low, min(high, value)) if type(value) is int else default

    reviewers = raw.get("reviewers") if isinstance(raw.get("reviewers"), dict) else {}
    return {
        "rounds": number("rounds", 2, 1, 5),
        "max_comments": number("max_comments", 8, 0, 20),
        "reviewers": {str(key).lower(): str(value) for key, value in reviewers.items()},
        "default": str(raw.get("default") or ""),
    }


def allowed_models(registry: dict) -> list:
    """`models.allowed` without Haiku models, which never run."""
    return [str(model) for model in (registry.get("models") or {}).get("allowed") or [] if "haiku" not in str(model).lower()]


def family(model: str) -> str:
    """The model family: the leading letters, such as `claude` or `gpt`."""
    found = re.match(r"[a-z]+", (model or "").lower())
    return found.group(0) if found else ""


def pick_reviewer(registry: dict, author: str) -> tuple:
    """(reviewer, warning). The reviewer comes from another family than the author, so no model grades its own work."""
    conf, allowed = settings(registry), allowed_models(registry)
    own = family(author)
    for model in (conf["reviewers"].get(own, ""), conf["default"], *allowed):
        if model in allowed and family(model) != own:
            return model, ""
    fallback = conf["default"] if conf["default"] in allowed else (allowed[0] if allowed else "")
    if not fallback:
        return "", "`models.allowed` in `sdks.yaml` has no model to review with."
    return fallback, f"No allowed model comes from another family than {code(author)}, so {code(fallback)} reviews it."


def default_model(registry: dict, override: str = "") -> str:
    """The port worker's default-tier model."""
    models, _ = load_models(registry, override)
    allowed = allowed_models(registry)
    return models["tiers"].get(models["default_tier"]) or (allowed[0] if allowed else "")


# pull request text ------------------------------------------------------------


def trailer(text: str, name: str) -> str:
    """The value of the last `Name: value` trailer line, or empty."""
    found = re.findall(rf"^{re.escape(name)}\s*`?([^`\s]+)`?\s*$", text or "", re.M)
    return found[-1] if found else ""


def author_model(pr: dict) -> str:
    """The model the port worker asked for, from the PR body. Empty for sync PRs and older PRs."""
    value = trailer(pr.get("body") or "", MODEL_TRAILER).lower()
    return value if MODEL_RE.fullmatch(value) else ""


def origin_of(pr: dict) -> str:
    """The published path of the change the PR ports, or empty."""
    value = trailer(pr.get("body") or "", ORIGIN_TRAILER)
    try:
        source = normalize_source(value) if value else None
    except FactoryError:
        return ""
    return source.path if source and source.kind in ("pull", "commit", "issues") else ""


def clean_finding(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    severity = str(raw.get("severity") or "").strip().lower()
    title = " ".join(str(raw.get("title") or "").split())[:160]
    if severity not in SEVERITIES or not title:
        return None
    line = raw.get("line")
    if isinstance(line, str) and line.strip().isdigit():
        line = int(line.strip())
    if type(line) is not int or line < 0:
        line = 0
    path = " ".join(str(raw.get("path") or "").replace("`", "").split())
    path = re.sub(r"^(\./)+", "", path).lstrip("/")[:300]
    return {"severity": severity, "title": title, "path": path, "line": line, "body": str(raw.get("body") or "").strip()[:2000]}


def parse_findings(text) -> tuple:
    """(findings, dropped) from the reviewer's findings JSON, or (None, 0) when it is not JSON.

    The input is a string, but a model may pass the array itself. Valid JSON
    never holds a backslash that escapes one of `{}#$%<`, and the scan reads
    `\\\\` as one escaped backslash, so the repair cannot change a valid document."""
    if isinstance(text, (list, dict)):
        data = text
    else:
        text = str(text or "").strip()
        if not text:
            return [], 0
        text = TEMPLATE_ESCAPE.sub(lambda found: found.group(1) if found.group(1) in "{}#$%<" else found.group(0), text)
        fenced = FENCED.search(text)
        if not text.startswith(("[", "{")) and fenced:
            text = fenced.group(1).strip()
        try:
            data, _ = json.JSONDecoder().raw_decode(text)
        except ValueError:
            return None, 0
    if isinstance(data, dict):
        data = data.get("findings")
    if not isinstance(data, list):
        return None, 0
    findings, dropped = [], 0
    for raw in data:
        item = clean_finding(raw)
        if item is None or len(findings) >= MAX_FINDINGS:
            dropped += 1
        else:
            findings.append(item)
    return findings, dropped


def defang(text: str, owner: str) -> str:
    """Put @mentions and links to other owners' repositories in code.

    GitHub does not link inside code, so a review adds no "mentioned" events
    to upstream threads. Links into the owner's own repositories stay."""

    def fix(found) -> str:
        whole = found.group(0)
        if found.group("mention") is None:
            who = found.group("url_owner") or found.group("ref_owner") or ""
            if who.lower() == owner.lower():
                return whole
        return f"`{whole}`"

    parts = CODE_RE.split(text or "")
    return "".join(part if index % 2 else LINKY_RE.sub(fix, part) for index, part in enumerate(parts))


def right_lines(patch: str) -> set:
    """New-file line numbers a review comment can sit on: the added and context lines of each hunk."""
    lines, number = set(), None
    for row in (patch or "").splitlines():
        hunk = HUNK_RE.match(row)
        if hunk:
            number = int(hunk.group(1))
        elif number is not None and not row.startswith(("-", "\\")):
            lines.add(number)
            number += 1
    return lines


def plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def cut(text: str, limit: int, what: str) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[The {what} is longer than {limit} characters. The rest is cut.]\n"


# GitHub -----------------------------------------------------------------------


def review_marks(gh, fork: str, number: int) -> list:
    """The factory's reviews of a PR, oldest first. Raises ApiError when GitHub cannot list them."""
    reviews = gh.api(f"repos/{fork}/pulls/{number}/reviews?per_page=100", paginate=True) or []
    marks = []
    for review in reviews:
        found = REVIEW_MARKER_RE.match((review.get("body") or "").lstrip())
        if found and review.get("author_association") in TRUSTED:
            marks.append({
                "head": found.group(1),
                "round": int(found.group(2)),
                "verdict": found.group(3),
                "id": review.get("id"),
                "body": review.get("body") or "",
            })
    return marks


def next_round(marks: list) -> int:
    return max((mark["round"] for mark in marks), default=0) + 1


def verify_comment(gh, fork: str, number: int, head: str) -> str:
    """The trusted verify result for this head, or empty."""
    try:
        comments = gh.api(f"repos/{fork}/issues/{number}/comments?per_page=100", paginate=True) or []
    except ApiError:
        return ""
    found = ""
    for comment in comments:
        body = (comment.get("body") or "").lstrip()
        match = MARKER_RE.match(body)
        if match and match.group(1) == head and comment.get("author_association") in TRUSTED:
            found = body
    return found


def default_branch(gh, hub: str) -> str:
    return str((gh.api(f"repos/{hub}") or {}).get("default_branch") or "main")


def review_runs(gh, hub: str) -> list:
    """The latest reviewer runs. The endpoint returns one object, so read one page and do not paginate."""
    try:
        data = gh.api(f"repos/{hub}/actions/workflows/{REVIEW_WORKFLOW}/runs?per_page=100") or {}
    except ApiError as err:
        if err.status == 404:
            return []
        raise
    return data.get("workflow_runs") or []


def run_title(fork: str, number: int, head: str, round_: int) -> str:
    """The reviewer run's name. `run-name` in factory-review.md builds the same text."""
    return f"Factory review {fork}/pull/{number} at {head} round {round_}"


def pull_link(fork: str, number: int) -> str:
    return f"[{fork}#{number}]({SERVER}/{fork}/pull/{number})"


# dispatch ---------------------------------------------------------------------


def dispatch_reviews(gh, registry: dict, hub: str, *, pr_path: str = "", ready: bool = True, cap: int = DISPATCH_CAP) -> int:
    """Start the reviewer on each verified factory PR head without a review. Returns the number of failed starts."""
    conf = settings(registry)
    targets = factory_pulls(gh, registry, hub.split("/")[0], pr_path)
    runs = review_runs(gh, hub) if targets else []
    running = {run.get("display_title") for run in runs if run.get("status") in RUNNING}
    tries: dict = {}
    for run in runs:
        if run.get("status") == "completed":
            tries[run.get("display_title")] = tries.get(run.get("display_title"), 0) + 1

    ref, started, failed, rows = "", 0, 0, []
    for sdk, fork, pr in targets:
        number, head = int(pr["number"]), str((pr.get("head") or {}).get("sha") or "")
        reviewer, warning = pick_reviewer(registry, author_model(pr))
        round_, state = 0, ""
        if not factory_pull(pr, fork):
            state = "skipped: not a factory pull request"
        elif not FULL_SHA.fullmatch(head):
            state = "skipped: unexpected head"
        elif head not in verified_heads(gh, fork, number):
            state = "waits for verify"
        else:
            try:
                marks = review_marks(gh, fork, number)
            except ApiError as err:
                marks, state = None, f"skipped: cannot read the reviews (HTTP {err.status or '?'})"
            if marks is not None:
                round_ = next_round(marks)
                title = run_title(fork, number, head, round_)
                done = [mark["round"] for mark in marks if mark["head"] == head]
                # A PR that gets no new review shows the round of its latest review.
                if done:
                    round_, state = done[-1], "already reviewed"
                elif marks and marks[-1]["verdict"] == "human":
                    round_, state = marks[-1]["round"], "waits for a maintainer"
                elif round_ > conf["rounds"]:
                    round_, state = round_ - 1, f"all {conf['rounds']} rounds used"
                elif title in running:
                    state = "running"
                elif not pr_path and tries.get(title, 0) >= TRIES:
                    state = f"stopped after {TRIES} runs without a review. Start it again with the `pr` input"
                elif not reviewer:
                    state = "no reviewer model"
                elif started >= cap:
                    state = "waits for the next run"
                elif not ready:
                    state = "not started: set the `FACTORY_TOKEN` and `COPILOT_GITHUB_TOKEN` secrets"
                else:
                    inputs = {"sdk": sdk, "target_repo": fork, "pr": str(number), "head_sha": head,
                              "round": str(round_), "model": reviewer}
                    try:
                        ref = ref or default_branch(gh, hub)
                        dispatch(gh, hub, REVIEW_WORKFLOW, ref, inputs)
                        started += 1
                        state = "started" + (f". {warning}" if warning else "")
                    except ApiError as err:
                        failed += 1
                        state = f"start failed (HTTP {err.status or '?'})"
        rows.append(f"| {sdk} | {pull_link(fork, number)} | {code(head[:7])} | {round_ or ''} | {code(reviewer) or 'none'} | {state} |")

    if rows:
        summary("\n".join(["### Factory reviews", "", "| SDK | Pull request | Head | Round | Reviewer | State |",
                           "|---|---|---|---|---|---|", *rows]))
    else:
        summary("### Factory reviews\n\nNo open factory pull requests.")
    return failed


# gather -----------------------------------------------------------------------


def review_inputs(registry: dict, hub: str) -> dict:
    """The reviewer run's inputs from the environment, checked against the registry."""
    env, owner = os.environ, hub.split("/")[0]
    sdk, fork, pr = env.get("SDK", ""), env.get("TARGET_REPO", ""), env.get("PR", "")
    head, round_, model = env.get("HEAD_SHA", ""), env.get("ROUND", ""), env.get("MODEL", "")
    problems = []
    if sdk not in registry["sdks"]:
        problems.append(f"Unknown SDK {code(sdk)}.")
    elif fork != fork_name(registry, owner, sdk):
        problems.append(f"The target repository must be {code(fork_name(registry, owner, sdk))}.")
    if not re.fullmatch(r"[0-9]{1,9}", pr):
        problems.append("The pull request must be a number.")
    if not FULL_SHA.fullmatch(head):
        problems.append("The head must be a full commit SHA.")
    if not re.fullmatch(r"[1-9]", round_):
        problems.append("The round must be a number from 1 to 9.")
    if model not in allowed_models(registry):
        problems.append(f"The model {code(model)} is not in `models.allowed`.")
    if problems:
        raise FactoryError(*problems)
    return {"sdk": sdk, "fork": fork, "pr": int(pr), "head": head, "round": int(round_), "model": model}


def git_text(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def pr_diff(gh, fork: str, number: int, base: str, head: str, sdk_dir: Path) -> str:
    """The PR's diff from the checkout, else from the API, else empty."""
    try:
        if subprocess.run(["git", "-C", str(sdk_dir), "cat-file", "-e", f"{base}^{{commit}}"], capture_output=True).returncode:
            git_text(sdk_dir, "fetch", "-q", "origin", base)
        return git_text(sdk_dir, "diff", f"{base}...{head}")
    except (OSError, subprocess.CalledProcessError):
        try:
            return gh.api(f"repos/{fork}/pulls/{number}", headers={"Accept": "application/vnd.github.diff"}, raw=True) or ""
        except ApiError:
            return ""


def gather_source(gh, origin: str, out: Path) -> None:
    """source.json and source.diff: the change the PR ports. They hold the source's path, never its URL."""
    info, diff_path, diff = {"path": origin}, "", ""
    if not origin:
        info["note"] = "The PR has no SDK-Factory-Origin trailer. A generator sync PR names its generator commit in the PR body."
    else:
        source = normalize_source(origin)
        try:
            if source.kind == "commit":
                data = gh.api(f"repos/{source.repo}/commits/{source.ref}") or {}
                message = (data.get("commit") or {}).get("message") or ""
                info.update(title=message.split("\n", 1)[0], body=cut(message, BODY_LIMIT, "message"))
                diff_path = f"repos/{source.repo}/commits/{source.ref}"
            else:
                kind = "pulls" if source.kind == "pull" else "issues"
                data = gh.api(f"repos/{source.repo}/{kind}/{source.ref}") or {}
                info.update(title=data.get("title") or "", body=cut(data.get("body") or "", BODY_LIMIT, "body"),
                            state=data.get("state") or "", merged=bool(data.get("merged_at")))
                diff_path = f"repos/{source.repo}/pulls/{source.ref}" if source.kind == "pull" else ""
        except ApiError as err:
            info["note"] = f"GitHub did not return the source (HTTP {err.status or '?'})."
        if diff_path:
            try:
                diff = gh.api(diff_path, headers={"Accept": "application/vnd.github.diff"}, raw=True) or ""
            except ApiError:
                info["note"] = "The source diff is too large to fetch. Read the files in the source instead."
    (out / "source.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    (out / "source.diff").write_text(cut(diff, DIFF_LIMIT, "diff"), encoding="utf-8")


def earlier_reviews(gh, fork: str, number: int, marks: list) -> list:
    reviews = []
    for mark in marks:
        try:
            comments = gh.api(f"repos/{fork}/pulls/{number}/reviews/{mark['id']}/comments?per_page=100", paginate=True) or []
        except ApiError:
            comments = []
        reviews.append({
            "round": mark["round"],
            "head": mark["head"],
            "verdict": mark["verdict"],
            "body": cut(mark["body"], BODY_LIMIT, "review"),
            "comments": [{"path": c.get("path"), "line": c.get("line"), "body": cut(c.get("body") or "", 4000, "comment")}
                         for c in comments],
        })
    return reviews


def gather(gh, registry: dict, hub: str, out: Path = OUT, sdk_dir: Path = Path("sdk")) -> None:
    item = review_inputs(registry, hub)
    fork, number, head = item["fork"], item["pr"], item["head"]
    pr = gh.api(f"repos/{fork}/pulls/{number}")
    if not factory_pull(pr, fork):
        raise FactoryError("The pull request is closed or is not a factory pull request.")
    if pr["head"]["sha"] != head:
        raise FactoryError("The pull request has a newer head. Its next verify run starts a review of it.")
    out.mkdir(parents=True, exist_ok=True)
    conf = settings(registry)
    base = pr["base"]["sha"]
    info = {
        "repo": fork,
        "number": number,
        "title": pr.get("title") or "",
        "body": cut(pr.get("body") or "", BODY_LIMIT, "body"),
        "head_ref": pr["head"]["ref"],
        "head_sha": head,
        "base_ref": pr["base"]["ref"],
        "base_sha": base,
        "sync": str(pr["head"]["ref"]).startswith("sync/factory-"),
        "author_model": author_model(pr),
        "reviewer_model": item["model"],
        "change_id": change_id_of(pr),
        "origin": origin_of(pr),
        "tracking": tracking_of(pr, hub),
        "round": item["round"],
        "rounds": conf["rounds"],
    }
    (out / "sdk.json").write_text(json.dumps(registry["sdks"][item["sdk"]], indent=2) + "\n", encoding="utf-8")
    (out / "pr.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    diff = pr_diff(gh, fork, number, base, head, sdk_dir)
    (out / "pr.diff").write_text(cut(diff, DIFF_LIMIT, "diff") if diff else "[GitHub did not return the diff. Use git in ./sdk.]\n",
                                 encoding="utf-8")
    try:
        files = gh.api(f"repos/{fork}/pulls/{number}/files?per_page=100", paginate=True) or []
    except ApiError:
        files = []
    listed = [{"name": f.get("filename"), "status": f.get("status"), "additions": f.get("additions"),
               "deletions": f.get("deletions")} for f in files[:FILES_CAP]]
    (out / "files.json").write_text(json.dumps(listed, indent=2) + "\n", encoding="utf-8")
    gather_source(gh, info["origin"], out)
    verify = verify_comment(gh, fork, number, head)
    (out / "verify.md").write_text(verify or "No verify result for this head.\n", encoding="utf-8")
    marks = review_marks(gh, fork, number)
    (out / "reviews.json").write_text(json.dumps(earlier_reviews(gh, fork, number, marks), indent=2) + "\n", encoding="utf-8")
    summary("\n".join([
        "### Review context",
        "",
        f"{pull_link(fork, number)} at {code(head[:7])}, round {item['round']} of {conf['rounds']}.",
        f"Author {code(info['author_model']) or 'unknown'}, reviewer {code(item['model'])}.",
        f"Diff of {plural(len(diff), 'character')} in {plural(len(listed), 'file')}. Earlier factory reviews: {len(marks)}.",
    ]))


# post -------------------------------------------------------------------------


def agent_item(path: str) -> dict | None:
    """The reviewer's last `post_review` output, or None."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    items = data.get("items") if isinstance(data, dict) else None
    found = [item for item in items or [] if isinstance(item, dict) and item.get("type") in ("post_review", "post-review")]
    return found[-1] if found else None


def diff_lines(gh, fork: str, number: int) -> dict:
    """{path: lines a comment can sit on} for the PR's files."""
    try:
        files = gh.api(f"repos/{fork}/pulls/{number}/files?per_page=100", paginate=True) or []
    except ApiError:
        return {}
    return {str(f.get("filename")): right_lines(f.get("patch") or "") for f in files}


def anchor(findings: list, files: dict, limit: int) -> set:
    """Indexes of the findings that sit on a line of the diff, most severe first, at most `limit`.

    The reviewer reads the SDK in ./sdk, so a path may start with `sdk/`."""
    chosen = set()
    for index, finding in enumerate(findings):
        if finding["path"] not in files and finding["path"].startswith("sdk/") and finding["path"][4:] in files:
            finding["path"] = finding["path"][4:]
        if len(chosen) < limit and finding["line"] and finding["line"] in files.get(finding["path"], set()):
            chosen.add(index)
    return chosen


def where(finding: dict) -> str:
    if not finding["path"]:
        return "general"
    return code(f"{finding['path']}:{finding['line']}" if finding["line"] else finding["path"])


def cell(text: str) -> str:
    return text.replace("|", "\\|")


def comment_body(finding: dict, index: int, owner: str, spot: str = "") -> str:
    body = f"**{index}. {finding['severity']}: {defang(finding['title'], owner)}**" + (f" ({spot})" if spot else "")
    if finding["body"]:
        body += "\n\n" + close_fences(defang(finding["body"], owner))
    return body


def close_fences(text: str) -> str:
    """Close a code fence the text leaves open, so it cannot swallow the rest of the review."""
    return text + "\n```" if len(re.findall(r"^\s*```", text, re.M)) % 2 else text


def render_review(info: dict, verdict: str, text: str, findings: list, inline: set, notes: list, next_step: str,
                  run_url: str = "") -> str:
    """The review body. `inline` holds the indexes of the findings posted as inline comments."""
    owner = info["fork"].split("/")[0]
    blocks = [
        REVIEW_MARKER.format(sha=info["head"], round=info["round"], verdict=verdict)
        + f"\n### SDK factory review, round {info['round']} of {info['rounds']}: {VERDICT_WORDS[verdict]}",
        f"Reviewer {code(info['model'])}, author {code(info['author']) or 'unknown'}, head {code(info['head'][:7])}.",
    ]
    if text.strip():
        blocks.append(close_fences(defang(cut(text.strip(), 8000, "summary"), owner)))
    if findings:
        rows = ["| # | Severity | Finding | Where |", "|---|---|---|---|"]
        for index, finding in enumerate(findings):
            spot = where(finding) + (", inline" if index in inline else "")
            rows.append(f"| {index + 1} | {finding['severity']} | {cell(defang(finding['title'], owner))} | {spot} |")
        blocks.append("\n".join(rows))
    end = ["\n".join(f"- {note}" for note in notes)] if notes else []
    end += [f"**Next:** {next_step}", FOOTER + (f" [Run]({run_url})" if run_url else "")]

    budget = REVIEW_LIMIT - len("\n\n".join(blocks + end)) - 200
    dropped = 0
    for index, finding in enumerate(findings):
        if index in inline or not finding["body"]:
            continue
        block = comment_body(finding, index + 1, owner, where(finding))
        if len(block) + 2 > budget:
            dropped += 1
            continue
        blocks.append(block)
        budget -= len(block) + 2
    if dropped:
        blocks.append(f"The details of {plural(dropped, 'more finding')} did not fit.")
    return "\n\n".join(blocks + end) + "\n"


def post_review(gh, fork: str, number: int, head: str, body: str, comments: list, token: str, fallback: str) -> str:
    """Post one COMMENT review. GitHub rejects the whole review when one comment misses the diff, so retry without them."""
    payload = {"commit_id": head, "event": "COMMENT", "body": body, "comments": comments}
    try:
        gh.api(f"repos/{fork}/pulls/{number}/reviews", method="POST", body=payload, token=token)
        return "posted"
    except ApiError as err:
        if err.status != 422 or not comments:
            raise
    gh.api(f"repos/{fork}/pulls/{number}/reviews", method="POST",
           body={"commit_id": head, "event": "COMMENT", "body": fallback}, token=token)
    return "posted without inline comments"


def request_copilot_review(gh, fork: str, number: int, token: str) -> str:
    try:
        gh.api(f"repos/{fork}/pulls/{number}/copilot-code-review", method="POST", body={"review_effort": "balanced"},
               headers={"X-GitHub-Api-Version": API_VERSION}, token=token)
        return "requested"
    except ApiError as err:
        if err.status == 409:
            return "already pending"
        return f"not available (HTTP {err.status or '?'})"


def decide(item: dict, findings, dropped: int) -> tuple:
    """(verdict, findings, notes): the reviewer's verdict, made consistent with its findings."""
    notes = []
    verdict = str(item.get("verdict") or "").strip().lower()
    if verdict not in VERDICTS:
        notes.append("The reviewer gave no valid verdict.")
        verdict = "human"
    if findings is None:
        notes.append("The reviewer's findings were not valid JSON, so this review lists none.")
        findings, verdict = [], "human"
    if dropped:
        notes.append(f"The review leaves out {plural(dropped, 'finding')} that {'was' if dropped == 1 else 'were'} "
                     f"incomplete or over the limit of {MAX_FINDINGS}.")
    findings = sorted(findings, key=lambda finding: SEVERITIES.index(finding["severity"]))
    if verdict == "ok" and any(finding["severity"] in SERIOUS for finding in findings):
        notes.append("The reviewer said the PR looks good but listed a blocker or major finding, so the verdict is `changes`.")
        verdict = "changes"
    if verdict == "changes" and not findings:
        notes.append("The reviewer asked for changes without listing any.")
        verdict = "human"
    return verdict, findings, notes


def post(gh, registry: dict, hub: str, agent_output: str, factory_token: str, *, copilot: bool = False,
         run_url: str = "", models_override: str = "") -> int:
    info = review_inputs(registry, hub)
    fork, number, head = info["fork"], info["pr"], info["head"]
    conf = settings(registry)
    info["rounds"] = conf["rounds"]
    item = agent_item(agent_output)
    if item is None:
        summary("### Factory review\n\nThe reviewer did not call `post_review`, so nothing was posted.")
        return 0

    pr = gh.api(f"repos/{fork}/pulls/{number}")
    stale = ""
    if not factory_pull(pr, fork):
        stale = "The pull request is closed or is no longer a factory pull request."
    elif pr["head"]["sha"] != head:
        stale = "The pull request has a newer head. Its next verify run starts a review of it."
    else:
        marks = review_marks(gh, fork, number)
        if any(mark["head"] == head for mark in marks):
            stale = "This head already has a factory review."
        elif info["round"] != next_round(marks) or info["round"] > conf["rounds"]:
            stale = f"Round {info['round']} is not the next review round."
    if stale:
        summary(f"### Factory review\n\nNot posted. {stale}")
        return 0

    info["author"] = author_model(pr)
    findings, dropped = parse_findings(item.get("findings"))
    verdict, findings, notes = decide(item, findings, dropped)
    head_ref = str(pr["head"]["ref"])
    change_id, origin, tracking = change_id_of(pr), origin_of(pr), tracking_of(pr, hub)
    revise = (verdict == "changes" and info["round"] < conf["rounds"] and head_ref.startswith("sdk-factory/")
              and bool(origin) and bool(change_id))
    if verdict == "ok":
        next_step = "A maintainer reviews, approves and merges."
    elif verdict == "human":
        next_step = "A maintainer takes over. The factory does not review this PR again."
    elif revise:
        next_step = f"The port worker revises this PR once. Round {info['round'] + 1} then reviews the new head."
    elif head_ref.startswith("sync/factory-"):
        next_step = "The factory does not revise sync PRs. A maintainer fixes the generator change or this PR."
    elif info["round"] >= conf["rounds"]:
        next_step = f"That was the last of {conf['rounds']} rounds. A maintainer takes over."
    else:
        next_step = "The PR has no factory origin or change id, so the factory cannot revise it. A maintainer takes over."

    owner = hub.split("/")[0]
    inline = anchor(findings, diff_lines(gh, fork, number), conf["max_comments"])
    comments = [{"path": f["path"], "line": f["line"], "side": "RIGHT", "body": comment_body(f, index + 1, owner)}
                for index, f in enumerate(findings) if index in inline]
    text = str(item.get("summary") or "")
    body = render_review(info, verdict, text, findings, inline, notes, next_step, run_url)

    state = {"review": "not posted, `FACTORY_TOKEN` is not set", "label": "", "revise": "", "copilot": ""}
    problems, posted = 0, False
    if factory_token:
        try:
            fallback = render_review(info, verdict, text, findings, set(), notes, next_step, run_url)
            state["review"] = post_review(gh, fork, number, head, body, comments, factory_token, fallback)
            posted = True
        except ApiError as err:
            problems += 1
            state["review"] = f"posting failed (HTTP {err.status or '?'})"
    if posted:
        try:
            set_label(gh, fork, number, verdict, factory_token, labels=REVIEW_LABELS)
            state["label"] = code(REVIEW_LABELS[verdict])
        except ApiError as err:
            problems += 1
            state["label"] = f"failed (HTTP {err.status or '?'})"
        if tracking:
            line = (f"**{info['sdk']}** review round {info['round']} of {conf['rounds']}: `{verdict}` for "
                    f"{pull_link(fork, number)} at {code(head[:7])}. {next_step}")
            try:
                gh.api(f"repos/{hub}/issues/{tracking}/comments", method="POST", fields=[("body", line)])
            except ApiError as err:
                problems += 1
                state["review"] += f", tracking comment failed (HTTP {err.status or '?'})"
        if revise:
            allowed = allowed_models(registry)
            model = info["author"] if info["author"] in allowed else default_model(registry, models_override)
            inputs = {"sdk": info["sdk"], "target_repo": fork, "source": origin, "change_id": change_id,
                      "base_branch": str(pr["base"]["ref"]), "tracking_issue": tracking, "notes": "",
                      "revise_pr": str(number), "model": model}
            try:
                dispatch(gh, hub, PORT_WORKFLOW, default_branch(gh, hub), inputs)
                state["revise"] = f"started with {code(model)}"
            except ApiError as err:
                problems += 1
                state["revise"] = f"start failed (HTTP {err.status or '?'})"
        if copilot and info["round"] == 1:
            state["copilot"] = request_copilot_review(gh, fork, number, factory_token)

    summary("\n".join([
        "### Factory review",
        "",
        "| SDK | Pull request | Round | Verdict | Review | Label | Revision | Copilot review |",
        "|---|---|---|---|---|---|---|---|",
        f"| {info['sdk']} | {pull_link(fork, number)} | {info['round']} of {conf['rounds']} | {verdict} | {state['review']} | "
        f"{state['label'] or 'none'} | {state['revise'] or 'none'} | {state['copilot'] or 'off'} |",
    ]))
    summary(body)
    return 1 if problems else 0


# prepare ----------------------------------------------------------------------


def prepare(gh, registry: dict, hub: str, out: Path = OUT, sdk_dir: Path = Path("sdk")) -> None:
    """Check out the PR branch for a revision and write the review it answers."""
    env, owner = os.environ, hub.split("/")[0]
    sdk, fork = env.get("SDK", ""), env.get("TARGET_REPO", "")
    change_id, base_branch, raw = env.get("CHANGE_ID", ""), env.get("BASE_BRANCH", ""), env.get("REVISE_PR", "")
    if sdk not in registry["sdks"] or fork != fork_name(registry, owner, sdk):
        raise FactoryError("The SDK and the target repository do not match `factory/sdks.yaml`.")
    if not (CHANGE_ID_RE.fullmatch(change_id) and BRANCH_RE.fullmatch(base_branch) and re.fullmatch(r"[0-9]{1,9}", raw)):
        raise FactoryError("The change id, base branch or pull request number is not valid.")
    number = int(raw)
    pr = gh.api(f"repos/{fork}/pulls/{number}")
    head_ref, head = str(pr["head"]["ref"]), str(pr["head"]["sha"])
    if not factory_pull(pr, fork):
        raise FactoryError("The pull request is closed or is not a factory pull request.")
    if not re.fullmatch(rf"sdk-factory/{re.escape(change_id)}(-[0-9a-f]{{16}})?", head_ref):
        raise FactoryError(f"The pull request's branch {code(head_ref)} is not the port branch of {code(change_id)}.")
    if pr["base"]["ref"] != base_branch:
        raise FactoryError(f"The pull request's base is {code(pr['base']['ref'])}, not {code(base_branch)}.")
    conf = settings(registry)
    marks = review_marks(gh, fork, number)
    last = marks[-1] if marks else None
    if not last or last["verdict"] != "changes" or last["head"] != head or last["round"] >= conf["rounds"]:
        raise FactoryError("The latest factory review does not ask for a revision of the pull request's head.")

    try:
        git_text(sdk_dir, "fetch", "-q", "origin", f"+refs/heads/{head_ref}:refs/remotes/origin/{head_ref}")
    except subprocess.CalledProcessError:
        git_text(sdk_dir, "rev-parse", "--verify", "-q", f"refs/remotes/origin/{head_ref}")
    git_text(sdk_dir, "switch", "-q", "-C", head_ref, f"refs/remotes/origin/{head_ref}")
    if git_text(sdk_dir, "rev-parse", "HEAD").strip() != head:
        raise FactoryError("The pull request's branch moved after the review. Its next review covers the new head.")

    out.mkdir(parents=True, exist_ok=True)
    revise = {"pr": number, "head_ref": head_ref, "head_sha": head, "round": last["round"],
              "next_round": last["round"] + 1, "rounds": conf["rounds"]}
    (out / "revise.json").write_text(json.dumps(revise, indent=2) + "\n", encoding="utf-8")
    review = earlier_reviews(gh, fork, number, [last])[0]
    lines = [f"# Factory review, round {last['round']} of {conf['rounds']}", "", review["body"]]
    if review["comments"]:
        lines += ["", "## Inline comments"]
        for comment in review["comments"]:
            spot = f"{comment['path']}:{comment['line']}"
            lines += ["", f"### {code(spot)}", "", comment["body"]]
    (out / "review.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "verify.md").write_text(verify_comment(gh, fork, number, head) or "No verify result for this head.\n", encoding="utf-8")
    summary(f"### Revision\n\nRevising {pull_link(fork, number)} on {code(head_ref)} at {code(head[:7])} "
            f"after review round {last['round']} of {conf['rounds']}.")


# main -------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="First-pass model review of the factory's pull requests.")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("dispatch", help="Start the reviewer on verified PR heads without a review")
    start.add_argument("--pr", default="", help="Only this PR, as a path like owner/js-sdk/pull/5")
    sub.add_parser("gather", help="Write the reviewer's context files. Reads the run inputs from the environment")
    sub.add_parser("post", help="Post the reviewer's output from GH_AW_AGENT_OUTPUT")
    sub.add_parser("prepare", help="Check out the PR branch for a revision run of the port worker")
    args = parser.parse_args(argv)

    hub = os.environ.get("GITHUB_REPOSITORY", "")
    try:
        registry, gh = load_registry(), GitHub()
        if args.command == "dispatch":
            ready = os.environ.get("HAS_FACTORY_TOKEN") == "true" and os.environ.get("HAS_COPILOT_TOKEN") == "true"
            return 1 if dispatch_reviews(gh, registry, hub, pr_path=args.pr.strip(), ready=ready) else 0
        if args.command == "gather":
            gather(gh, registry, hub)
            return 0
        if args.command == "prepare":
            prepare(gh, registry, hub)
            return 0
        run_id = os.environ.get("GITHUB_RUN_ID", "")
        return post(gh, registry, hub, os.environ.get("GH_AW_AGENT_OUTPUT", ""), os.environ.get("FACTORY_TOKEN", ""),
                    copilot=os.environ.get("FACTORY_COPILOT_REVIEW", "") == "on",
                    run_url=f"{SERVER}/{hub}/actions/runs/{run_id}" if run_id else "",
                    models_override=os.environ.get("FACTORY_MODELS", ""))
    except (FactoryError, ApiError, subprocess.CalledProcessError) as err:
        print(f"::error::{err}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
