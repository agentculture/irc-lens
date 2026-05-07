#!/usr/bin/env bash
# Idempotent CF round-trip setup. Re-runs are safe: existing resources
# are reused, only missing ones are created. Prints / writes
# `.cf-roundtrip.env` with values the test reads at runtime.
set -euo pipefail

: "${CF_API_TOKEN:?must be set}"
: "${CF_ACCOUNT_ID:?must be set}"
: "${CF_ZONE_ID:?must be set}"
: "${CF_TEST_HOSTNAME:?must be set}"
: "${CF_TEAM_DOMAIN:?must be set}"

BASE="https://api.cloudflare.com/client/v4"
HDR=(-H "Authorization: Bearer $CF_API_TOKEN" -H "Content-Type: application/json")
TUNNEL_NAME="irc-lens-roundtrip"
TOKEN_NAME="irc-lens-roundtrip"
APP_NAME="irc-lens roundtrip"
APP_NAME_ENCODED="irc-lens%20roundtrip"
TOKEN_FILE="${HOME}/.config/irc-lens/cf-roundtrip-token.json"
ENV_OUT="${ENV_OUT:-.cf-roundtrip.env}"

cf_get() { curl -sS "${HDR[@]}" "$BASE$1"; }
cf_post() { curl -sS "${HDR[@]}" -X POST "$BASE$1" -d "$2"; }

echo "[1/5] tunnel" >&2
TUNNEL_ID="$(cf_get "/accounts/$CF_ACCOUNT_ID/cfd_tunnel?name=$TUNNEL_NAME" \
  | jq -r '.result[0].id // empty')"
if [[ -z "$TUNNEL_ID" ]]; then
  TUNNEL_SECRET="$(openssl rand -base64 32)"
  TUNNEL_ID="$(cf_post "/accounts/$CF_ACCOUNT_ID/cfd_tunnel" \
    "$(jq -n --arg n "$TUNNEL_NAME" --arg s "$TUNNEL_SECRET" \
        '{name:$n, tunnel_secret:$s}')" \
    | jq -r '.result.id')"
fi
echo "  tunnel_id=$TUNNEL_ID" >&2
TUNNEL_TOKEN="$(cf_get "/accounts/$CF_ACCOUNT_ID/cfd_tunnel/$TUNNEL_ID/token" | jq -r '.result')"

echo "[2/5] dns" >&2
DNS_ID="$(cf_get "/zones/$CF_ZONE_ID/dns_records?name=$CF_TEST_HOSTNAME" \
  | jq -r '.result[0].id // empty')"
if [[ -z "$DNS_ID" ]]; then
  cf_post "/zones/$CF_ZONE_ID/dns_records" \
    "$(jq -n --arg n "$CF_TEST_HOSTNAME" --arg c "$TUNNEL_ID.cfargotunnel.com" \
        '{type:"CNAME", name:$n, content:$c, proxied:true}')" >/dev/null
fi

echo "[3/5] access app" >&2
APP_ID="$(cf_get "/accounts/$CF_ACCOUNT_ID/access/apps?name=$APP_NAME_ENCODED" \
  | jq -r '.result[0].id // empty')"
if [[ -z "$APP_ID" ]]; then
  APP_ID="$(cf_post "/accounts/$CF_ACCOUNT_ID/access/apps" \
    "$(jq -n --arg n "$APP_NAME" --arg d "$CF_TEST_HOSTNAME" \
        '{name:$n, domain:$d, type:"self_hosted", session_duration:"24h"}')" \
    | jq -r '.result.id')"
fi
echo "  app_id=$APP_ID" >&2

AUD="$(cf_get "/accounts/$CF_ACCOUNT_ID/access/apps/$APP_ID" | jq -r '.result.aud')"

echo "[4/5] service token" >&2
if [[ -f "$TOKEN_FILE" ]]; then
  CLIENT_ID="$(jq -r .client_id "$TOKEN_FILE")"
  CLIENT_SECRET="$(jq -r .client_secret "$TOKEN_FILE")"
else
  TOKEN_JSON="$(cf_post "/accounts/$CF_ACCOUNT_ID/access/service_tokens" \
    "$(jq -n --arg n "$TOKEN_NAME" '{name:$n}')" | jq -r '.result')"
  CLIENT_ID="$(echo "$TOKEN_JSON" | jq -r .client_id)"
  CLIENT_SECRET="$(echo "$TOKEN_JSON" | jq -r .client_secret)"
  mkdir -p "$(dirname "$TOKEN_FILE")"
  echo "$TOKEN_JSON" >"$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
fi

echo "[5/5] policy" >&2
EXISTING="$(cf_get "/accounts/$CF_ACCOUNT_ID/access/apps/$APP_ID/policies" \
  | jq -r '.result[] | select(.name=="allow-svc-token") | .id' | head -1)"
if [[ -z "$EXISTING" ]]; then
  cf_post "/accounts/$CF_ACCOUNT_ID/access/apps/$APP_ID/policies" \
    "$(jq -n --arg cid "$CLIENT_ID" \
        '{name:"allow-svc-token", decision:"non_identity", precedence:1,
          include:[{service_token:{token_id:$cid}}]}')" >/dev/null
fi

cat >"$ENV_OUT" <<EOF
IRC_LENS_TEST_AUD=$AUD
IRC_LENS_TEST_HOSTNAME=$CF_TEST_HOSTNAME
IRC_LENS_TEST_TEAM_DOMAIN=$CF_TEAM_DOMAIN
IRC_LENS_TEST_CLIENT_ID=$CLIENT_ID
IRC_LENS_TEST_CLIENT_SECRET=$CLIENT_SECRET
IRC_LENS_TEST_TOKEN_NAME=$TOKEN_NAME
IRC_LENS_TEST_TUNNEL_TOKEN=$TUNNEL_TOKEN
EOF
chmod 600 "$ENV_OUT"
echo "wrote $ENV_OUT" >&2
