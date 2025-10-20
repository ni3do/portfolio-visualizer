#!/usr/bin/env bash
set -euo pipefail

COMPOSE_BIN="${COMPOSE_BIN:-docker compose}"

# Update yfinance mappings only when not already set
$COMPOSE_BIN exec -T postgres bash -lc "
  PGPASSWORD=\$(cat /run/secrets/postgres_app_password) \
  psql -v ON_ERROR_STOP=1 \
       -U \$(cat /run/secrets/postgres_app_user) \
       -d portfolio <<'SQL'
BEGIN;
UPDATE instruments
   SET yfinance_symbol = 'EURDKK=X'
 WHERE symbol = 'EUR.DKK'
   AND COALESCE(NULLIF(yfinance_symbol, ''), '') = '';

UPDATE instruments
   SET yfinance_symbol = 'UETW.DE'
 WHERE symbol = 'WRDUSW'
   AND COALESCE(NULLIF(yfinance_symbol, ''), '') = '';

UPDATE instruments
   SET yfinance_symbol = 'BTC-USD'
 WHERE symbol = 'XBT'
   AND COALESCE(NULLIF(yfinance_symbol, ''), '') = '';
COMMIT;
SQL
"

# Refresh FX rates first so BTC valuations convert from purchase currency
$COMPOSE_BIN run --rm etl fx-update

# Refresh latest prices with the new mappings
$COMPOSE_BIN run --rm etl price-update

# Recompute snapshots to propagate price and FX updates
$COMPOSE_BIN run --rm etl snapshot-recompute

# Show the updated mappings for confirmation
$COMPOSE_BIN exec -T postgres bash -lc "
  PGPASSWORD=\$(cat /run/secrets/postgres_app_password) \
  psql -U \$(cat /run/secrets/postgres_app_user) -d portfolio \
       -c \"SELECT symbol, yfinance_symbol FROM instruments WHERE symbol IN ('EUR.DKK','WRDUSW','XBT');\"
"
