"""
agentic_config.py — central configuration for the agentic repair pipeline.

Edit this file to tune behaviour, set API keys, and choose model versions.
Every value here can also be overridden at run time via CLI flags or env vars
(env vars always take precedence over values set in this file):

  CLI flags:  --max-iterations, --model   on run_agentic.py / agentic_claude_cli.py
  Env vars:   AGENTIC_MAX_ITERATIONS, AGENTIC_MODEL, ANTHROPIC_API_KEY
"""

from pathlib import Path

_CONFIG_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _CONFIG_DIR.parent
ANTHROPIC_API_KEY_FILE = _PROJECT_DIR / ".anthropic_api_key"

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
# Put your Anthropic key in AF_Codex_Agent/.anthropic_api_key, or export
# ANTHROPIC_API_KEY in the shell. Environment variables always win.
# Leave the file empty to rely only on the environment variable.
# ===========================================================================

ANTHROPIC_API_KEY: str = _read_secret_file(ANTHROPIC_API_KEY_FILE)   # "sk-ant-..."  — used by all claude-* models
OPENAI_API_KEY: str    = ""   # unused by the Claude CLI pipeline


# ===========================================================================
# MODEL VERSIONS
# Canonical model IDs for each provider. run_agentic.py resolves short
# aliases (e.g. "claude" → CLAUDE_MODELS["claude"]) using these dicts.
# Update the IDs here when a new model version is released.
# ===========================================================================

CLAUDE_MODELS: dict = {
    # short alias          → full Anthropic model ID
    "claude":              "claude-sonnet-4-6",   # default alias
    "claude-sonnet":       "claude-sonnet-4-6",
    "sonnet":              "claude-sonnet-4-6",
    "claude-opus":         "claude-opus-4-7",
    "opus":                "claude-opus-4-7",
    "haiku":               "claude-haiku-4-5-20251001",
}

OPENAI_MODELS: dict = {
    # short alias          → full OpenAI model ID
    "openai":              "gpt-4o",              # default OpenAI alias
    "gpt-4o":              "gpt-4o",
    "gpt-4o-mini":         "gpt-4o-mini",
    "gpt-4.1":             "gpt-4.1",
    "gpt-4.1-mini":        "gpt-4.1-mini",
}

# Default model used when --model is not passed on the CLI.
# Must be a key in CLAUDE_MODELS or a full Anthropic model ID.
DEFAULT_MODEL: str = "claude-sonnet-4-6"


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
