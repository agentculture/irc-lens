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

## Run

    ./scripts/cf-roundtrip/setup.sh
    set -a; source .cf-roundtrip.env; set +a
    pytest -m cloudflare -v

## Teardown

    ./scripts/cf-roundtrip/teardown.sh

## Notes

These scripts are intended for operator-driven (manual) use. Issue #28 tracks automation of this provisioning step in CI.
