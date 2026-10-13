# TODO

The short list. Collaborative detail lives in
[GitHub issues](https://github.com/sushiHex/hardline-mcp/issues); everything
below is known and deliberate.

## Open design question

**Stable instance addresses.** Ownership rests on reclaimable names, with
absence made insufficient rather than replaced: a claim needs evidence. A
2026-10-13 design pass deferred a separate instance address against stated
criteria and recorded when to revisit; see "Stable instance addresses" under
[Decisions](docs/architecture.md#decisions-and-what-was-rejected). Acks are now
recorded (`acked_by`), so its misdelivery trigger can be checked.

## Known limits, each a deliberate trade

**Mixed revisions are half-blind.** Older processes read and write the
`sessions` table; current code uses `agent_sessions` and consults both for
ownership. That closes this side only: an older process will not read the new
table whatever we write. It resolves itself as old processes exit, and the
orphan `sessions` table can be dropped once none remain. `hardline-mcp report`
shows the revisions registered servers run and any live legacy writer; a
process that never registers stays invisible, so an empty answer is evidence,
not proof.

**A record without a creation token is honoured on pid liveness alone.** Every
platform reads one now (macOS since #69), but rows written before that, or
whose token would not read, let a reused pid pass for its process: they can
keep a lane held or a job looking owned, which refuses rather than steals. They
age out with the processes that wrote them.

**`claim` probes the OS inside `BEGIN IMMEDIATE`.** It holds the single writer
while checking holders and job owners. The row count is normally zero or one,
so it is microseconds, but it is the writer lock.

## Shipped since this list was written

- Session-to-session mail watched working on the real store, Claude to Codex
  and Claude to Claude (see "Not yet verified live" in `CLAUDE.md`).
- The mutation harness and an isolated test store (#25): `scripts/mutate.py`,
  `tests/mutations.json`, run in CI.
- Process-instance binding (#29): job owners carry `owner_key`, and a session
  whose launching host exited is dead, so an orphaned server no longer holds
  its lane.
- Async Codex from Claude across an MCP reconnect, watched live (#32), and
  `server_info().agent_commands` naming the executable a spawn runs (#33).
