# mixture-of-tasks

`mixture-of-tasks` is one maintained Agent Skills package for Claude Code, Codex,
and DeepSeek Harness (dsh). It reads Spec Kit feature artifacts holistically and
generates a provenance-bound launch contract plus an executable, unattended Wiggum
pipeline.

The canonical skill is [skills/mixture-of-tasks/SKILL.md](skills/mixture-of-tasks/SKILL.md).
All three harnesses should link to that directory so fixes do not drift between copies.

## Use

Invoke the skill from a repository that contains one or more Spec Kit feature directories:

```text
# Codex
$mixture-of-tasks derive a pipeline for specs/007-example

# Claude Code or dsh
/mixture-of-tasks derive a pipeline for specs/007-example
```

The skill inventories the supplied feature artifacts, records their provenance in a launch
contract, validates the contract, and renders a Bash launcher. Generation does not execute
the pipeline unless the request separately asks for execution.

## Requirements

- Linux or macOS local execution
- Python 3.10 or newer (standard library only)
- Bash 4 or newer for the generated launcher
- a Wiggum installation compatible with the options selected in the contract

Cloud-hosted skill synchronization and Windows execution have not been validated by this
package.

## Onboard

Use the included linker from this checkout. It refuses to replace an unrelated existing
skill.

```bash
./bin/onboard-skill --harness codex --scope user
./bin/onboard-skill --harness claude --scope user
./bin/onboard-skill --harness dsh --scope user
```

Install all three for the current user:

```bash
./bin/onboard-skill --harness all --scope user
```

The `all` form uses the shared `.agents/skills` location for both Codex and dsh, which
prevents dsh from discovering the same skill through two default roots. If
`DSH_AGENTS_HOME` points somewhere else, the linker adds the dsh-specific link as well.

For one repository, run from that repository or pass `--repo`:

```bash
./bin/onboard-skill --harness all --scope repo --repo /path/to/project
```

The targets are:

| Harness | User scope | Repository scope | Invocation |
| --- | --- | --- | --- |
| Codex | `~/.agents/skills/mixture-of-tasks` | `.agents/skills/mixture-of-tasks` | `$mixture-of-tasks` |
| Claude Code | `~/.claude/skills/mixture-of-tasks` | `.claude/skills/mixture-of-tasks` | `/mixture-of-tasks` |
| dsh | `${DSH_HOME:-~/.dsh}/skills/mixture-of-tasks` | `.dsh/skills/mixture-of-tasks` | `/mixture-of-tasks` |

Codex and Claude Code both document symlinked skill folders. Codex scans `.agents/skills`
from the working directory to the repository root and `~/.agents/skills`; Claude Code
scans `.claude/skills` at personal and repository scope. These paths were checked against
the [official Codex skill documentation](https://developers.openai.com/codex/skills) and
[official Claude Code skill documentation](https://code.claude.com/docs/en/skills) on
2026-09-09. The dsh paths are verified against the installed dsh 0.1.0-rc.8 filesystem
provider, which scans repository `.dsh/skills` and user `$DSH_HOME/skills` roots and
follows symlinks.

After onboarding, inspect the link and skill without changing it:

```bash
./bin/onboard-skill --harness all --scope user --check
```

If this checkout moves, the absolute symlink breaks. Run the same onboarding command with
`--repair`; it replaces only an existing symlink and refuses to replace a real file or
directory. Do not keep copied harness variants: change the canonical package and let all
three harnesses read it through their links.

Codex normally detects changes automatically; restart if the skill does not appear. In
Claude Code, use `/skills` and then `/mixture-of-tasks`. In dsh, invoke it in the prompt,
for example:

```bash
dsh --profile headless "/mixture-of-tasks derive a pipeline for specs/007-example"
```

## Develop and validate

```bash
python3 -m unittest discover -s tests -v
bash -n bin/onboard-skill
shellcheck bin/onboard-skill
```

The test suite uses temporary repositories and stub commands. It does not call a model,
run Wiggum, provision infrastructure, or install the skill.

Validation on 2026-09-09 covered skill structure, repository-scope discovery links, and
the installed harness layouts (Codex CLI 0.153.4, Claude Code 2.1.266, dsh 0.1.0-rc.8).
End-to-end skill invocation by each model host remains a live integration check because it
would call configured model backends; the package does not label the local layout checks
as a completed model run.
