# Claude Agent Internals

This project uses the Claude Code CLI as the repair agent for flaky-test
containers. The user-facing run manual is the top-level `README.md`; this file
only explains what happens inside one run.

## Entry Point

Run from the repository root:

```bash
.venv/bin/python AF_Codex_Agent/agentic/run_agentic.py <container> \
  --runs 1 \
  --models claude \
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

The key is loaded in this order:

1. `ANTHROPIC_API_KEY` from the shell.
2. `AF_Codex_Agent/.anthropic_api_key`.

The key file is ignored by Git.

## Run Layout

Each invocation creates the next run directory:

```text
AF_Codex_Agent/data/<container>/run_<NN>/
  claude_inputs/
    prompt_user.txt
    prompt_system.txt
    trace_config.json
  claude_outputs/
    trial.ndjson
    claude.stderr
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
   container is replaced before Claude starts with narrow mounts for only the
   editable `Flaky/` tree, read-only prompt/reproduction inputs, writable
   outputs, and the Maven cache. Developer-fixed/reference trees are never
   visible to Claude.
3. It reproduces the flaky failure and writes the baseline log.
4. `agentic_claude_cli.py` builds `prompt_user.txt` and `prompt_system.txt`.
5. Claude Code runs inside Docker from `/app/work/Flaky`.
6. The driver captures Claude's in-place edits as `claude_outputs/patch.diff`.
7. The tree is restored from a protected baseline.
8. `apply_fix.py` applies the patch.
9. For TD, the evaluator privately builds and calibrates the four-tree semantic
   oracle (`B`, `P`, `F`, `FP`), composes both applicable timing contexts over
   a frozen candidate snapshot, and exposes only one disposable execution tree
   to Docker at a time.
10. `agentic_verify.py` runs each official attempt using fresh Surefire XML (or
    strict text evidence when XML is unavailable) and writes the final verdict.

The final verifier is authoritative. Claude's own self-verification is useful
for search, but it is not the final score.

The public verdict contract is binary: `run_verdict.txt`,
`verify_after_fix.verdict`, per-attempt `verdict` fields, and CSV verdict columns
contain only `PASSED` or `FAILED`. Infrastructure or insufficient evidence is
recorded as an internal diagnostic and maps fail-closed to public `FAILED`.

## Claude Command

The driver runs this shape of command inside Docker:

```bash
claude -p "$(cat /app/work/claude_inputs/prompt_user.txt)" \
  --model claude-sonnet-4-6 \
  --append-system-prompt "$(cat /app/work/claude_inputs/prompt_system.txt)" \
  --permission-mode bypassPermissions \
  --bare \
  --output-format stream-json \
  --verbose \
  --include-partial-messages \
  --max-turns "$AGENTIC_MAX_ITERATIONS" \
  --max-budget-usd "$MAX_BUDGET_USD"
```

Important details:

- `--bare` keeps the run isolated from local Claude project memory.
- `IS_SANDBOX=1` allows `bypassPermissions` inside the Docker container.
- `CLAUDE_CONFIG_DIR` is set to a temporary directory for each run.
- `AGENTIC_MAX_ITERATIONS` is passed to Claude Code as `--max-turns`.
- `--max-budget-usd` is optional but recommended while testing; the flag is
  threaded down from `run_agentic.py` and the `--max-budget-usd` argument is
  omitted entirely when it is not set.
- Usage details are stored in `claude_outputs/usage.json` and summarized by
  `run_agentic_pass_at_k.py`.

## Common Debug Flags

All optional; pass them to `run_agentic.py` (they are forwarded through
`run_agentic_pass_at_k.py` to the per-type shell script and the CLI driver):

```bash
--cli-timeout-s 2400      # Claude Code wall-clock timeout
--verify-pass-runs 10     # extra successful verification runs required
--max-budget-usd 0.50     # hard Claude Code spend cap per run
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
