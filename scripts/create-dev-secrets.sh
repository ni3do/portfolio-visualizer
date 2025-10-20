#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRETS_DIR="$ROOT_DIR/secrets"

OVERWRITE=0

usage() {
  cat <<EOF
Usage: $(basename "$0") [--overwrite]

Creates development Docker secret files under ./secrets/.
Existing files are left untouched unless --overwrite is provided.
EOF
}

need_tool() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Required tool '$1' not found in PATH." >&2
    exit 1
  fi
}

random_hex() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
  else
    python3 - <<'PY'
import secrets
print(secrets.token_hex(24))
PY
  fi
}

write_secret() {
  local name="$1"
  local value="$2"
  local dest="$SECRETS_DIR/$name"

  if [[ -f "$dest" && $OVERWRITE -eq 0 ]]; then
    echo "[skip] $name already exists. Use --overwrite to regenerate."
    return
  fi

  printf '%s' "$value" >"$dest"
  chmod 600 "$dest"
  echo "[ok] wrote $name"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --overwrite)
      OVERWRITE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

mkdir -p "$SECRETS_DIR"

write_secret "postgres_app_user" "portfolio_app"
write_secret "postgres_app_password" "$(random_hex)"
write_secret "grafana_admin_user" "admin"
write_secret "grafana_admin_password" "$(random_hex)"
write_secret "ibkr_flex_token" "${IBKR_FLEX_TOKEN:-CHANGE_ME}"
write_secret "ibkr_flex_query_id" "${IBKR_FLEX_QUERY_ID:-CHANGE_ME}"
write_secret "visualizer_basic_auth_user" "visualizer"
write_secret "visualizer_basic_auth_password" "$(random_hex)"

echo "Secrets ready under $SECRETS_DIR"
