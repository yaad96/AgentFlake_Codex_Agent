# Codex Agent Internals

This project uses the Codex CLI as the repair agent for flaky-test
containers. The user-facing run manual is the top-level `README.md`; this file
only explains what happens inside one run.

## Entry Point

Run from the repository root:

```bash
.venv/bin/python AF_Codex_Agent/agentic/run_agentic.py <container> \
  --runs 1 \
  --models codex \
  --max-iterations 5
```

`run_agentic.py` reads `AF_Codex_Agent/test_config.csv`, detects the test type,
and routes to one of:

| Type | Script |
|---|---|
| `id` | `run_agentic_id.sh` |
| `od` | `run_agentic_od.sh` |
| `nio` | `run_agentic_nio.sh` |
| `td` | `run_agentic_td.sh` |

## API Key

The key is read from **`AF_Codex_Agent/.openai_api_key` and nowhere else** --
its first non-comment, non-blank line.

An `OPENAI_API_KEY` exported in the shell is **ignored**, and every entry point
says so when it sees one:

```
[setup] NOTE: exported OPENAI_API_KEY ignored; using .../.openai_api_key
```

This is deliberate. The old order was env-first, file-as-fallback, and a stale
key exported from `~/.zshrc` silently shadowed the file: every run billed an
exhausted account while the file being edited -- and every manual `curl` test
-- used a working one. The symptom was "no credits" on an account that had
credits, and it cost two days.

To change the key, edit the file. Nothing else works. `setup.sh` will not
overwrite an existing key file; it only seeds one that is absent.

Check which key is live, without printing it:

```bash
head -1 AF_Codex_Agent/.openai_api_key | tr -d '[:space:]' | wc -c   # expect 164
```

Pre-flight the account before a long batch (free, bills nothing):

```bash
KEY=$(head -1 AF_Codex_Agent/.openai_api_key | tr -d '[:space:]')
curl -sS -o /tmp/r.json -w 'http=%{http_code}\n' \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.4","input":"hi","max_output_tokens":16}' \
  https://api.openai.com/v1/responses; cat /tmp/r.json
```

`200` = ready. `401` = wrong or truncated key. `429 credit_balance_exhausted` =
the account has no credits (note this bills nothing, so it leaves **no usage
record** -- an empty usage dashboard is consistent with this error, not
evidence against it).

The key file is ignored by Git.

## Run Layout

Each invocation creates the next run directory:

```text
AF_Codex_Agent/data/<container>/run_<NN>/
  codex_inputs/
    prompt_user.txt
    prompt_system.txt
    trace_config.json
  codex_outputs/
    trial.ndjson
    codex.stderr
    thinking.txt
    tool_calls.jsonl
    usage.json
    patch.diff
    llm_response.json
    apply_report.json
    verify_after_fix.log
    verify_after_fix.verdict
    verify_after_fix.result.json
    run_verdict.txt
    td_validation/                 # TD only: immutable attempts + oracle audit
      aggregate.json
      oracle_manifest.json
      calibration.json
      composition.json
      victim_oracle_audit.json
      runs/attempt_<NN>.{json,log}
    meta.json
  pipeline.log
  .run_complete
```

Large source folders such as `Flaky/`, `Fixed/`, `FixedCodeChange/`, `Flakym2/`,
and `FlakyCodeChange/` are removed after completed `PASSED` or `FAILED` runs.

## Pipeline

1. The per-type shell script unzips the target project into
   `data/<container>/run_<NN>/`.
2. It starts the Docker container as `tm_<container>`. For TD, the setup
   container is replaced before Codex starts with narrow mounts for only the
   editable `Flaky/` tree, read-only prompt/reproduction inputs, writable
   outputs, and the Maven cache. Developer-fixed/reference trees are never
   visible to Codex.
3. It reproduces the flaky failure and writes the baseline log.
4. `agentic_codex_cli.py` builds `prompt_user.txt` and `prompt_system.txt`.
5. Codex runs inside Docker from `/app/work/Flaky`.
6. The driver captures Codex's in-place edits as `codex_outputs/patch.diff`.
7. The tree is restored from a protected baseline.
8. `apply_fix.py` applies the patch.
9. For TD, the evaluator privately builds and calibrates the four-tree semantic
   oracle (`B`, `P`, `F`, `FP`), composes both applicable timing contexts over
   a frozen candidate snapshot, and exposes only one disposable execution tree
   to Docker at a time.
10. `agentic_verify.py` runs each official attempt using fresh Surefire XML (or
    strict text evidence when XML is unavailable) and writes the final verdict.

The final verifier is authoritative. Codex's own self-verification is useful
for search, but it is not the final score.

The public verdict contract is binary: `run_verdict.txt`,
`verify_after_fix.verdict`, per-attempt `verdict` fields, and CSV verdict columns
contain only `PASSED` or `FAILED`. Infrastructure or insufficient evidence is
recorded as an internal diagnostic and maps fail-closed to public `FAILED`.

## Codex Command

The driver runs this shape of command inside Docker:

```bash
export CODEX_HOME="$(mktemp -d)"
cp /app/work/codex_inputs/prompt_system.txt "$CODEX_HOME/AGENTS.md"
printenv OPENAI_API_KEY | codex login --with-api-key

cd /app/work/Flaky
codex exec "$(cat /app/work/codex_inputs/prompt_user.txt)" \
  --model gpt-5.4 \
  -c model_reasoning_effort=high \
  --dangerously-bypass-approvals-and-sandbox \
  --skip-git-repo-check \
  --json
```

Important details:

