# Historical consumption is imported as an external statistic

The one-year consumption backfill is written to Home Assistant long-term statistics
under an **external** statistic id, `eredes:energy_<cpe suffix>` (source `eredes`), via
`async_add_external_statistics` — not attached to the `sensor.…_daily_energy` entity.

## Context

External statistics must use a `<source>:<object_id>` id; entity-style dotted ids
(`sensor.…`) are rejected by `valid_statistic_id`. An earlier version built the id in
the dotted form but still called `async_add_external_statistics`, so every import
fetched a full year and then failed with `Invalid statistic_id` — the backfill never
landed.

## Considered Options

- **Attach to the Daily Energy sensor** (`async_import_statistics`, source `recorder`,
  entity id) — rejected. That sensor is `state_class=TOTAL` with a daily `last_reset`,
  so the recorder already compiles its own statistics for it; importing an independent
  hourly cumulative series onto the same id would collide with the recorder's sums.
- **External statistic under a dedicated id** — chosen. It is independent of the live
  sensor, doesn't require the entity to exist, and is the idiomatic way to feed
  historical energy into the Energy Dashboard.

## Consequences

The Energy Dashboard consumption source is the `eredes:energy_…` statistic, not the
sensor (see README). The cumulative `sum` is seeded from the last imported hour on
resume, so imports are append-only; corrections to already-imported hours are not
re-pulled.

**Update (v2.0.0):** the Daily Energy sensor is gone (see
[0005](0005-manual-sync-replaces-polling.md)); the statistic id, source, name and unit
are unchanged. Resume is still append-only, with two refinements: the newest hour is
not written until all four quarter-hours are published, and a last stored hour left
partial by 1.x is reopened once it comes back complete.

**Update (v2.0.1):** the metadata now sets `unit_class: "energy"`. It used to pass
`None`, and the Energy dashboard's statistic picker only lists statistics of the
`energy` class, so the imported history could not be selected. Every non-dry sync
re-writes the metadata (even with no new hours), which repairs statistics created
earlier.
