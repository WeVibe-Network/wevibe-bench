#!/usr/bin/env bash
# Store the OrcaRouter API key for the --cloud bench path.
# The key file is gitignored (config/cloud.env) and written with mode 0600.
# Re-running overwrites. Fails loud on empty input.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEY_FILE="${WEVIBE_BENCH_CLOUD_KEY_FILE:-${REPO_ROOT}/config/cloud.env}"

printf '%s' "Enter your OrcaRouter API key: "
IFS= read -r -s KEY || true
printf '\n'

if [[ -z "${KEY:-}" ]]; then
    echo "error: empty OrcaRouter API key — nothing stored." >&2
    exit 1
fi

umask 077
printf 'ORCAROUTER_API_KEY=%s\n' "${KEY}" > "${KEY_FILE}"
chmod 600 "${KEY_FILE}"
unset KEY
echo "OrcaRouter API key stored at ${KEY_FILE} (mode 0600). Re-run to overwrite."