- **System prompt.** Codex has no `--append-system-prompt`. The system prompt is
  written to `$CODEX_HOME/AGENTS.md`, which Codex reads as global instructions.
  It deliberately does *not* go into `Flaky/AGENTS.md`: a file inside the
  checkout would be picked up by the `git diff` patch capture and contaminate
  every candidate patch.
- **`CODEX_HOME`** is a fresh temporary directory per run, so auth and session
  state never leak between containers.
- **`--dangerously-bypass-approvals-and-sandbox`.** The Docker container *is*
  the sandbox. Codex's own Landlock/seccomp layer is redundant here and can
  break Maven when nested inside Docker.
- **`--skip-git-repo-check`** is required, not optional. The driver deletes
  `Flaky/.git` and keeps the baseline repository at an external `GIT_DIR` the
  agent cannot see, so the agent's working directory is deliberately not a Git
  repository — and `codex exec` refuses to start in one by default.
- **Auth** uses `codex login --with-api-key` reading from stdin, because a VM or
  container has no browser for the interactive `codex login` flow.
- **No turn or cost cap.** `codex exec` runs a single turn whose internal tool
  loop is unbounded, and Codex exposes no spend ceiling. `--max-iterations` is
  still accepted for compatibility but is logged and ignored; the effective
  bound on a run is `--cli-timeout-s` (enforced by `timeout` inside the
  container).
- **Model choice.** The default is `gpt-5.4` (released 2026-03-05), chosen as
  the closest contemporary of Claude Sonnet 4.6 (2026-02-17) so the Codex and
  Claude runs are a like-for-like comparison. `codex debug models` marks it
  `visibility: hide` (superseded by gpt-5.5 / gpt-5.6-*) while it remains
  available in the API. Newer models are stronger but change the comparison.
- **A wrong model id does not fail loudly.** Codex logs "Model metadata for
  `<slug>` not found. Defaulting to fallback metadata" and runs anyway. The
  driver treats that as a broken run and refuses to score it. Verify any new
  slug against `codex debug models` before adding it to `CODEX_MODELS`.
- Usage details are stored in `codex_outputs/usage.json` and summarized by
  `run_agentic_pass_at_k.py`.

## Stream Artifacts

`codex exec --json` emits JSON Lines to `codex_outputs/trial.ndjson`. The driver
splits it into three views:

| Artifact | Source |
|---|---|
| `thinking.txt` | `reasoning` items (**summaries**, not raw chain-of-thought) |
| `agent_messages.txt` | `agent_message` items — the agent's own narrative of what it did and why |
| `tool_calls.jsonl` | `command_execution`, `file_change`, `mcp_tool_call`, `web_search` items |
| `usage.json` | `turn.completed` usage totals + driver wall-clock, plus `error` / `item_errors` |

Note that `thinking.txt` holds reasoning *summaries*. OpenAI does not expose raw
chain-of-thought, so this artifact is not directly comparable to the raw
thinking traces captured by the Claude-based variant of this pipeline.

**Reasoning summaries must be requested explicitly.** Under Codex's default the
model still reasons — runs bill `reasoning_output_tokens` — but emits no
`reasoning` stream items, so `thinking.txt` comes out empty. This was observed
on a real gpt-5.4 run at `effort=high`: 881 reasoning tokens billed, zero
reasoning items. `agentic_config.MODEL_REASONING_SUMMARY` (default
`"detailed"`) is forwarded as `-c model_reasoning_summary=` to surface them.
`agent_messages.txt` is captured regardless and is the fallback trace.

## Reporting

The core CSV keys are unchanged — `container`, `test_type`, `model`, `run`,
`verdict` / `final verdict`, the `validation_*` family, and the token totals —
so summaries stay comparable across pipelines. Some columns are Codex-shaped:

| Column | Meaning |
|---|---|
| `agent_actions` | Total tool calls the agent made. Replaces `agentic_iterations`, which no longer described anything real: `codex exec` is a single turn with an unbounded tool loop, so there is no iteration count to report. |
| `commands_run` / `files_changed` | Breakdown of `agent_actions` — the reproduce/verify loop vs. actual edits. |
| `reasoning_chunks` | Number of reasoning summary items. **Zero unless `model_reasoning_summary` is set** — see below. |
| `agent_messages` | Number of agent narrative messages (also written to `agent_messages.txt`). |
| `codex_turns` | Raw turn count, kept for completeness (normally 1). |
| `reasoning_effort` | Replaces `temperature`. Codex has no temperature knob, so reporting one would be fiction. Legacy rows keep their `temperature` value; new rows leave it blank. |
| `agent_error` | Stream-level errors, deduped and length-capped. Empty on a clean run. |

Token accounting is computed once, in `parse_stream`, and the CSV reads those
values rather than recomputing — so the artifact and the summary cannot drift:

```
billed_input_tokens = input_tokens + cache_write_input_tokens
total_tokens        = billed_input_tokens + output_tokens
```

`cached_input_tokens` is a *subset* of `input_tokens` and is deliberately not
added; `reasoning_output_tokens` is likewise a subset of `output_tokens`.

## Common Debug Flags

All optional; pass them to `run_agentic.py` (they are forwarded through
`run_agentic_pass_at_k.py` to the per-type shell script and the CLI driver):

```bash
--cli-timeout-s 2400      # Codex wall-clock timeout
--verify-pass-runs 10     # extra successful verification runs required
--force-rebuild-image     # rebuild the Docker image even if one exists
```

`KEEP_CONTAINER=1` remains an environment variable, honoured when a per-type
shell script is invoked directly, and keeps `tm_<container>` after the run.

## Summaries

After runs finish, summary files are updated:

```text
AF_Codex_Agent/Complete_Containers_Summary.csv
AF_Codex_Agent/data/<container>/summary.csv
```
