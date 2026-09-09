#!/usr/bin/env bash

set -euo pipefail

readonly PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly PYTHON_VERSION="${PYTHON_VERSION:-3.13}"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    cat <<EOF
Usage: ./setup.sh

Create the locked Diagnose Em' All environment and verify its dependencies.
Set PYTHON_VERSION to use a supported Python version other than 3.13.
EOF
    exit 0
fi

if (( $# > 0 )); then
    printf 'Unknown argument: %s\nRun ./setup.sh --help for usage.\n' "$1" >&2
    exit 2
fi

if ! command -v uv >/dev/null 2>&1; then
    printf '%s\n' 'Missing prerequisite: uv' >&2
    printf '%s\n' 'Install it from https://docs.astral.sh/uv/getting-started/installation/ and rerun this script.' >&2
    exit 1
fi

if ! command -v keyring >/dev/null 2>&1; then
    printf '%s\n' 'Missing prerequisite: keyring with the artifacts-keyring backend.' >&2
    printf '%s\n' 'Provision it using your organization-approved workstation setup, then rerun this script.' >&2
    exit 1
fi

if ! keyring_backends="$(keyring --list-backends 2>&1)"; then
    printf '%s\n%s\n' 'Unable to inspect keyring backends:' "$keyring_backends" >&2
    exit 1
fi

if [[ "$keyring_backends" != *artifacts_keyring.ArtifactsKeyringBackend* ]]; then
    printf '%s\n' 'Missing prerequisite: the artifacts-keyring backend is not available.' >&2
    printf '%s\n' 'Provision it using your organization-approved workstation setup, then rerun this script.' >&2
    exit 1
fi

cd "$PROJECT_ROOT"

printf 'Syncing the locked Python %s environment...\n' "$PYTHON_VERSION"
uv sync --locked --python "$PYTHON_VERSION"

printf '%s\n' 'Verifying runtime dependencies...'
uv run --no-sync python -c 'import azure.identity, httpx, pygame, sounddevice, websockets'

printf '\n%s\n' 'Setup complete. Start the game with:'
printf '%s\n' '  uv run --no-sync python -m src.debug_example'