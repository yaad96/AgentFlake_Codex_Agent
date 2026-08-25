#!/usr/bin/env python3
"""
run_agentic.py — central dispatcher for the agentic flaky-test repair pipeline.

Reads the test type from test_config.csv and routes to the correct per-type
shell script via run_agentic_pass_at_k.py. Supports multiple models in a
single invocation; each model runs independently and archives to its own
subdirectory.

Usage:
    python3 run_agentic.py <container> [--models codex] [--runs 3]
                                       [--verify-pass-runs 10]
                                       [--cli-timeout-s 2400]
                                       [--force-rebuild-image]

    # multiple models in one shot:
    python3 run_agentic.py <container> --models gpt-5.4,gpt-5.5 --runs 3

Model aliases are defined in agentic_config.py (CODEX_MODELS).
Common aliases:
    codex          ->  gpt-5.4   (default)
    gpt-5.2 / gpt-5.4 / gpt-5.4-mini / gpt-5.5 / gpt-5.6-sol /
    gpt-5.6-terra / gpt-5.6-luna  ->  themselves
    Any other value is passed through to `codex exec --model` unchanged;
    run `codex debug models` to see what the account can actually use.

The default gpt-5.4 (2026-03-05) is the closest contemporary of Claude Sonnet
4.6 (2026-02-17), the model the Claude variant of this pipeline was
benchmarked on. Newer models are stronger but are not a like-for-like
comparison.

Note: `codex exec` has no turn cap or cost ceiling, so --max-iterations is
accepted but ignored (a warning is logged). The effective bound on a run is
--cli-timeout-s.

The API key is read ONLY from AF_Codex_Agent/.openai_api_key. An exported
OPENAI_API_KEY is deliberately ignored.
"""

from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR     = Path(__file__).resolve().parent
REPROFLAKE_DIR = SCRIPT_DIR.parent
CSV_FILE       = REPROFLAKE_DIR / "test_config.csv"
PASS_AT_K      = SCRIPT_DIR / "run_agentic_pass_at_k.py"

sys.path.insert(0, str(SCRIPT_DIR))
import agentic_config  # type: ignore  # noqa: E402

SUPPORTED_TYPES = {"od", "td", "id", "nio"}


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------

def resolve_model(alias: str) -> tuple[str, str]:
    """Return (model_id, provider) for a Codex alias or raw model ID.

    Known short aliases are expanded via agentic_config.CODEX_MODELS; anything
    else is passed through to `codex exec --model` verbatim. Codex is the
    authority on which model IDs an account may use (`codex models`), so
    rejecting unknown strings here would only block newly released models.
    """
    key = alias.strip().lower()
    if not key:
        sys.exit("ERROR: empty model alias")
    return agentic_config.CODEX_MODELS.get(key, alias.strip()), "openai"


