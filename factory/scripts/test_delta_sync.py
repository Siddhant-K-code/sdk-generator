"""Tests for delta_sync.sh with a fake make. Run: python3 -m unittest discover -s factory/scripts"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "delta_sync.sh"

# Writes what a generator would: the generator's version, a spec-driven file,
# a README without the SDK's hand edits, and a file the SDK does not track.
FAKE_MAKE = """#!/usr/bin/env bash
set -eu
target=$1
shift
out="" ref=pin
for arg in "$@"; do
  case $arg in
    CLIENTS_OUTPUT_DIR=*) out=${arg#*=} ;;
    OPEN_API_REF=*) ref=${arg#*=} ;;
  esac
done
sdk="$out/fga-${target#build-client-}-sdk"
cat VERSION >"$sdk/version.txt"
printf 'header\\nspec: %s\\ntemplate: %s\\nfooter\\n' "$ref" "$(cat TEMPLATE)" >"$sdk/api.txt"
printf '# SDK\\n\\nGenerated intro.\\n' >"$sdk/README.md"
printf 'built from %s\\n' "$ref" >"$sdk/artifact.txt"
if [ "$ref" = new ]; then printf 'new model\\n' >"$sdk/model_new.txt"; fi
if [ -n "${FAKE_READ_ONLY:-}" ]; then
  printf 'docs\\n' >"$sdk/$FAKE_READ_ONLY"
  chmod 444 "$sdk/$FAKE_READ_ONLY"
fi
"""

SDK_MAIN = {
    "version.txt": "0.9.8\n",
    "api.txt": "header\nspec: pin\ntemplate: v1\nfooter\n",
    "README.md": "# SDK\n\nGenerated intro.\n\nHand-written paragraph.\n",
}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout


def generator(path: Path, template: str) -> Path:
    path.mkdir(parents=True)
    (path / "VERSION").write_text("0.9.7\n")
    (path / "TEMPLATE").write_text(template)
    return path


@unittest.skipUnless(shutil.which("git") and shutil.which("bash"), "needs git and bash")
class DeltaSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.hub = generator(self.tmp / "hub", "v1")
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "make").write_text(FAKE_MAKE)
        (self.bin / "make").chmod(0o755)
        self.sdk = self.hub / "clients" / "fga-js-sdk"
        self.sdk.mkdir(parents=True)
        git(self.sdk, "init", "-q", "-b", "main")

    def commit_main(self, **overrides: str) -> str:
        for name, text in {**SDK_MAIN, **overrides}.items():
            (self.sdk / name).write_text(text)
        git(self.sdk, "add", "-A")
        git(self.sdk, "commit", "-q", "-m", "main")
        return git(self.sdk, "rev-parse", "HEAD").strip()

    def sync(self, **env: str) -> subprocess.CompletedProcess:
        environ = {k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")}
        environ["PATH"] = f"{self.bin}{os.pathsep}{environ['PATH']}"
        return subprocess.run(["bash", str(SCRIPT), "js"], cwd=self.hub, env={**environ, **env}, capture_output=True, text=True)

    def read(self, name: str) -> str:
        return (self.sdk / name).read_text()

    def test_api_change_keeps_the_sdk_version_and_hand_edits(self):
        main = self.commit_main()
        result = self.sync(NEW_OPENAPI_REF="new")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.read("api.txt"), "header\nspec: new\ntemplate: v1\nfooter\n")
        self.assertEqual(self.read("version.txt"), "0.9.8\n")
        self.assertEqual(self.read("README.md"), SDK_MAIN["README.md"])
        self.assertEqual(self.read("model_new.txt"), "new model\n")
        self.assertFalse((self.sdk / "artifact.txt").exists())
        self.assertIn("Left out 1 generated file(s)", result.stdout)
        self.assertIn("The change alters the js SDK: 3 files changed", result.stdout)
        for state in ("CHERRY_PICK_HEAD", "MERGE_MSG"):
            self.assertFalse((self.sdk / ".git" / state).exists(), state)
        self.assertEqual(sorted(git(self.sdk, "status", "--porcelain").splitlines()), [" M api.txt", "?? model_new.txt"])
        self.assertEqual(git(self.sdk, "symbolic-ref", "--short", "HEAD").strip(), "main")
        self.assertEqual(git(self.sdk, "rev-parse", "HEAD").strip(), main)

    def test_generator_change_builds_the_base_with_the_base_generator(self):
        self.commit_main()
        (self.hub / "TEMPLATE").write_text("v2")
        base = generator(self.tmp / "base", "v1")
        result = self.sync(BASE_GENERATOR_DIR=str(base))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.read("api.txt"), "header\nspec: pin\ntemplate: v2\nfooter\n")
        self.assertEqual(git(self.sdk, "status", "--porcelain").splitlines(), [" M api.txt"])

    def test_change_to_hand_edited_lines_needs_a_person(self):
        self.commit_main(**{"api.txt": "header\nspec: pin, patched\ntemplate: v1\nfooter\n"})
        result = self.sync(NEW_OPENAPI_REF="new")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Merge these files by hand", result.stdout)
        self.assertIn("  api.txt", result.stdout)

    def test_files_a_build_left_read_only_are_written_again(self):
        # Like a build step that runs as root and replaces a file with the same content.
        self.commit_main(**{"docs.txt": "docs\n"})
        result = self.sync(NEW_OPENAPI_REF="new", FAKE_READ_ONLY="docs.txt")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.read("api.txt"), "header\nspec: new\ntemplate: v1\nfooter\n")
        self.assertEqual(self.read("docs.txt"), "docs\n")

    def test_no_change_leaves_the_sdk_clean(self):
        self.commit_main()
        result = self.sync()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("does not alter", result.stdout)
        self.assertEqual(git(self.sdk, "status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()
