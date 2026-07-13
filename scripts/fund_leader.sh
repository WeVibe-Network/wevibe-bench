#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"

# Load durable bench env (throwaway local-dev seeds) for standalone runs.
# When invoked by seed_corpus.py the vars are already in the inherited env.
if [ -f "$REPO_ROOT/config/bench.env" ]; then
  # shellcheck disable=SC1091
  source "$REPO_ROOT/config/bench.env"
fi

: "${WEVIBE_BENCH_LEADER_SEED_HEX:?missing required env WEVIBE_BENCH_LEADER_SEED_HEX}"

LEADER_SIGNER_DIR="${LEADER_SIGNER_DIR:-${WEVIBE_BENCH_LEADER_SIGNER_DIR:-$REPO_ROOT/scaffold/leader-signer}}"
FUND_TARGET_UVIBE="${FUND_TARGET_UVIBE:-100000000}"
FUND_GRANT_UVIBE="${FUND_GRANT_UVIBE:-1000000000}"
CHAIN_CONTAINER="${CHAIN_CONTAINER:-wevibe-chain}"
FAUCET_KEY="${FAUCET_KEY:-faucet}"
CHAIN_ID="${CHAIN_ID:-wevibe-local-1}"
CHAIN_REST="${CHAIN_REST:-http://localhost:1317}"
KEYRING_BACKEND="${KEYRING_BACKEND:-test}"

CHAIN_REST="${CHAIN_REST%/}"

RUNS_DIR="${WEVIBE_BENCH_RUNS_DIR:-$REPO_ROOT/runs}/fund-leader"
RUN_TS="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
LOG_FILE="${RUNS_DIR}/${RUN_TS}.log"
mkdir -p "$RUNS_DIR"
touch "$LOG_FILE"

log() {
  local message="$*"
  printf '%s\n' "$message" | tee -a "$LOG_FILE"
}

compact_ws() {
  printf '%s' "$1" | tr '\n\r' ' '
}

fail() {
  local message="$*"
  log "op=fund_leader.err ${message}"
  exit 1
}

require_uint() {
  local name="$1"
  local value="$2"
  if [[ ! "$value" =~ ^[0-9]+$ ]]; then
    fail "field=${name} reason=not-uint value=${value}"
  fi
}

fetch_uvibe_balance() {
  local addr="$1"
  local endpoint="${CHAIN_REST}/cosmos/bank/v1beta1/balances/${addr}"
  local payload
  payload="$(curl --silent --show-error --fail "$endpoint")"
  python3 -c '
import json
import sys

payload = json.loads(sys.stdin.read())
balances = payload.get("balances")
if balances is None:
    print(0)
    raise SystemExit(0)
if not isinstance(balances, list):
    raise SystemExit("balances field is not an array")

for coin in balances:
    if isinstance(coin, dict) and coin.get("denom") == "uvibe":
        amount = coin.get("amount", "0")
        if isinstance(amount, int):
            print(amount)
            raise SystemExit(0)
        if isinstance(amount, str) and amount.isdigit():
            print(int(amount))
            raise SystemExit(0)
        raise SystemExit(f"uvibe amount is not numeric: {amount!r}")

print(0)
' <<<"$payload"
}

require_uint "FUND_TARGET_UVIBE" "$FUND_TARGET_UVIBE"
require_uint "FUND_GRANT_UVIBE" "$FUND_GRANT_UVIBE"

SIGNER_CLI="${LEADER_SIGNER_DIR}/dist/cli.js"
if [[ ! -f "$SIGNER_CLI" ]]; then
  fail "step=derive-address reason=missing-cli path=${SIGNER_CLI}"
fi

log "op=fund_leader.start chain_container=${CHAIN_CONTAINER} chain_id=${CHAIN_ID} rest=${CHAIN_REST} target=${FUND_TARGET_UVIBE} grant=${FUND_GRANT_UVIBE} logfile=${LOG_FILE}"
log "op=fund_leader.step step=derive-address leader_signer_dir=${LEADER_SIGNER_DIR}"

set +e
derive_output="$({ WEVIBE_IDENTITY_SEED_HEX="$WEVIBE_BENCH_LEADER_SEED_HEX" node "$SIGNER_CLI" derive-address; } 2>&1)"
derive_rc=$?
set -e
if [[ $derive_rc -ne 0 ]]; then
  fail "step=derive-address rc=${derive_rc}"
fi

set +e
derive_parsed="$(python3 -c '
import json
import sys

lines = [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]
if not lines:
    raise SystemExit("derive-address returned empty output")

payload = None
for line in reversed(lines):
    try:
        candidate = json.loads(line)
    except json.JSONDecodeError:
        continue
    if isinstance(candidate, dict) and "address" in candidate and "seed_fp" in candidate:
        payload = candidate
        break

if payload is None:
    raise SystemExit("derive-address payload not found in output")

addr = payload.get("address")
seed_fp = payload.get("seed_fp")
if not isinstance(addr, str) or not addr:
    raise SystemExit("derive-address missing address")
if not isinstance(seed_fp, str) or not seed_fp:
    raise SystemExit("derive-address missing seed_fp")

print(f"{addr}\t{seed_fp}")
' <<<"$derive_output")"
parse_derive_rc=$?
set -e
if [[ $parse_derive_rc -ne 0 ]]; then
  fail "step=derive-address reason=parse-failed err=$(compact_ws "$derive_parsed")"
