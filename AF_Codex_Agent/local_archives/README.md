# local_archives

Only for **HADOOP-12588**: put the substitute `HADOOP-12588.zip` here and the pipeline uses it
instead of the Zenodo URL in `test_config.csv` (the Zenodo forcing does not make the test fail).

- Used by: `AF_Codex_Agent/agentic/run_agentic_td.sh`.
- It replaces any cached/staged copy of HADOOP-12588; the log says `Using local archive ...`.
- Every other container is downloaded from Zenodo exactly as before, even if a zip with its name is put here.
- Folder can be overridden with `LOCAL_ARCHIVES_DIR`.
- `*.zip` files here are git-ignored (too large for GitHub); copy the zip to the machine that runs the pipeline.
