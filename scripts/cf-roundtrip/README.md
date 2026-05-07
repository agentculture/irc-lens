# CF round-trip helpers

> **Secrets**: `setup.sh` writes the service-token client-secret to `.cf-roundtrip.env` in the repo root. The repo's `.gitignore` excludes this file — do not commit it.

`setup.sh` provisions (or reuses) a Cloudflare Tunnel, DNS record,
Access application, allow-policy, and service token for the
`@pytest.mark.cloudflare` round-trip test.

## Required env

| Var | Purpose |
| --- | --- |
| `CF_API_TOKEN` | Cloudflare token with Account:Tunnel:Edit, Account:Access:Apps and Policies:Edit, Zone:DNS:Edit on `CF_ZONE_ID` |
| `CF_ACCOUNT_ID` | your Cloudflare account ID |
| `CF_ZONE_ID` | the zone hosting `CF_TEST_HOSTNAME` |
| `CF_TEST_HOSTNAME` | the hostname the test will hit |
| `CF_TEAM_DOMAIN` | `<team>.cloudflareaccess.com` |

## Env file written by setup.sh

`setup.sh` writes `.cf-roundtrip.env` (gitignored, `chmod 600`) with these variables:

| Var | Purpose |
| --- | --- |
| `IRC_LENS_TEST_AUD` | Cloudflare Access application audience tag |
| `IRC_LENS_TEST_HOSTNAME` | public hostname the test hits |
| `IRC_LENS_TEST_TEAM_DOMAIN` | `<team>.cloudflareaccess.com` |
| `IRC_LENS_TEST_CLIENT_ID` | service-token client ID (`<uuid>.access`), used in `CF-Access-Client-Id` request header |
| `IRC_LENS_TEST_CLIENT_SECRET` | service-token client secret, used in `CF-Access-Client-Secret` request header |
| `IRC_LENS_TEST_TOKEN_NAME` | service-token display name written by setup.sh; the test puts this value in `auth.allowed_service_tokens` (Cloudflare populates `common_name` with the token's display name, not the UUID client_id) |

## Run

    ./scripts/cf-roundtrip/setup.sh
    set -a; source .cf-roundtrip.env; set +a
    pytest -m cloudflare -v

## Teardown

    ./scripts/cf-roundtrip/teardown.sh

## Notes

These scripts are intended for operator-driven (manual) use. Issue #28 tracks automation of this provisioning step in CI.
