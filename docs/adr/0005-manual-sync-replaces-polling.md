# Consumption is imported by a manual sync, not by polling

A 91-minute, non-renewable access token (see
[0001](0001-manual-token-paste-auth.md)) cannot support a polling integration, so the
coordinator, the sensors and token storage are gone. A config entry holds only the
**CPE**. Consumption is imported on demand by a **sync**: the user pastes a fresh token,
and the integration fetches the missing range and appends it to the
`eredes:energy_<cpe suffix>` external statistic (see
[0002](0002-external-statistics-for-history.md)). The token is used for that one call
and never stored, logged, put in an exception or returned.

## How a sync works

- **Window.** With stored statistics: from local midnight two days before the last
  stored hour, to now. Without: `history_days` back (365 by default). Request windows
  are naive Europe/Lisbon wall clock — the clock `edm/get` speaks
  ([0004](0004-load-curve-timestamps-are-lisbon-local.md)).
- **Blocks.** At most 31 days each, cut backwards from now so the newest block is
  never a sliver, with interior boundaries on local midnight. Fetched in sequence with
  a short pause. Readings repeated at a shared boundary are de-duplicated by UTC
  instant.
- **All or nothing.** Every block is fetched before anything is aggregated, and the
  result is written with one `async_add_external_statistics` call. The sync then waits
  for the recorder to run that import, so an immediate second sync reads the new last
  hour and appends nothing.
- **Partial final hour** (fewer than 4 quarter-hours in its UTC bucket) is dropped and
  picked up by the next sync. Incomplete hours before it are imported and reported.
- **Reopening.** If the last stored hour comes back complete with more energy than was
  stored (a partial hour left by version 1.x), it is rewritten from the sum before it.

## Error policy

`401` (token dead) and `403` with a `recaptcha` header (bot challenge) are separate
`ServiceValidationError`s with different messages, as
[0003](0003-authorization-request-is-the-recaptcha-slot.md) requires. Any other HTTP
status, a network error, or a body that is not the expected envelope aborts with a
`HomeAssistantError`. Nothing is written in any of these cases.

The one tolerance is the **leading edge**: history may begin at the contract date, and
the portal's answer to earlier ranges has not been observed. The prudent assumption
implemented is that it answers `Success: false` or an empty load curve. Those are
tolerated only until the first block returns data. After that, `Success: false`
aborts, and an empty block aborts if data comes back after it. An HTTP error aborts
even on the first block, with a hint to try a smaller `history_days`. If real-world
runs show the portal answering pre-contract ranges with an HTTP error, revisit this.

## The token and the `call_service` event

Home Assistant fires every action call as a `call_service` event carrying the raw
service data. The recorder stores it by default, and core logs it at debug level. So
an `eredes.sync` call with a `token` field puts the token in the database, which is
outside this integration's control. Two entry points therefore share one sync:

- the **"Sync now" options flow**, whose form input is not an event. This is the
  recommended path;
- the **`eredes.sync` action**, for scripts and automations. The README tells users to
  exclude `call_service` from the recorder if they use it.

## Considered Options

- **Keep polling and the reauth flow** — rejected: it fails by design about two hours
  after every paste.
- **Action only** — rejected: the token would be persisted by Home Assistant itself.
- **Options flow only** — rejected: the action is needed for scripting and dry runs
  from Developer Tools.
- **Write each block as it arrives** — rejected: a failure mid-way would leave a hole
  that the append-only resume never re-fetches. This was the 1.x behaviour, where a
  failed block was only logged.
