#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$ROOT_DIR/AF_Codex_Agent"
VENV_DIR="$ROOT_DIR/.venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"
BUILD_IMAGES=0
SKIP_DOCKER_CHECK=0
FORCE_REBUILD_IMAGES=0

usage() {
  cat <<'USAGE'
Usage:
  OPENAI_API_KEY=sk-... bash setup.sh [--build-images]

Options:
  --build-images          Prebuild all Docker images that have Dockerfiles.
  --force-rebuild-images  Rebuild images even when a local image already exists.
  --skip-docker-check     Install Python deps and key file without checking Docker.
  -h, --help              Show this help.

This script installs repo-local dependencies. It does not install Docker Desktop
or create an OpenAI API key; provide those before running a repair.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --build-images) BUILD_IMAGES=1 ;;
    --force-rebuild-images) FORCE_REBUILD_IMAGES=1 ;;
    --skip-docker-check) SKIP_DOCKER_CHECK=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown option: $1" >&2; usage; exit 2 ;;
  esac
  shift
done

echo "[setup] repo: $ROOT_DIR"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "ERROR: python3 is required but was not found on PATH." >&2
  exit 1
fi

if ! "$PYTHON_BIN" -m venv --help >/dev/null 2>&1; then
  cat >&2 <<'MSG'
ERROR: python3 venv support is required but is not available.

On Debian/Ubuntu, install it with:
  sudo apt install python3-venv python3-pip

Then re-run setup.sh.
MSG
  exit 1
fi

if [[ ! -d "$VENV_DIR" ]]; then
  echo "[setup] creating Python virtual environment: .venv"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

echo "[setup] installing Python dependencies"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r "$PROJECT_DIR/requirements.txt"

if [[ -n "${OPENAI_API_KEY:-}" ]]; then
  echo "[setup] storing OpenAI API key in AF_Codex_Agent/.openai_api_key"
  umask 077
  printf '%s\n' "$OPENAI_API_KEY" > "$PROJECT_DIR/.openai_api_key"
elif [[ ! -s "$PROJECT_DIR/.openai_api_key" ]]; then
  cat >&2 <<'MSG'
ERROR: no OpenAI API key found.

Run setup like this:
  OPENAI_API_KEY=sk-... bash setup.sh --build-images

Or create AF_Codex_Agent/.openai_api_key yourself.
MSG
  exit 1
else
  echo "[setup] using existing AF_Codex_Agent/.openai_api_key"
fi

if [[ "$SKIP_DOCKER_CHECK" != "1" ]]; then
  if ! command -v docker >/dev/null 2>&1; then
    cat >&2 <<'MSG'
ERROR: Docker is required but was not found on PATH.

Install and start Docker Desktop on macOS/Windows, or install Docker Engine on
Linux, then re-run this setup command. The repository can build the project
images, but it should not silently install system-level Docker for you.
MSG
    exit 1
  fi

  if ! docker info >/dev/null 2>&1; then
    echo "ERROR: Docker is installed but the daemon is not running." >&2
    exit 1
  fi
fi

if [[ "$BUILD_IMAGES" == "1" ]]; then
  if [[ "$SKIP_DOCKER_CHECK" == "1" ]]; then
    echo "ERROR: --build-images cannot be used with --skip-docker-check" >&2
    exit 1
  fi

  PLATFORM_ARGS=()
  if [[ -n "${AGENTIC_DOCKER_PLATFORM:-}" ]]; then
    PLATFORM_ARGS=(--platform "$AGENTIC_DOCKER_PLATFORM")
  elif [[ "$(uname -s)" == "Darwin" && "$(uname -m)" == "arm64" ]]; then
    PLATFORM_ARGS=(--platform linux/amd64)
  fi

  images=(
    "flaky_base_jdk8|Dockerfile"
    "flaky_base_jdk8_hadoop|Dockerfile.hadoop"
    "flaky_base_jdk8_od_cov|Dockerfile.od"
    "flaky_base_jdk11_od_cov|Dockerfile.od11"
    "flaky_base_jdk_8_id_cover_new|Dockerfile8.id"
    "flaky_base_jdk_11_id_cover_new|Dockerfile11.id"
    "flaky_base_jdk_17_id_cover_new|Dockerfile17.id"
  )

  image_has_codex() {
    docker run --rm --entrypoint sh "$1" -lc 'command -v codex >/dev/null 2>&1'
  }

  for entry in "${images[@]}"; do
    image="${entry%%|*}"
    dockerfile="${entry#*|}"
    if [[ "$FORCE_REBUILD_IMAGES" != "1" ]] && docker image inspect "$image" >/dev/null 2>&1; then
      if image_has_codex "$image"; then
        echo "[setup] image ready: $image"
        continue
      fi
      echo "[setup] image exists but lacks Codex CLI or cannot run: $image"
    elif [[ "$FORCE_REBUILD_IMAGES" == "1" ]] && docker image inspect "$image" >/dev/null 2>&1; then
      echo "[setup] force rebuilding image: $image"
    else
      echo "[setup] image missing: $image"
    fi
    if [[ ! -f "$PROJECT_DIR/$dockerfile" ]]; then
      echo "ERROR: Dockerfile not found: $PROJECT_DIR/$dockerfile" >&2
      exit 1
    fi
    echo "[setup] building $image from $dockerfile"
    docker build "${PLATFORM_ARGS[@]}" -t "$image" -f "$PROJECT_DIR/$dockerfile" "$PROJECT_DIR"
  done
fi

cat <<'DONE'

[setup] done.

Use the venv interpreter when running the tool:
  .venv/bin/python AF_Codex_Agent/agentic/run_agentic.py <container> --runs 1 --models codex

If local Docker images are stale, rebuild them with:
  bash setup.sh --build-images --force-rebuild-images
DONE
