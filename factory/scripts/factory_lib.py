"""Shared helpers for the SDK factory scripts. Standard library only."""

from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = ROOT / "factory" / "sdks.yaml"
ACCEPTANCE_DIR = ROOT / "factory" / "acceptance"
SERVER = os.environ.get("GITHUB_SERVER_URL", "https://github.com")

NAME = r"[A-Za-z0-9_.-]+"
CHANGE_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,48}")
SHA_RE = re.compile(r"[0-9a-fA-F]{7,40}")
BRANCH_RE = re.compile(r"[A-Za-z0-9._/-]+")
ORIGIN_TRAILER = "SDK-Factory-Origin:"
CHANGE_TRAILER = "SDK-Factory-Change:"
BOT_LOGIN = "github-actions[bot]"
# Option labels of the issue form. Changing one breaks every open request.
LANES = {
    "A": "A: API spec bump",
    "B": "B: port an SDK change",
    "C": "C: generator template change",
}
# Kind of change options of the issue form. The keys are the `reach` keys in sdks.yaml.
KINDS = {
    "baseline": "Baseline behavior",
    "core-api": "Core API wrapper",
    "other-api": "Other API wrapper",
    "fix": "Fix or internal change",
}
# Model options of the issue form. The keys other than auto are the `models.tiers` keys in sdks.yaml.
MODEL_TIERS = {
    "auto": "Auto",
    "deep": "Deep",
    "standard": "Standard",
    "light": "Light",
}
SECTIONS = ("lane", "kind of change", "model", "source", "change id", "target sdks", "options", "notes")


class FactoryError(Exception):
    """Problems a maintainer can fix. Every message is safe to post."""

    def __init__(self, *problems: str):
        super().__init__("; ".join(problems))
        self.problems = list(problems)


class ApiError(Exception):
    def __init__(self, path: str, message: str):
        super().__init__(f"{path}: {message}")
        found = re.search(r"HTTP (\d{3})", message)
        self.status = int(found.group(1)) if found else None


@dataclasses.dataclass
class Source:
    repo: str  # owner/name
    kind: str  # pull, commit, issues, tree, or short (owner/repo#N until resolved)
    ref: str

    @property
    def path(self) -> str:
        """Published form. It has no host, so upstream threads get no mention."""
        return f"{self.repo}/{self.kind}/{self.ref}"


def normalize_source(raw: str) -> Source:
    text = re.sub(r"[`<>\s]", "", raw or "")
    if not text:
        raise FactoryError("The source is empty.")
    text = re.sub(r"^https?://", "", text, flags=re.I)
    text = re.sub(r"^(www\.)?github\.com/", "", text, flags=re.I)
    short = re.fullmatch(rf"({NAME}/{NAME})#([0-9]+)", text)
    if short:
        return Source(short.group(1), "short", short.group(2))
    parts = re.split(r"[?#]", text, maxsplit=1)[0].strip("/").split("/")
    hint = "Write it as a path, for example `openfga/js-sdk/pull/228`."
    if len(parts) < 4 or not all(re.fullmatch(NAME, p) and p.strip(".") for p in parts[:2]):
        raise FactoryError(f"Cannot read the source. {hint}")
    repo, kind, rest = f"{parts[0]}/{parts[1]}", parts[2], parts[3:]
    if kind in ("pull", "pulls", "issues") and re.fullmatch(r"[0-9]+", rest[0]):
        return Source(repo, "issues" if kind == "issues" else "pull", rest[0])
    if kind == "commit" and SHA_RE.fullmatch(rest[0]):
        return Source(repo, "commit", rest[0].lower())
    if kind == "tree":
        ref = "/".join(rest)
        if BRANCH_RE.fullmatch(ref) and ".." not in ref:
            return Source(repo, "tree", ref)
    raise FactoryError(
        f"The source `{repo}/{kind}/...` is not a pull request, commit, issue or tree. {hint}"
    )


def load_yaml(path: Path) -> dict:
    done = subprocess.run(["yq", "-o=json", str(path)], check=True, capture_output=True, text=True)
    return json.loads(done.stdout or "null") or {}


def load_registry(path: Path = REGISTRY_PATH) -> dict:
    return load_yaml(path)


def load_pack(change_id: str, root: Path = ACCEPTANCE_DIR) -> dict | None:
    """The change's pack.yaml, or None when the change has no acceptance pack."""
    if not CHANGE_ID_RE.fullmatch(change_id or ""):
        return None
    path = root / change_id / "pack.yaml"
    return load_yaml(path) if path.is_file() else None


def fork_name(registry: dict, owner: str, sdk: str) -> str:
    return f"{owner}/{registry['sdks'][sdk]['repo']}"