fi

IFS=$'\t' read -r ADDR SEED_FP <<<"$derive_parsed"

if [[ -z "$ADDR" || -z "$SEED_FP" ]]; then
  fail "step=derive-address reason=parse-empty-fields"
fi

if [[ "$ADDR" != wevibe1* ]]; then
  fail "step=derive-address reason=invalid-address addr=${ADDR}"
fi

log "op=fund_leader.addr addr=${ADDR} seed_fp=${SEED_FP}"

set +e
balance_output="$(fetch_uvibe_balance "$ADDR" 2>&1)"
balance_rc=$?
set -e
if [[ $balance_rc -ne 0 ]]; then
  fail "step=query-balance addr=${ADDR} err=$(compact_ws "$balance_output")"
fi
BALANCE="$balance_output"
require_uint "BALANCE" "$BALANCE"

log "op=fund_leader.balance addr=${ADDR} seed_fp=${SEED_FP} balance=${BALANCE} target=${FUND_TARGET_UVIBE}"

if (( BALANCE >= FUND_TARGET_UVIBE )); then
  log "op=fund_leader.skip addr=${ADDR} seed_fp=${SEED_FP} balance=${BALANCE} target=${FUND_TARGET_UVIBE} reason=already-funded"
  exit 0
fi

log "op=fund_leader.step step=bank-send addr=${ADDR} seed_fp=${SEED_FP} grant=${FUND_GRANT_UVIBE} faucet_key=${FAUCET_KEY}"

set +e
tx_output="$({ docker exec "$CHAIN_CONTAINER" wevibed tx bank send "$FAUCET_KEY" "$ADDR" "${FUND_GRANT_UVIBE}uvibe" --keyring-backend "$KEYRING_BACKEND" --chain-id "$CHAIN_ID" --fees 5000uvibe --gas 200000 --broadcast-mode sync --yes -o json; } 2>&1)"
tx_rc=$?
set -e
if [[ $tx_rc -ne 0 ]]; then
  fail "step=bank-send rc=${tx_rc} err=$(compact_ws "$tx_output")"
fi

set +e
tx_parsed="$(python3 -c '
import json
import sys

lines = [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]
if not lines:
    raise SystemExit("bank send returned empty output")

payload = None
for line in reversed(lines):
    try:
        candidate = json.loads(line)
    except json.JSONDecodeError:
        continue
    if isinstance(candidate, dict) and "txhash" in candidate:
        payload = candidate
        break

if payload is None:
    raise SystemExit("bank send payload not found in output")

txhash = payload.get("txhash")
code = payload.get("code", 0)
raw_log = payload.get("raw_log", "")

if not isinstance(txhash, str) or not txhash:
    raise SystemExit("bank send missing txhash")

if isinstance(code, str):
    if not code.strip().lstrip("-").isdigit():
        raise SystemExit(f"bank send code is not numeric: {code!r}")
    code = int(code)
elif isinstance(code, int):
    pass
else:
    raise SystemExit(f"bank send code has invalid type: {type(code).__name__}")

if raw_log is None:
    raw_log = ""
elif not isinstance(raw_log, str):
    raw_log = str(raw_log)

flat_raw_log = " ".join(raw_log.split())
print(f"{txhash}\t{code}\t{flat_raw_log}")
' <<<"$tx_output")"
parse_tx_rc=$?
set -e
if [[ $parse_tx_rc -ne 0 ]]; then
  fail "step=bank-send reason=parse-failed err=$(compact_ws "$tx_parsed")"
fi

IFS=$'\t' read -r TXHASH TXCODE TX_RAW_LOG <<<"$tx_parsed"

if [[ -z "$TXHASH" || -z "$TXCODE" ]]; then
  fail "step=bank-send reason=parse-empty-fields"
fi

if [[ "$TXCODE" != "0" ]]; then
  fail "step=bank-send code=${TXCODE} txhash=${TXHASH} raw_log=${TX_RAW_LOG}"
fi

log "op=fund_leader.tx addr=${ADDR} seed_fp=${SEED_FP} txhash=${TXHASH} code=${TXCODE}"

LAST_BALANCE="$BALANCE"
for attempt in $(seq 1 20); do
  set +e
  poll_balance_output="$(fetch_uvibe_balance "$ADDR" 2>&1)"
  poll_rc=$?
  set -e
  if [[ $poll_rc -ne 0 ]]; then
    fail "step=poll-balance attempt=${attempt} txhash=${TXHASH} err=$(compact_ws "$poll_balance_output")"
  fi

  LAST_BALANCE="$poll_balance_output"
  require_uint "POLL_BALANCE" "$LAST_BALANCE"
  log "op=fund_leader.poll attempt=${attempt} balance=${LAST_BALANCE} target=${FUND_TARGET_UVIBE}"

  if (( LAST_BALANCE >= FUND_TARGET_UVIBE )); then
    log "op=fund_leader.ok addr=${ADDR} seed_fp=${SEED_FP} txhash=${TXHASH} balance=${LAST_BALANCE} grant=${FUND_GRANT_UVIBE}"
    exit 0
  fi

  sleep 1
done

fail "step=poll-balance timeout attempts=20 txhash=${TXHASH} last_balance=${LAST_BALANCE} target=${FUND_TARGET_UVIBE}"
