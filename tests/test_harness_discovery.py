"""Tier 2: real skill discovery by the installed pi and prime loaders, no model.

Each test drives tests/harness_probe.mjs with a temporary HOME, temporary
PI_CODING_AGENT_DIR / PRIME_AGENT_CODING_AGENT_DIR, and a temporary git repository
onboarded through bin/onboard-skill. A missing node, pi or prime-agent is a skip
with the reason, never a pass.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "mixture-of-loops"
ONBOARD = ROOT / "bin" / "onboard-skill"
PROBE = ROOT / "tests" / "harness_probe.mjs"
PACKAGES = {
    "pi": Path(os.environ.get("MOL_PI_PACKAGE", "/usr/lib/node_modules/@earendil-works/pi-coding-agent")),
    "prime": Path(os.environ.get("MOL_PRIME_PACKAGE", "/usr/lib/node_modules/prime-agent")),
}
BINARIES = {"pi": "pi", "prime": "prime-agent"}
HARNESSES = ("pi", "prime")
NAME = "mixture-of-loops"
OVERRIDES = ("PI_CODING_AGENT_DIR", "PRIME_AGENT_CODING_AGENT_DIR", "DSH_HOME", "DSH_AGENTS_HOME")


def unavailable(harness: str) -> str | None:
    """The reason Tier 2 cannot cover this harness, or None."""
    if shutil.which("node") is None:
        return "node is not installed"
    if shutil.which(BINARIES[harness]) is None:
        return f"{BINARIES[harness]} is not on PATH"
    if not (PACKAGES[harness] / "package.json").is_file():
        return f"{harness} package not found at {PACKAGES[harness]}"
    return None


def version(harness: str) -> str:
    return json.loads((PACKAGES[harness] / "package.json").read_text(encoding="utf-8"))["version"]


class Sandbox:
    def __init__(self, base: Path):
        self.base = base
        self.home = base / "home"
        self.repo = base / "repo"
        self.elsewhere = base / "elsewhere"
        self.agent = {"pi": base / "agent-pi", "prime": base / "agent-prime"}
        for path in (self.home, self.repo, self.elsewhere):
            path.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)

    def environment(self) -> dict[str, str]:
        environment = {key: value for key, value in os.environ.items() if key not in OVERRIDES}
        environment.update(
            HOME=str(self.home), TMPDIR=str(self.base), PYTHONDONTWRITEBYTECODE="1",
            PI_CODING_AGENT_DIR=str(self.agent["pi"]),
            PRIME_AGENT_CODING_AGENT_DIR=str(self.agent["prime"]),
            PI_OFFLINE="1", PI_OTEL_DISABLE="1", PRIME_AGENT_TELEMETRY="0", DO_NOT_TRACK="1",
        )
        return environment

    def onboard(self, harness: str, scope: str, *flags: str) -> subprocess.CompletedProcess[str]:
        args = [str(ONBOARD), "--harness", harness, "--scope", scope, *flags]
        if scope == "repo":
            args += ["--repo", str(self.repo)]
        return subprocess.run(args, env=self.environment(), text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, check=False)

    def probe(self, harness: str, cwd: Path, *args: str) -> dict:
        result = subprocess.run(["node", str(PROBE), harness, str(cwd), *args], env=self.environment(),
                                text=True, capture_output=True, stdin=subprocess.DEVNULL, check=False,
                                timeout=120)
        if result.returncode:
            raise AssertionError(f"probe failed ({result.returncode}): {result.stderr}")
        return json.loads(result.stdout)


def ours(report: dict) -> list[dict]:
    return [skill for skill in report["skills"] if skill["name"] == NAME]


def collisions(report: dict) -> list[dict]:
    return [d for d in report["diagnostics"] if d.get("type") == "collision"
            and d.get("collision", {}).get("name") == NAME]


def setUpModule() -> None:
    covered = [f"{h} {version(h)} ({PACKAGES[h]})" for h in HARNESSES if unavailable(h) is None]
    skipped = [f"{h}: {unavailable(h)}" for h in HARNESSES if unavailable(h) is not None]
    print(f"\nTier 2 covers: {', '.join(covered) or 'nothing'}"
          + (f"; skipped: {'; '.join(skipped)}" if skipped else ""), file=sys.stderr)


class HarnessDiscoveryTests(unittest.TestCase):
    def sandbox(self) -> Sandbox:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Sandbox(Path(temporary.name))

    def each_harness(self):
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                reason = unavailable(harness)
                if reason:
                    self.skipTest(reason)
                yield harness

    def assertOnboarded(self, box: Sandbox, harness: str, scope: str) -> None:
        result = box.onboard(harness, scope)
        self.assertEqual(result.returncode, 0, result.stdout)

    def assertCanonical(self, skill: dict) -> None:
        self.assertEqual(skill["realPath"], str((SKILL / "SKILL.md").resolve()))

    # T020: discovered once, from the canonical package.
    def test_repo_onboarding_is_discovered_once_from_the_canonical_package(self) -> None:
        for harness in self.each_harness():
            box = self.sandbox()
            self.assertOnboarded(box, "all", "repo")
            self.assertOnboarded(box, harness, "repo")      # native repo link as well
            self.assertOnboarded(box, harness, "user")      # and a user link: still one entry
            report = box.probe(harness, box.repo, "--trust", "approve")
            self.assertEqual(report["version"], version(harness))
            found = ours(report)
            self.assertEqual(len(found), 1, report["skills"])
            self.assertCanonical(found[0])
            self.assertEqual(found[0]["scope"], "project")
            self.assertEqual(collisions(report), [])
            self.assertEqual(report["diagnostics"], [])

    # T021: user scope only, from an unrelated directory.
    def test_user_scope_only_is_discovered_from_an_unrelated_cwd(self) -> None:
        for harness in self.each_harness():
            for shared in (False, True):
                box = self.sandbox()
                self.assertOnboarded(box, "codex" if shared else harness, "user")
                report = box.probe(harness, box.elsewhere)
                found = ours(report)
                self.assertEqual(len(found), 1, (shared, report["skills"]))
                self.assertCanonical(found[0])
                self.assertEqual(found[0]["scope"], "user")
                expected = (box.home / ".agents/skills" if shared else box.agent[harness] / "skills") / NAME
                self.assertEqual(found[0]["baseDir"], str(expected))

    # T022: pi trust. No user-scope link exists, so nothing can mask the result.
    def test_pi_repo_scope_needs_trust_in_headless_runs(self) -> None:
        reason = unavailable("pi")
        if reason:
            self.skipTest(reason)
        for via in ("pi", "codex"):
            with self.subTest(via=via):
                box = self.sandbox()
                self.assertOnboarded(box, via, "repo")
                self.assertFalse((box.agent["pi"] / "skills" / NAME).exists())
                self.assertFalse((box.home / ".agents/skills" / NAME).exists())

                headless = box.probe("pi", box.repo)
                self.assertIs(headless["projectTrusted"], False)
                self.assertEqual(ours(headless), [], "untrusted headless pi must skip repository skills")
                self.assertEqual(box.onboard("pi", "repo", "--check").returncode,
                                 7 if via == "pi" else 5)

                approved = box.probe("pi", box.repo, "--trust", "approve")
                self.assertEqual(len(ours(approved)), 1)

                box.agent["pi"].mkdir(exist_ok=True)
                (box.agent["pi"] / "trust.json").write_text(json.dumps({str(box.repo): True}), encoding="utf-8")
                trusted = box.probe("pi", box.repo)
                self.assertIs(trusted["projectTrusted"], True)
                self.assertEqual(len(ours(trusted)), 1)
                if via == "pi":
                    self.assertEqual(box.onboard("pi", "repo", "--check").returncode, 0)

    # T023: prime's repository path, and no .claude/skills.
    def test_prime_uses_prime_agent_skills_and_not_claude_skills(self) -> None:
        reason = unavailable("prime")
        if reason:
            self.skipTest(reason)
        box = self.sandbox()
        self.assertOnboarded(box, "claude", "repo")
        self.assertOnboarded(box, "claude", "user")
        self.assertEqual(ours(box.probe("prime", box.repo)), [], ".claude/skills must not be searched")
        self.assertOnboarded(box, "prime", "repo")
        found = ours(box.probe("prime", box.repo))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["baseDir"], str(box.repo / ".prime/agent/skills" / NAME))
        self.assertCanonical(found[0])
        self.assertEqual(box.onboard("prime", "repo", "--check").returncode, 0)

    # T024: broken links, duplicates, and a different second copy.
    def test_broken_links_duplicates_and_collisions(self) -> None:
        for harness in self.each_harness():
            native = ".pi/skills" if harness == "pi" else ".prime/agent/skills"
            box = self.sandbox()
            self.assertOnboarded(box, "codex", "repo")
            broken = box.repo / native / NAME
            broken.parent.mkdir(parents=True)
            broken.symlink_to(box.base / "moved-checkout/skills" / NAME)
            report = box.probe(harness, box.repo, "--trust", "approve")
            self.assertEqual(len(ours(report)), 1, "the broken link is skipped, the shared one still loads")
            checked = box.onboard(harness, "repo", "--check")
            self.assertEqual(checked.returncode, 4, checked.stdout)
            self.assertIn("(broken)", checked.stdout)

            broken.unlink()
            broken.symlink_to(SKILL)
            twin = box.repo / ".agents/skills" / "same-dir-again"
            twin.symlink_to(SKILL)
            report = box.probe(harness, box.repo, "--trust", "approve")
            self.assertEqual(len(ours(report)), 1, "links to one real directory deduplicate")
            self.assertEqual(collisions(report), [])

            copy = box.home / ".agents/skills/older-copy"
            shutil.copytree(SKILL, copy, ignore=shutil.ignore_patterns("__pycache__"))
            report = box.probe(harness, box.repo, "--trust", "approve")
            found = collisions(report)
            self.assertEqual(len(found), 1, report["diagnostics"])
            self.assertEqual(found[0]["collision"]["loserPath"], str(copy / "SKILL.md"))
            self.assertEqual(len(ours(report)), 1)
            self.assertCanonical(ours(report)[0])  # repository scope is scanned first and wins
            checked = box.onboard(harness, "repo", "--check")
            self.assertEqual(checked.returncode, 6, checked.stdout)
            self.assertIn(str(copy / "SKILL.md"), checked.stdout)

    # T025: the /skill: expansion and every resource SKILL.md references.
    def test_skill_expansion_names_the_base_dir_and_references_resolve(self) -> None:
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        links = set(re.findall(r"\]\(((?:references|assets|scripts)/[^)]+)\)", text))
        scripts = set(re.findall(r"SKILL_ROOT/((?:scripts|references|assets)/[\w./-]+)", text))
        self.assertTrue(links and scripts, "SKILL.md references were not found")
        for harness in self.each_harness():
            box = self.sandbox()
            self.assertOnboarded(box, "codex", "repo")
            report = box.probe(harness, box.repo, "--trust", "approve",
                               "--expand", f"/skill:{NAME} derive a pipeline for specs/001-demo")
            base = Path(ours(report)[0]["baseDir"])
            self.assertTrue(base.is_symlink(), "the base directory is the symlink path")
            expansion = report["expansion"]
            self.assertTrue(expansion.startswith(f'<skill name="{NAME}" location="{base}/SKILL.md">'), expansion[:200])
            self.assertIn(f"References are relative to {base}.", expansion)
            self.assertTrue(expansion.endswith("derive a pipeline for specs/001-demo"))
            self.assertIn("Resolve relative resources from the directory containing this `SKILL.md`", expansion)
            self.assertEqual(report["expansionErrors"], [])
            for relative in sorted(links | scripts):
                self.assertTrue((base / relative).is_file(), f"{relative} missing under {base}")
            # the scripts run from the symlinked base with no controlling terminal and stdin closed
            feature = box.repo / "specs/001-demo"
            feature.mkdir(parents=True)
            (feature / "tasks.md").write_text("# Tasks\n\n## Phase 1: Setup\n- [ ] T001 Do it\n", encoding="utf-8")
            bootstrap = subprocess.run(
                ["setsid", sys.executable, str(base / "scripts/bootstrap_contract.py"), "--repo", str(box.repo),
                 "--feature", "specs/001-demo", "--output", "-"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                env={**box.environment(), "MOL_VIA": "shell"}, check=False, timeout=60)
            self.assertEqual(bootstrap.returncode, 0, bootstrap.stderr)
            self.assertEqual([task["id"] for task in json.loads(bootstrap.stdout)["inventory"]["tasks"]], ["T001"])

if __name__ == "__main__":
    unittest.main()