def sdk_for_repo(registry: dict, owner: str, repo: str) -> str | None:
    wanted = repo.lower()
    for key, entry in registry["sdks"].items():
        if wanted in (entry["upstream"].lower(), fork_name(registry, owner, key).lower()):
            return key
    return None


def slug(text: str, limit: int = 49) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-")


def code(text) -> str:
    """Inline code for Markdown, safe inside tables. GitHub never links inside code."""
    value = re.sub(r"[`\r\n]+", " ", str(text)).strip().replace("|", "\\|")
    return f"`{value}`" if value else ""


def first_line(text) -> str:
    return (text or "").strip().split("\n", 1)[0].strip()


def acceptance_runners(change_id: str, root: Path = ACCEPTANCE_DIR) -> list | None:
    """SDK keys with a runner in the change's acceptance pack, or None without a pack."""
    pack = root / change_id
    if not change_id or not pack.is_dir():
        return None
    return sorted(p.parent.name for p in pack.glob("*/run.sh"))


class GitHub:
    """Thin wrapper over `gh api`. Pass token= to act with a different token.

    body= sends a JSON request body, for payloads that fields cannot express.
    raw=True returns the response text, for media types such as diffs."""

    def api(self, path, *, method="GET", fields=None, headers=None, paginate=False, token=None, body=None, raw=False):
        cmd = ["gh", "api", "-X", method, path]
        if paginate:
            cmd += ["--paginate", "--slurp"]
        for key, value in (headers or {}).items():
            cmd += ["-H", f"{key}: {value}"]
        for key, value in fields or []:
            if isinstance(value, bool):
                cmd += ["-F", f"{key}={str(value).lower()}"]
            else:
                cmd += ["-f", f"{key}={value}"]
        if body is not None:
            cmd += ["--input", "-"]
        env = dict(os.environ)
        if token:
            env["GH_TOKEN"] = token
        stdin = json.dumps(body) if body is not None else None
        done = subprocess.run(cmd, capture_output=True, text=True, env=env, input=stdin)
        if done.returncode != 0:
            raise ApiError(path, (done.stderr or done.stdout).strip())
        if raw:
            return done.stdout
        if not done.stdout.strip():
            return None
        data = json.loads(done.stdout)
        if paginate:
            return [item for page in data for item in page]
        return data


def fork_sync(gh, upstream: str, fork: str, token: str | None, apply: bool = True) -> tuple:
    """Bring fork main up to upstream main. Returns (status text, safe to act on)."""
    owner = fork.split("/")[0]
    try:
        compare = gh.api(f"repos/{upstream}/compare/main...{owner}:main?per_page=1")
    except ApiError as err:
        return f"compare failed (HTTP {err.status or '?'})", False
    behind = int((compare or {}).get("behind_by") or 0)
    if behind == 0:
        return "up to date", True
    commits = f"{behind} commit{'s' if behind != 1 else ''}"
    if not apply:
        return f"{commits} behind", False
    if not token:
        return f"{commits} behind, needs FACTORY_TOKEN", False
    try:
        gh.api(f"repos/{fork}/merge-upstream", method="POST", fields=[("branch", "main")], token=token)
    except ApiError as err:
        return f"{commits} behind, sync failed (HTTP {err.status or '?'})", False
    return f"synced {commits}", True


def issue_form(
    lane: str, source: str, change_id: str = "", targets: str = "", notes: str = "", kind: str = "", model: str = ""
) -> str:
    """Body in the layout of .github/ISSUE_TEMPLATE/factory-propagate.yaml."""
    return "\n".join(
        [
            "### Lane", "", lane, "",
            "### Kind of change", "", kind or "_No response_", "",
            "### Model", "", model or "_No response_", "",
            "### Source", "", source, "",
            "### Change id", "", change_id or "_No response_", "",
            "### Target SDKs", "", targets or "_No response_", "",
            "### Options", "",
            "- [ ] Plan only. Post the plan but do not start workers.",
            "- [ ] Lanes A and C: also start a port worker on each sync PR for the hand-written layer.",
            "",
            "### Notes", "", notes or "_No response_", "",
        ]
    )


def parse_sections(body: str) -> dict:
    """Split an issue form body into its known `### Heading` sections.

    Text before the first heading is ignored, so scanners can add an intro."""
    sections, current = {}, None
    for line in (body or "").splitlines():
        heading = re.fullmatch(r"\s*###\s+(.+?)\s*", line)
        if heading and heading.group(1).lower() in SECTIONS:
            current = heading.group(1).lower()
            sections[current] = []
        elif current:
            sections[current].append(line)
    result = {}
    for key, lines in sections.items():
        text = "\n".join(lines).strip()
        result[key] = "" if text == "_No response_" else text
    return result


def summary(text: str) -> None:
    """Print a Markdown report and add it to the job summary."""
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text.rstrip("\n") + "\n\n")
