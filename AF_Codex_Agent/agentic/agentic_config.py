"""
agentic_config.py — central configuration for the agentic repair pipeline.

Edit this file to tune behaviour, set API keys, and choose model versions.
Every value here can also be overridden at run time via CLI flags or env vars
(env vars always take precedence over values set in this file):

  CLI flags:  --max-iterations, --model   on run_agentic.py / agentic_codex_cli.py
  Env vars:   AGENTIC_MAX_ITERATIONS, AGENTIC_MODEL

The API key is deliberately NOT an env var. See require_openai_api_key().
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

OPENAI_API_KEY: str = _read_secret_file(OPENAI_API_KEY_FILE)


def require_openai_api_key() -> str:
    """The API key, read only from .openai_api_key. Exits if unusable.

    Any OPENAI_API_KEY in the environment is ignored on purpose; when one is
    present and differs from the file, that is announced rather than silently
    preferred, because silently preferring it is exactly the bug this function
    exists to prevent.
    """
    import os
    import sys

    key = (OPENAI_API_KEY or "").strip()
    env_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if env_key and env_key != key:
        print(f"[config] NOTE: OPENAI_API_KEY is set in the environment but is "
              f"being IGNORED (...{env_key[-4:]}); the key is always read from "
              f"{OPENAI_API_KEY_FILE}", file=sys.stderr, flush=True)
    if not key:
        sys.exit(
            f"ERROR: no API key in {OPENAI_API_KEY_FILE}.\n"
            f"       Write the key into that file (first non-comment line).\n"
            f"       Exporting OPENAI_API_KEY will NOT work -- it is ignored "
            f"by design.")
    return key   # "sk-..." 


# ===========================================================================
# MODEL VERSIONS
# run_agentic.py resolves short aliases (e.g. "codex" →
# CODEX_MODELS["codex"]) using this dict.
# Update the IDs here when a new model version is released.
# ===========================================================================

CODEX_MODELS: dict = {
    # short alias   → model id passed to `codex exec --model`
    #
    # Verified against `codex debug models` (Codex CLI 0.149.0). Slugs not in
    # that catalog do NOT fail loudly: Codex logs "Model metadata for <x> not
    # found. Defaulting to fallback metadata" and runs degraded, so a typo here
    # silently corrupts a whole batch. Re-check with `codex debug models`
    # before adding an entry.
    "codex":         "gpt-5.4",       # default alias
    "gpt-5.4":       "gpt-5.4",       # released 2026-03-05
    "gpt-5.4-mini":  "gpt-5.4-mini",  # smaller sibling of the same generation
    "gpt-5.2":       "gpt-5.2",       # released 2025-12-11
    "gpt-5.5":       "gpt-5.5",       # released 2026-04-23
    "gpt-5.6-sol":   "gpt-5.6-sol",   # released 2026-07-09, frontier coding
    "gpt-5.6-terra": "gpt-5.6-terra",
    "gpt-5.6-luna":  "gpt-5.6-luna",
}
# Unknown values are passed through to `codex --model` unchanged rather than
# rejected: the set of models an account can use changes over time and is
# authoritatively listed by `codex models`. A hard allow-list here would
# silently block a newly released model.

# Reasoning effort forwarded via `-c model_reasoning_effort=...`.
# Support is per-model (from `codex debug models`):
#   gpt-5.2 / gpt-5.4 / gpt-5.5      low, medium, high, xhigh
#   gpt-5.6-luna                     ... + max
#   gpt-5.6-sol / gpt-5.6-terra      ... + max, ultra
# Note there is no "minimal" level on any current model.
# "default" is a sentinel, not a level: it omits -c model_reasoning_effort
# entirely and lets the model's own default apply. Per `codex debug models`
# that is currently medium for gpt-5.2/5.4/5.5 and low for gpt-5.6-sol.
#
# Prefer an explicit level for benchmark runs. "default" is whatever the
# provider ships today, so it can change under you between runs and makes a
# published result harder to reproduce.
REASONING_EFFORTS = ("default", "low", "medium", "high", "xhigh", "max", "ultra")
MODEL_REASONING_EFFORT: str = "default"

# Reasoning SUMMARY visibility, forwarded via `-c model_reasoning_summary=...`.
# One of: "auto" | "concise" | "detailed" | "none".
#
# This is not cosmetic. Under the default the model still reasons (runs bill
# reasoning_output_tokens) but emits no `reasoning` stream items at all, so
# thinking.txt comes out EMPTY and the run has no recoverable reasoning trace.
# "detailed" is the setting that actually surfaces one. OpenAI does not expose
# raw chain-of-thought, so this is a summary either way.
REASONING_SUMMARIES = ("auto", "concise", "detailed", "none")
MODEL_REASONING_SUMMARY: str = "detailed"

# Default model used when --model is not passed on the CLI.
#
# gpt-5.4 (2026-03-05) is chosen deliberately as the closest CONTEMPORARY of
# Claude Sonnet 4.6 (2026-02-17), which the Claude variant of this pipeline was
# benchmarked on — 16 days apart, and the same generation. Later models
# (gpt-5.5, gpt-5.6-*) are stronger but would not be a like-for-like
# comparison. `codex debug models` marks gpt-5.4 visibility=hide (superseded)
# while supported_in_api stays true, so it still runs; if OpenAI eventually
# retires it, the comparison has to be re-baselined rather than silently moved
# to a newer model.
DEFAULT_MODEL: str = "gpt-5.4"


# ===========================================================================
# ITERATION LIMITS
# ===========================================================================

# NOTE: `codex exec` runs a single turn with an unbounded internal tool loop
# and exposes no turn cap, so this is NOT enforced. It is kept only so the
# --max-iterations flag keeps parsing; the real bound on a run is
# --cli-timeout-s. See agentic_codex_cli.run_agent_in_container.
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
# LEGACY API CALL SETTINGS — NOT USED BY THE CODEX CLI PATH
#
# These belong to the older direct-API orchestrator. The Codex CLI owns its own
# sampling and tool-output handling, so nothing below is read by
# agentic_codex_cli.py. Reasoning effort (MODEL_REASONING_EFFORT above) is the
# only sampling control this pipeline actually applies. Left in place for the
# archived orchestrator; do not add new readers.
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
