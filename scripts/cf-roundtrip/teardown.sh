#!/usr/bin/env bash
# Remove every resource setup.sh creates. Idempotent (404s and 2xx ignored; other statuses logged as warnings).
set -euo pipefail

: "${CF_API_TOKEN:?}"
: "${CF_ACCOUNT_ID:?}"
: "${CF_ZONE_ID:?}"
: "${CF_TEST_HOSTNAME:?}"

BASE="https://api.cloudflare.com/client/v4"
HDR=(-H "Authorization: Bearer $CF_API_TOKEN")
TOKEN_NAME="irc-lens-roundtrip"
TUNNEL_NAME="irc-lens-roundtrip"
APP_NAME_ENCODED="irc-lens%20roundtrip"

cf_del() {
  local resp
  resp="$(curl -sS -o /dev/null -w '%{http_code}' "${HDR[@]}" -X DELETE "$BASE$1" 2>/dev/null || echo "000")"
  case "$resp" in
    2*|404) ;;  # success or already gone — both fine for teardown
    *) printf 'warning: DELETE %s returned HTTP %s\n' "$1" "$resp" >&2 ;;
  esac
}
cf_get() { curl -sS "${HDR[@]}" "$BASE$1"; }

# Service token
TOK_ID="$(cf_get "/accounts/$CF_ACCOUNT_ID/access/service_tokens" \
  | jq -r --arg n "$TOKEN_NAME" '.result[] | select(.name==$n) | .id' | head -1)"
[[ -n "$TOK_ID" ]] && cf_del "/accounts/$CF_ACCOUNT_ID/access/service_tokens/$TOK_ID"

# Access app
APP_ID="$(cf_get "/accounts/$CF_ACCOUNT_ID/access/apps?name=$APP_NAME_ENCODED" \
  | jq -r '.result[0].id // empty')"
[[ -n "$APP_ID" ]] && cf_del "/accounts/$CF_ACCOUNT_ID/access/apps/$APP_ID"

# DNS
DNS_ID="$(cf_get "/zones/$CF_ZONE_ID/dns_records?name=$CF_TEST_HOSTNAME" \
  | jq -r '.result[0].id // empty')"
[[ -n "$DNS_ID" ]] && cf_del "/zones/$CF_ZONE_ID/dns_records/$DNS_ID"

# Tunnel
TUN_ID="$(cf_get "/accounts/$CF_ACCOUNT_ID/cfd_tunnel?name=$TUNNEL_NAME" \
  | jq -r '.result[0].id // empty')"
[[ -n "$TUN_ID" ]] && cf_del "/accounts/$CF_ACCOUNT_ID/cfd_tunnel/$TUN_ID"

rm -f "${HOME}/.config/irc-lens/cf-roundtrip-token.json"
echo "teardown complete" >&2
