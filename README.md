# AgentFlake Codex Agent

Codex CLI pipeline for repairing flaky Java tests. The tool stages a flaky test,
reproduces the failure inside Docker, asks Codex to edit the project, captures
Codex's patch, verifies the patch from a clean baseline, and archives the full run
under `AF_Codex_Agent/data/<test>/run_<NN>/`. The examples below cover the ID, OD,
NIO, and TD flaky-test categories.

## Requirements

- Docker installed and running.
- An OpenAI API key.
- Linux and macOS are supported.

The repository installs its own Python dependencies and builds its own Docker
images. Codex CLI is installed inside the project images: the run scripts build
the needed image from the included Dockerfile when the image is missing, or when
an existing local image does not contain `codex`.

## Setup

From the repo root, create a file `.openai_api_key` and store your API key there.
The key is read from that file during a run. The file is git-ignored, so it is safe.

## Basic Run

Run from the repository root with the venv interpreter, passing the test name:

```bash
.venv/bin/python AF_Codex_Agent/agentic/run_agentic.py <test> \
  --runs 1 \
  --models codex \
```


## Model Aliases

Aliases are defined in `AF_Codex_Agent/agentic/agentic_config.py`.

| Alias | Model |
|---|---|
| `codex`, `gpt-5.4` | `gpt-5.4` |
| `gpt-5.4-mini` | `gpt-5.4-mini` |
| `gpt-5.2` | `gpt-5.2` |
| `gpt-5.5` | `gpt-5.5` |


## Examples

### ID

```bash
.venv/bin/python AF_Codex_Agent/agentic/run_agentic.py \
  commonslang1163e17testReflectionHashCodeExcludeFields \
  --runs 1 --models codex --max-iterations 75
```

Run data for this test is in
`AF_Codex_Agent_Data.zip/ID/commonslang1163e17testReflectionHashCodeExcludeFields`.

### OD

```bash
.venv/bin/python AF_Codex_Agent/agentic/run_agentic.py \
  jnrposixd9f3f84 \
  --runs 1 --models codex --max-iterations 75
```

Run data for this test is in `AF_Codex_Agent_Data.zip/OD/jnrposixd9f3f84`.

### NIO

```bash
.venv/bin/python AF_Codex_Agent/agentic/run_agentic.py \
  quickcheckc1c1 \
  --runs 1 --models codex --max-iterations 75
```

Run data for this test is in `AF_Codex_Agent_Data.zip/NIO/quickcheckc1c1`.

### TD

```bash
.venv/bin/python AF_Codex_Agent/agentic/run_agentic.py \
  BOOKKEEPER-846 \
  --runs 1 --models codex --max-iterations 75
```

Run data for this test is in `AF_Codex_Agent_Data.zip/TD/BOOKKEEPER-846`.

## Options

The values shown below are those defaults.

| Option / env var | Purpose |
|---|---|
| `--runs N` | Independent runs for pass@k, which counts a test as repaired if at least one of the N independently sampled runs yields a verified fix. |
| `--models codex,gpt-5.5` | One or more Codex models. Each model runs independently and archives to its own subdirectory. |
| `--max-iterations 75` | Max Codex turns per run. |
| `--verify-pass-runs 10` | Extra passing verification runs required after the first pass. |
| `--cli-timeout-s 2400` | Wall-clock cap for Codex. |
| `--force-rebuild-image` | Rebuild the Docker image for a single run. |
| `AGENTIC_REASONING_EFFORT` | Reasoning effort for the run: `default`, `low`, `medium`, `high`, `xhigh`, `max`, `ultra`. |

## Output

Each run is archived under:

```text
AF_Codex_Agent/data/<test>/run_<NN>/
  codex_inputs/
    prompt_user.txt
    prompt_system.txt
    trace_config.json
  codex_outputs/
    trial.ndjson
    codex.stderr
    tool_calls.jsonl
    thinking.txt
    agent_messages.txt
    usage.json
    patch.diff
    llm_response.json
    apply_report.json
    verify_after_fix.log
    verify_after_fix.verdict
    run_verdict.txt
    meta.json
    validation/
  pipeline.log
  .run_complete
```

The verdict in `run_verdict.txt` is `PASSED` or `FAILED`.


Summaries are written to:

```text
AF_Codex_Agent/data/<test>/summary.csv
AF_Codex_Agent/Complete_Containers_Summary.csv
```

All run data is available in `AF_Codex_Agent_Data.zip`, covering 41 OD, 41 ID,
41 NIO, and 41 TD tests.
