#!/usr/bin/env python3
"""Progress of the OD batch. Reads run artifacts; safe to run mid-batch."""
import csv, json, os, sys, time
from pathlib import Path

REPO = Path(os.environ.get("REPO", Path.home() / "AgentFlake_Codex_Agent"))
DATA = REPO / "AF_Codex_Agent" / "data"
TOTAL = int(os.environ.get("TOTAL", "41"))

passed = failed = incomplete = 0
rows, running = [], []

for cdir in sorted(p for p in DATA.iterdir() if p.is_dir()) if DATA.is_dir() else []:
    for rdir in sorted(cdir.glob("run_*")):
        steps = rdir / "codex_outputs"
        complete = (rdir / ".run_complete").is_file()
        verdict = (steps / "run_verdict.txt").read_text().strip() if (steps / "run_verdict.txt").is_file() else ""
        usage = {}
        if (steps / "usage.json").is_file():
            try: usage = json.loads((steps / "usage.json").read_text())
            except Exception: pass
        tokens = (usage.get("usage") or {}).get("total_tokens", 0) or 0
        is_err = bool(usage.get("is_error"))

        if not complete:
            age = int(time.time() - rdir.stat().st_mtime)
            running.append((cdir.name, rdir.name, age))
            continue

        # infrastructure failure: agent never produced a valid turn
        infra = is_err or (verdict == "FAILED" and tokens == 0)
        if verdict == "PASSED":   passed += 1;     kind = "PASSED"
        elif infra:               incomplete += 1; kind = "INCOMPLETE"
        else:                     failed += 1;     kind = "FAILED"
        rows.append((cdir.name, rdir.name, kind, tokens, rdir.stat().st_mtime))

attempted = passed + failed + incomplete
print(f"PROGRESS  {attempted}/{TOTAL} runs complete   ({TOTAL - attempted} remaining)")
print(f"  PASSED      {passed:>3}")
print(f"  FAILED      {failed:>3}   genuine repair failures")
print(f"  INCOMPLETE  {incomplete:>3}   infrastructure -- NOT a model result, re-run these")
if attempted:
    print(f"  pass rate   {100*passed/attempted:.1f}%  (of completed, excluding incomplete: "
          f"{100*passed/max(1,passed+failed):.1f}%)")

if running:
    print("\nIN PROGRESS")
    for c, r, age in running:
        print(f"  {c[:44]:<46} {r}  {age//60}m{age%60:02d}s")

if incomplete:
    print("\nINCOMPLETE (re-run after fixing the cause)")
    for c, r, k, t, _ in rows:
        if k == "INCOMPLETE":
            print(f"  {c}")

print("\nLAST 8 COMPLETED")
for c, r, k, t, _ in sorted(rows, key=lambda x: -x[4])[:8]:
    print(f"  {k:<11} {c[:44]:<46} {t:>9,} tok")
