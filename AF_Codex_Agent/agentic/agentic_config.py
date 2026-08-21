"""
agentic_config.py — central configuration for the agentic repair pipeline.

Edit this file to tune behaviour, set API keys, and choose model versions.
Every value here can also be overridden at run time via CLI flags or env vars
(env vars always take precedence over values set in this file):

  CLI flags:  --max-iterations, --model   on run_agentic.py / agentic_codex_cli.py
  Env vars:   AGENTIC_MAX_ITERATIONS, AGENTIC_MODEL, OPENAI_API_KEY
"""

from pathlib import Path

_CONFIG_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _CONFIG_DIR.parent
OPENAI_API_KEY_FILE = _PROJECT_DIR / ".openai_api_key"

def _read_secret_file(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            value = line.strip()
            if value and not value.startswith("#"):
                return value
    except FileNotFoundError:
        return ""
    return ""

# ===========================================================================
# API KEYS
# Put your OpenAI key in AF_Codex_Agent/.openai_api_key, or export
# OPENAI_API_KEY in the shell. Environment variables always win.
# Leave the file empty to rely only on the environment variable.
# ===========================================================================

OPENAI_API_KEY: str = _read_secret_file(OPENAI_API_KEY_FILE)   # "sk-..." 


# ===========================================================================
# MODEL VERSIONS
# run_agentic.py resolves short aliases (e.g. "codex" →
# CODEX_MODELS["codex"]) using this dict.
# Update the IDs here when a new model version is released.
# ===========================================================================

CODEX_MODELS: dict = {
    # short alias      → model id passed to `codex exec --model`
    "codex":           "gpt-5.4",          # default alias
    "gpt-5.4":         "gpt-5.4",
    "gpt-5.1-codex":   "gpt-5.1-codex",
    "gpt-5-codex":     "gpt-5-codex",
}
# Unknown values are passed through to `codex --model` unchanged rather than
# rejected: the set of models an account can use changes over time and is
# authoritatively listed by `codex models`. A hard allow-list here would
# silently block a newly released model.

# Reasoning effort forwarded via `-c model_reasoning_effort=...`.
# One of: "minimal" | "low" | "medium" | "high".
MODEL_REASONING_EFFORT: str = "high"

# Default model used when --model is not passed on the CLI.
DEFAULT_MODEL: str = "gpt-5.4"


# ===========================================================================
# ITERATION LIMITS
# ===========================================================================

MAX_ITERATIONS: int = 75
# Hard cap on submit_patch attempts per container run.
# The agent may call as many read-only context tools as it likes per
# iteration; this only counts the terminal "submit a fix" action.
# Typical range: 3–20.

MAX_TOOL_TURNS_PER_ITERATION: int = 20
# Maximum API round-trips (context-tool calls) within a single iteration
# before the orchestrator gives up waiting for a submit_patch.
# Guards against a runaway exploration loop.

VERIFY_PASS_RUNS: int = 10
# After a patch first passes, run the verification command this many more
# times before declaring success. All runs must pass — if any fail, Flaky/
# is restored and the agent is told the fix is still non-deterministic.
# Set to 1 to accept the first passing run as sufficient.


# ===========================================================================
# API CALL SETTINGS
# ===========================================================================

MAX_TOKENS: int = 8192
# Maximum completion tokens per API call.
# 8192 is fine for Sonnet; raise to 16384 for Opus if you need longer diffs.

TEMPERATURE: float = 0.0
# Sampling temperature. 0.0 = deterministic (greedy).
# Values above 0.3 tend to produce noisier patches without quality gains.


# ===========================================================================
# TOOL OUTPUT
# ===========================================================================

TOOL_OUTPUT_MAX_CHARS: int = 16_000
# Per-tool-call output cap in characters. Results beyond this limit are
# truncated and a notice appended. Prevents a large file from blowing
# the context window.
