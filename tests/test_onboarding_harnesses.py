"""Tier 1 (hermetic): bin/onboard-skill for pi and prime, and the unchanged harnesses.

Every test uses a temporary HOME and temporary agent directories; nothing here reads
or writes the real harness homes, calls a harness binary, or calls a model.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "mixture-of-loops"
ONBOARD = ROOT / "bin" / "onboard-skill"
OVERRIDES = ("PI_CODING_AGENT_DIR", "PRIME_AGENT_CODING_AGENT_DIR", "DSH_HOME", "DSH_AGENTS_HOME")


def run(*args: object, home: Path, cwd: Path | None = None, **extra: str) -> subprocess.CompletedProcess[str]:
    environment = {key: value for key, value in os.environ.items() if key not in OVERRIDES}
    environment.update(HOME=str(home), PYTHONDONTWRITEBYTECODE="1", **extra)
    return subprocess.run([str(item) for item in args], cwd=cwd, env=environment, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)


def links(base: Path) -> dict[str, str]:
    """Every symlink under base, as {path relative to base: target}."""
    found = {}
    for directory, dirnames, filenames in os.walk(base):
        for name in dirnames + filenames:
            path = Path(directory) / name
            if path.is_symlink():
                found[str(path.relative_to(base))] = os.readlink(path)
    return found


class Sandbox:
    """A temporary HOME and a temporary git repository side by side."""

    def __init__(self, base: Path):
        self.base = base
        self.home = base / "home"
        self.repo = base / "repo"
        self.home.mkdir()
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)

    def onboard(self, harness: str, scope: str, *flags: str, **extra: str) -> subprocess.CompletedProcess[str]:
        args = [ONBOARD, "--harness", harness, "--scope", scope, *flags]
        if scope == "repo":
            args += ["--repo", self.repo]
        return run(*args, home=self.home, **extra)


class OnboardingHarnessTests(unittest.TestCase):
    def sandbox(self) -> Sandbox:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Sandbox(Path(temporary.name))

    def assertCanonicalLink(self, path: Path) -> None:
        self.assertTrue(path.is_symlink(), path)
        self.assertEqual(path.resolve(), SKILL.resolve())

    # T012: documented links for pi and prime, with and without the override variables.
    def test_pi_and_prime_links_at_user_and_repo_scope(self) -> None:
        cases = [
            ("pi", "user", {}, lambda s: s.home / ".pi/agent/skills/mixture-of-loops"),
            ("prime", "user", {}, lambda s: s.home / ".prime/agent/skills/mixture-of-loops"),
            ("pi", "user", {"PI_CODING_AGENT_DIR": "AGENT"}, lambda s: s.base / "agent-pi/skills/mixture-of-loops"),
            ("prime", "user", {"PRIME_AGENT_CODING_AGENT_DIR": "AGENT"},
             lambda s: s.base / "agent-prime/skills/mixture-of-loops"),
            ("pi", "user", {"PI_CODING_AGENT_DIR": "~/tilde-pi"}, lambda s: s.home / "tilde-pi/skills/mixture-of-loops"),
            ("pi", "repo", {}, lambda s: s.repo / ".pi/skills/mixture-of-loops"),
            ("prime", "repo", {}, lambda s: s.repo / ".prime/agent/skills/mixture-of-loops"),
            ("pi", "repo", {"PI_CODING_AGENT_DIR": "AGENT"}, lambda s: s.repo / ".pi/skills/mixture-of-loops"),
            ("prime", "repo", {"PRIME_AGENT_CODING_AGENT_DIR": "AGENT"},
             lambda s: s.repo / ".prime/agent/skills/mixture-of-loops"),
        ]
        for harness, scope, extra, expected in cases:
            with self.subTest(harness=harness, scope=scope, extra=extra):
                box = self.sandbox()
                extra = {key: str(box.base / f"agent-{harness}") if value == "AGENT" else value
                         for key, value in extra.items()}
                result = box.onboard(harness, scope, **extra)
                self.assertEqual(result.returncode, 0, result.stdout)
                target = expected(box)
                self.assertCanonicalLink(target)
                self.assertEqual(set(links(box.base)), {str(target.relative_to(box.base))})
                self.assertIn(f"{harness}: linked {target}", result.stdout)

    # T013: --check is truthful.
    def test_check_reports_present_missing_broken_and_collision(self) -> None:
        for harness, scope in (("pi", "user"), ("prime", "user"), ("prime", "repo")):
            with self.subTest(harness=harness, scope=scope):
                box = self.sandbox()
                missing = box.onboard(harness, scope, "--check")
                self.assertEqual(missing.returncode, 5, missing.stdout)
                self.assertIn(f"{harness}: missing:", missing.stdout)
                self.assertEqual(links(box.base), {})

                self.assertEqual(box.onboard(harness, scope).returncode, 0)
                present = box.onboard(harness, scope, "--check")
                self.assertEqual(present.returncode, 0, present.stdout)
                self.assertIn(f"{harness}: ok -> {SKILL}", present.stdout)

                agent = box.home / (".pi/agent" if harness == "pi" else ".prime/agent")
                target = (agent / "skills" if scope == "user" else
                          box.repo / ".prime/agent/skills") / "mixture-of-loops"
                target.unlink()
                target.symlink_to(box.base / "moved-checkout/skills/mixture-of-loops")
                broken = box.onboard(harness, scope, "--check")
                self.assertEqual(broken.returncode, 4, broken.stdout)
                self.assertIn("existing link points elsewhere (broken)", broken.stdout)

    def test_check_and_install_detect_a_different_skill_with_the_same_name(self) -> None:
        impostor_roots = {
            "pi": lambda b: b.home / ".agents/skills/vendored",          # a user root
            "prime": lambda b: b.repo / ".agents/skills/nested/copy",    # a repository root
        }
        for harness, where in impostor_roots.items():
            with self.subTest(harness=harness):
                box = self.sandbox()
                for other in ("codex", "claude", harness):
                    self.assertEqual(box.onboard(other, "repo").returncode, 0)
                impostor = where(box)
                impostor.mkdir(parents=True)
                (impostor / "SKILL.md").write_text(
                    "---\nname: mixture-of-loops\ndescription: an older copy\n---\n", encoding="utf-8")
                message = f"{harness}: a different mixture-of-loops skill is visible: {impostor}/SKILL.md"
                checked = box.onboard(harness, "repo", "--check")
                self.assertEqual(checked.returncode, 6, checked.stdout)
                self.assertIn(message, checked.stdout)
                everything = box.onboard("all", "repo", "--check")
                self.assertEqual(everything.returncode, 6, everything.stdout)
                self.assertIn(message, everything.stdout)

                # installation refuses and leaves no link behind; --check still names both problems
                target = box.repo / (".pi/skills" if harness == "pi" else ".prime/agent/skills") / "mixture-of-loops"
                target.unlink()
                refused = box.onboard(harness, "repo")
                self.assertEqual(refused.returncode, 6, refused.stdout)
                self.assertIn("refusing to link", refused.stdout)
                self.assertFalse(target.exists() or target.is_symlink())
                missing = box.onboard(harness, "repo", "--check")
                self.assertEqual(missing.returncode, 5, missing.stdout)
                self.assertIn(message, missing.stdout)

    def test_a_second_link_to_the_canonical_package_is_not_a_collision(self) -> None:
        box = self.sandbox()
        self.assertEqual(box.onboard("codex", "repo").returncode, 0)
        extra = box.home / ".agents/skills/alias"
        extra.parent.mkdir(parents=True)
        extra.symlink_to(SKILL)
        for harness in ("pi", "prime"):
            self.assertEqual(box.onboard(harness, "user").returncode, 0)
            checked = box.onboard(harness, "user", "--check")
            self.assertEqual(checked.returncode, 0, checked.stdout)
            self.assertNotIn("different", checked.stdout)

    def test_pi_check_is_not_ok_for_an_untrusted_project(self) -> None:
        box = self.sandbox()
        self.assertEqual(box.onboard("pi", "repo").returncode, 0)
        untrusted = box.onboard("pi", "repo", "--check")
        self.assertEqual(untrusted.returncode, 7, untrusted.stdout)
        self.assertIn(f"pi: not ok for headless runs: project {box.repo} is untrusted", untrusted.stdout)
        self.assertIn("pass -a/--approve, or add a trust entry yourself", untrusted.stdout)
        self.assertFalse((box.home / ".pi/agent/trust.json").exists(), "the linker never writes trust")

        # `all` reports the same state as a warning and keeps exit 0 (see README)
        self.assertEqual(box.onboard("codex", "repo").returncode, 0)
        everything = box.onboard("all", "repo", "--check")
        self.assertEqual(everything.returncode, 5, everything.stdout)  # claude link still missing
        self.assertEqual(box.onboard("claude", "repo").returncode, 0)
        everything = box.onboard("all", "repo", "--check")
        self.assertEqual(everything.returncode, 0, everything.stdout)
        self.assertIn("pi: warning: not ok for headless runs", everything.stdout)

        agent = box.home / ".pi/agent"
        agent.mkdir(parents=True, exist_ok=True)
        (agent / "trust.json").write_text(json.dumps({str(box.base): True}), encoding="utf-8")
        trusted = box.onboard("pi", "repo", "--check")
        self.assertEqual(trusted.returncode, 0, trusted.stdout)
        self.assertIn(f"trust.json entry {box.base}", trusted.stdout)  # nearest ancestor

        (agent / "trust.json").write_text(json.dumps({str(box.base): True, str(box.repo): False}), encoding="utf-8")
        self.assertEqual(box.onboard("pi", "repo", "--check").returncode, 7)

        (agent / "trust.json").unlink()
        (agent / "settings.json").write_text(json.dumps({"defaultProjectTrust": "always"}), encoding="utf-8")
        always = box.onboard("pi", "repo", "--check")
        self.assertEqual(always.returncode, 0, always.stdout)
        self.assertIn("defaultProjectTrust=always", always.stdout)

        custom = box.base / "custom-pi"
        custom.mkdir()
        (custom / "trust.json").write_text(json.dumps({str(box.repo): True}), encoding="utf-8")
        overridden = box.onboard("pi", "repo", "--check", PI_CODING_AGENT_DIR=str(custom))
        self.assertEqual(overridden.returncode, 0, overridden.stdout)

    def test_prime_has_no_trust_caveat(self) -> None:
        box = self.sandbox()
        self.assertEqual(box.onboard("prime", "repo").returncode, 0)
        checked = box.onboard("prime", "repo", "--check")
        self.assertEqual(checked.returncode, 0, checked.stdout)
        self.assertNotIn("trust", checked.stdout)

    # T014: --repair replaces only symlinks.
    def test_repair_replaces_a_stale_link_and_refuses_a_real_directory(self) -> None:
        for harness, scope in (("pi", "user"), ("pi", "repo"), ("prime", "user"), ("prime", "repo")):
            with self.subTest(harness=harness, scope=scope):
                box = self.sandbox()
                self.assertEqual(box.onboard(harness, scope).returncode, 0)
                target = next(Path(p) for p in (box.home / ".pi/agent/skills/mixture-of-loops",
                                                box.home / ".prime/agent/skills/mixture-of-loops",
                                                box.repo / ".pi/skills/mixture-of-loops",
                                                box.repo / ".prime/agent/skills/mixture-of-loops")
                              if Path(p).is_symlink())
                target.unlink()
                target.symlink_to(box.base)
                stale = box.onboard(harness, scope)
                self.assertEqual(stale.returncode, 4, stale.stdout)
                repaired = box.onboard(harness, scope, "--repair")
                self.assertEqual(repaired.returncode, 0, repaired.stdout)
                self.assertCanonicalLink(target)

                target.unlink()
                target.mkdir()
                (target / "keep.txt").write_text("user data\n", encoding="utf-8")
                refused = box.onboard(harness, scope, "--repair")
                self.assertEqual(refused.returncode, 4, refused.stdout)
                self.assertIn("refusing to replace existing path", refused.stdout)
                self.assertFalse(target.is_symlink())
                self.assertEqual((target / "keep.txt").read_text(encoding="utf-8"), "user data\n")

    # T015: --harness all creates exactly the documented set.
    def test_all_creates_exactly_the_documented_links(self) -> None:
        user_set = {"home/.agents/skills/mixture-of-loops", "home/.claude/skills/mixture-of-loops"}
        repo_set = {"repo/.agents/skills/mixture-of-loops", "repo/.claude/skills/mixture-of-loops"}
        variations = [{}, {"PI_CODING_AGENT_DIR": "AGENT-pi"}, {"PRIME_AGENT_CODING_AGENT_DIR": "AGENT-prime"},
                      {"PI_CODING_AGENT_DIR": "AGENT-pi", "PRIME_AGENT_CODING_AGENT_DIR": "AGENT-prime"}]
        for scope, expected in (("user", user_set), ("repo", repo_set)):
            for extra in variations:
                with self.subTest(scope=scope, extra=extra):
                    box = self.sandbox()
                    extra = {key: str(box.base / value) for key, value in extra.items()}
                    result = box.onboard("all", scope, **extra)
                    self.assertEqual(result.returncode, 0, result.stdout)
                    self.assertEqual(set(links(box.base)), expected)
                    for relative, target in links(box.base).items():
                        self.assertEqual(Path(target), SKILL)
                    for harness in ("dsh", "pi", "prime"):
                        self.assertRegex(result.stdout, rf"{harness}: shared through the .*\.agents/skills link")
                    for value in extra.values():
                        self.assertFalse(Path(value).exists(), "no native pi/prime link under `all`")
                    checked = box.onboard("all", scope, "--check", **extra)
                    self.assertEqual(checked.returncode, 0, checked.stdout)

    def test_existing_harness_links_are_unchanged(self) -> None:
        expected = {
            ("codex", "user"): "home/.agents/skills/mixture-of-loops",
            ("claude", "user"): "home/.claude/skills/mixture-of-loops",
            ("dsh", "user"): "home/.dsh/skills/mixture-of-loops",
            ("codex", "repo"): "repo/.agents/skills/mixture-of-loops",
            ("claude", "repo"): "repo/.claude/skills/mixture-of-loops",
            ("dsh", "repo"): "repo/.dsh/skills/mixture-of-loops",
        }
        for (harness, scope), relative in expected.items():
            with self.subTest(harness=harness, scope=scope):
                box = self.sandbox()
                result = box.onboard(harness, scope)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertEqual(set(links(box.base)), {relative})
                self.assertEqual(result.stdout.strip(), f"{harness}: linked {box.base / relative} -> {SKILL}")
        box = self.sandbox()
        moved = box.onboard("all", "user", DSH_AGENTS_HOME=str(box.base / "dsh-agents"))
        self.assertEqual(moved.returncode, 0, moved.stdout)
        self.assertEqual(set(links(box.base)), {
            "home/.agents/skills/mixture-of-loops", "home/.claude/skills/mixture-of-loops",
            "home/.dsh/skills/mixture-of-loops"})

    # T016: linters.
    def test_onboard_script_passes_bash_n_and_shellcheck(self) -> None:
        syntax = subprocess.run(["bash", "-n", str(ONBOARD)], capture_output=True, text=True, check=False)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        if shutil.which("shellcheck") is None:
            self.skipTest("shellcheck is not installed")
        lint = subprocess.run(["shellcheck", str(ONBOARD)], capture_output=True, text=True, check=False)
        self.assertEqual(lint.returncode, 0, lint.stdout)


if __name__ == "__main__":
    unittest.main()
