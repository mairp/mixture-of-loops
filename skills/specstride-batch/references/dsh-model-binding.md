# Binding a model on dsh (DeepSeek Harness)

`dsh` (`/usr/bin/dsh`) has no `--model` flag. The model is `agent-default-model` in
`$DSH_HOME/settings.yaml`:

```yaml
agent-default-model:
  provider: zai          # a key under llm-pi-ai.providers in ~/.dsh/settings.yaml
  model: glm-5.3         # an id listed under that provider's models
  reasoningEffort: max   # one of the levels the provider declares for that model
```

`dsh --profile headless --patch <file>` with an `agent-default-model` entry shows up in
`--dump-config` but **loses to settings.yaml at run time** (verified twice on this host:
2026-08-31 and 2026-09-23 — the patched run answered from the settings' model). So
`run-batch.sh` builds a throwaway home per batch:

1. `mktemp -d` → `$OVERLAY`
2. symlink every entry of `~/.dsh` into it except `settings.yaml` (profiles, skills,
   sessions, `.credentials.yaml` — nothing is copied or installed)
3. write `$OVERLAY/settings.yaml` = the new `agent-default-model` block + the real file
   minus its old block
4. run with `DSH_HOME=$OVERLAY DSH_PERMISSION_MODE=danger-full-access`
5. remove `$OVERLAY` on exit

Because `sessions/` is a symlink, evidence still lands in
`~/.dsh/sessions/<cwd-slug>/session-*/session.jsonl(.zstd)` (`zstd -dc`). Confirm the binding
there, never from the config dump:

```bash
zstd -dc ~/.dsh/sessions/--root-specstride-claude--/session-<id>/session.jsonl.zstd \
  | grep -o '"provider":"[^"]*","model":"[^"]*"\|reasoningEffort[^,}]*' | sort -u
```

Providers declared on this host (2026-09-30): `zai` (GLM 4.5 … 5.3, 5.3-flash; 5.2/5.3 accept
`max`), `local-high` (llama-swap :8081: qwen3.8-27b-q5, qwen3.6-27b, qwen3-coder-30b-a3b,
nemotron-lightning-30b, muse-glimmer-30b — `high` only), `compass-*` shims. Anything else
needs `--provider` and must exist in `~/.dsh/settings.yaml` first.

Skills: dsh reads `$DSH_HOME/skills` (symlinked → `~/.dsh/skills`, which holds the
`speckit-*` set) plus the repo's `.dsh/skills` and `.agents/skills`. Observed 2026-09-30 on
four headless runs: when the prompt names the skill ("Use the speckit-analyze skill:
/speckit-analyze …"), dsh injects the SKILL.md body into the user message and no `skill`
tool call appears in the session JSONL — the agent goes straight to
`check-prerequisites.sh`. So verify a run by that call and by the findings section in the
log, not by a `tool/call` named `skill`.
