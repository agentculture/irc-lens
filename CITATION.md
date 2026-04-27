# Citation

`irc-lens` follows Culture's **cite-don't-import** rule. Cited code is
copied (and adapted) from upstream sources, then carried in this repo as
its own source. We track the upstream commit each file was lifted from
so future updates can be diffed against it deliberately.

## Sources

| Local path | Source repo | Source path | Source SHA | Adaptation |
| --- | --- | --- | --- | --- |
| `src/irc_lens/irc/buffer.py` | `agentculture/culture` | `packages/agent-harness/message_buffer.py` | `57d3ba8` | Byte-faithful copy. |
| `src/irc_lens/irc/message.py` | `agentculture/culture` | `culture/protocol/message.py` | `57d3ba8` | Byte-faithful copy. The IRC line parser is shared by every AgentIRC client; the lens needs it to decode wire bytes in the read loop. |
| `src/irc_lens/irc/transport.py` | `agentculture/culture` | `packages/agent-harness/irc_transport.py` | `57d3ba8` | Imports rewired to `irc_lens.irc.*`; `culture.aio.maybe_await` inlined as `_maybe_await` (3 lines). **`CAP REQ :message-tags` removed** per the spec — the lens doesn't render IRCv3 tags (precedent: `culture/console/client.py:50-55`). **All telemetry/OTEL infrastructure removed** — the `_span` helper, the `tracer`/`metrics`/`backend` constructor kwargs, the traceparent injection in `send_raw`, and the inbound traceparent extraction in `_handle`. The lens has no agent loop and the spec excludes telemetry. Persistent-connection + read-loop shape preserved. `system-` event filter retained. **Two upstream bug-fixes + one minor improvement carried (flag for back-port to culture):** (a) `disconnect()` now catches `OSError` on `wait_closed()` instead of just `ConnectionError`, matching the QUIT-send handling above it (`ConnectionResetError` is one of several transport errors that can raise from `wait_closed`). (b) `_read_loop` now buffers as bytes and decodes per complete line; per-chunk `decode("utf-8", errors="replace")` corrupts multibyte sequences split across `recv` boundaries with U+FFFD. (c) `_reconnect` now wraps the retry loop in `try/finally` so `self._reconnecting` is released even if the loop exits because `_should_run` flipped or an unexpected exception escapes — upstream only releases the gate on successful return, so the gate could leak. The `except OSError:` itself is unchanged from upstream and correct: `ConnectionError` (which `_do_connect` raises) is a subclass of `OSError` in Python 3.3+, so the bare `OSError` catch handles it. |
| `src/irc_lens/commands.py` | `agentculture/culture` | `culture/console/commands.py` | `57d3ba8` | Byte-faithful copy. |

## Refresh

To refresh a cited file against a newer upstream, diff the new upstream
against the source SHA recorded above, port intentional changes, and
update the SHA in this table. Do not just overwrite — culture's
`packages/agent-harness` and `culture/console` evolve together with
agent-runtime concerns that `irc-lens` does not need.