def get_api_key(provider: str = "openai") -> tuple[str, str]:
    """Return (api_key, source) for the Codex CLI backend.

    Always the key file. An exported OPENAI_API_KEY is ignored by design.
    """
    return agentic_config.require_openai_api_key(), "key file"


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def load_csv_row(container: str) -> dict | None:
    if not CSV_FILE.is_file():
        sys.exit(f"ERROR: CSV not found: {CSV_FILE}")
    with open(CSV_FILE, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if (row.get("result_container") or "").strip() == container:
                return row
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Central dispatcher for the agentic flaky-test repair pipeline.",
    )
    ap.add_argument("container",
                    help="result_container name from test_config.csv")
    ap.add_argument("--models", default="codex",
                    help="comma-separated model names/IDs (default: codex). "
                         "Example: gpt-5.4,gpt-5.5")
    ap.add_argument("--runs", type=int, default=3,
                    help="independent runs per model for pass@k (default 3)")
    ap.add_argument("--max-iterations", type=int, default=None,
                    help="accepted for compatibility; codex exec has no turn "
                         "cap, so this is logged and ignored (the effective "
                         "bound is --cli-timeout-s)")
    ap.add_argument("--verify-pass-runs", type=int, default=None,
                    help="extra passing verification runs required after the first pass")
    ap.add_argument("--cli-timeout-s", type=int, default=None,
                    help="wall-clock cap in seconds for Codex")
    ap.add_argument("--force-rebuild-image", action="store_true",
                    help="rebuild the Docker image even if one already exists")
    args = ap.parse_args()

    # ---- validate container ----
    row = load_csv_row(args.container)
    if not row:
        sys.exit(f"ERROR: container '{args.container}' not found in {CSV_FILE.name}")
    test_type = row.get("test_type", "").strip().lower()
    if test_type not in SUPPORTED_TYPES:
        sys.exit(f"ERROR: unsupported test_type '{test_type}' for container "
                 f"'{args.container}'.\n"
                 f"       Supported: {', '.join(sorted(SUPPORTED_TYPES))}")

    victim   = row.get("flaky_test", "").strip()
    polluter = row.get("polluter/state setter", "").strip()
    java_ver = row.get("java", "").strip()
    print(f"[dispatcher] container   = {args.container}")
    print(f"[dispatcher] test_type   = {test_type}")
    print(f"[dispatcher] victim      = {victim}")
    if polluter:
        print(f"[dispatcher] polluter    = {polluter}")
    print(f"[dispatcher] java        = {java_ver}")

    # ---- resolve models and check keys ----
    raw_models = [m.strip() for m in args.models.split(",") if m.strip()]
    if not raw_models:
        sys.exit("ERROR: --models cannot be empty")

    resolved: list[tuple[str, str, str]] = []  # (alias, model_id, provider)
    for alias in raw_models:
        model_id, provider = resolve_model(alias)

        api_key, source = get_api_key(provider)
        if not api_key:
            sys.exit(f"ERROR: no API key for '{model_id}'.\n"
                     "       Put the key in AF_Codex_Agent/.openai_api_key "
                     "(first non-comment line).\n"
                     "       Exporting OPENAI_API_KEY will NOT work -- it is "
                     "ignored by design.")

        resolved.append((alias, model_id, provider))
        key_display = api_key[:8] + "..." + api_key[-4:] if len(api_key) > 12 else "***"
        print(f"[dispatcher] model       = {alias} -> {model_id}  "
              f"(key from {source}: {key_display})")

    if not resolved:
        sys.exit("ERROR: no valid models to run after resolution.")

    print(f"[dispatcher] runs        = {args.runs}")
    print(f"[dispatcher] max-iters   = {args.max_iterations}")

    # Optional per-run knobs, forwarded to run_agentic_pass_at_k.py only when set.
    passthrough = []
    if args.verify_pass_runs is not None:
        passthrough += ["--verify-pass-runs", str(args.verify_pass_runs)]
        print(f"[dispatcher] verify-runs = {args.verify_pass_runs}")
    if args.cli_timeout_s is not None:
        passthrough += ["--cli-timeout-s", str(args.cli_timeout_s)]
        print(f"[dispatcher] cli-timeout = {args.cli_timeout_s}s")
    if args.force_rebuild_image:
        passthrough.append("--force-rebuild-image")
        print("[dispatcher] force-rebuild-image = on")
    print()

    # ---- dispatch once per model ----
    exit_codes: dict[str, int] = {}
    for alias, model_id, provider in resolved:
        print(f"{'='*60}")
        print(f"[dispatcher] Starting model: {model_id}  runs={args.runs}")
        print(f"{'='*60}")

        cmd = [
            sys.executable, str(PASS_AT_K),
            args.container,
            "--runs",           str(args.runs),
            "--max-iterations", str(args.max_iterations),
            "--model",          model_id,
        ] + passthrough

        # Inject API key into the subprocess environment so the shell scripts
        # and orchestrator see it even if it was only set in agentic_config.py.
        env = os.environ.copy()
        api_key, _ = get_api_key(provider)
        env["OPENAI_API_KEY"] = api_key

        proc = subprocess.run(cmd, env=env)
        exit_codes[model_id] = proc.returncode
        status = "OK" if proc.returncode == 0 else f"exit={proc.returncode}"
        print(f"\n[dispatcher] {model_id}: {status}\n")

    # ---- final summary ----
    print(f"{'='*60}")
    print("[dispatcher] All models done.")
    for model_id, rc in exit_codes.items():
        status = "PASSED (≥1 run)" if rc == 0 else "no run passed"
        print(f"  {model_id:40s}  {status}")
    print(f"{'='*60}")

    if any(rc != 0 for rc in exit_codes.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
