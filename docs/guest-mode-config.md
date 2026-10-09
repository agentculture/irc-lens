# Guest mode configuration

The optional `guest_mode:` section of the lens config switches on the public
guest sandbox for chat.culture.dev. **It is off by default**: with the section
absent or `enabled: false`, guest routes return 404 and authentication behaves
exactly as before — approved emails pass, anyone else gets 403.

```yaml
guest_mode:
  enabled: false            # the switch, and the kill switch during abuse
  sandbox:                  # the isolated, unlinked IRCd guests connect to
    name: sbx
    host: 127.0.0.1
    port: 6668
  store_path: ~/.local/share/irc-lens/guests.db   # default: $XDG_DATA_HOME/irc-lens/guests.db
  legal_version_url: https://culture.dev/legal/version.json
  mail:                     # sender for guest token emails
    provider: none
    from: ""
    api_key_env: IRC_LENS_MAIL_API_KEY            # env var holding the provider key
  rate_limits:
    entry_per_min: 10
    messages_per_min: 20
    password_attempts_per_15min: 5
```

| Key | `LensConfig` field | Default |
| --- | --- | --- |
| `enabled` | `guest_enabled` | `false` |
| `sandbox.name` | `guest_sandbox_name` | `sbx` |
| `sandbox.host` | `guest_sandbox_host` | `127.0.0.1` |
| `sandbox.port` | `guest_sandbox_port` | `6668` |
| `store_path` | `guest_store_path` | `$XDG_DATA_HOME/irc-lens/guests.db` |
| `legal_version_url` | `guest_legal_version_url` | `https://culture.dev/legal/version.json` |
| `mail.provider` | `guest_mail_provider` | `none` |
| `mail.from` | `guest_mail_from` | `""` |
| `mail.api_key_env` | `guest_mail_api_key_env` | `IRC_LENS_MAIL_API_KEY` |
| `rate_limits.entry_per_min` | `guest_rate_entry_per_min` | `10` |
| `rate_limits.messages_per_min` | `guest_rate_messages_per_min` | `20` |
| `rate_limits.password_attempts_per_15min` | `guest_rate_password_attempts_per_15min` | `5` |

Validation: every sub-section must be a mapping; unknown keys anywhere in the
section are rejected (typo guard); `enabled` must be a boolean, ports go
through the shared port check, `legal_version_url` must be an `http(s)` URL
with a host, and rate limits must be integers.
