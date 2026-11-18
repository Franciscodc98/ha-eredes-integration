# OMIE day-ahead prices are fetched from omie.es for indexed-tariff estimates

To estimate market-indexed offers, the integration downloads the OMIE day-ahead
marginal prices for Portugal and values every 15-minute reading at its own price. This
is the one deliberate exception to "contact no host other than
`balcaodigital.e-redes.pt`" ([0005](0005-manual-sync-replaces-polling.md)), approved
by the user. Only public price files are requested, and no user data is sent.

## Source

`https://www.omie.es/pt/file-download?parents=marginalpdbcpt&filename=marginalpdbcpt_YYYYMMDD.1`
is public and needs no account. Use that exact query: the `parents%5B0%5D=` form
shown in some links answers with a redirect that sets a gateway cookie.

The file has one line per period (`year;month;day;period;price;price;`) in €/MWh:

- Files since the Iberian market moved to 15-minute periods have 96 periods, with 92
  and 100 on DST days. Older files have 24 hourly periods, which are expanded to
  quarter-hours.
- Verified against real files for 2026-11-16 (96 periods), 2026-10-25 (100),
  2026-03-29 (92) and 2025-01-15 (24).
- **Periods count from Madrid midnight**, one hour ahead of Lisbon. They are
  consecutive real intervals, so period *n* starts at Madrid midnight plus *n − 1*
  period lengths in absolute time. That covers the DST days with no special case.
- The fifth column is read as the Portuguese price. The two price columns were equal
  on every day checked, so which one is Portugal's is **unverified**. It only matters
  on days when the Iberian market splits.

Alternatives with an API (ENTSO-E Transparency, REE e·sios) need registration and a
token, so they were not used.

## How it is used

- Prices are cached per market day under one `.storage` key, since they are the same
  for everyone. Only missing days are downloaded, half a second apart.
- Each sync values the new readings at the OMIE price and appends
  `eredes:market_<sfx>`: hourly sums of kWh × price, in €. The same window and append
  rules as the other derived series apply, and the series never runs past the newest
  hour whose every quarter is priced.
- **OMIE problems never fail the sync.** The energy series are written anyway. The
  market series stops before the first unpriced quarter and the response reports
  `market_prices_missing_from`. Fetching stops at the first failed day, so the series
  stays contiguous, and the next sync continues from there. Dry runs do not contact
  OMIE.

## Indexed plans

A plan can price energy as **indexed** instead of fixed. Per 15-minute interval:

`OMIE × (1 + losses) × K1 + K2 + TAR energy`

This is the structure of EDP's "Eletricidade Indexada Horária" standard offer sheet
for 2026: K1 = 1.08, K2 = 0.0185 €/kWh, and a fixed 0.1171 €/day plus TAR power.
Other suppliers' formulas fit the same shape. The plan's per-period fields hold the
TAR energy, and the power price is the supplier's daily fee plus TAR power.

The formula is linear in consumption, so the hourly market value and the hourly
period split are enough to rebuild it exactly.

Network losses are a **single average percentage**. EDP publishes 16.4 % for its
"média" variant. The exact method uses ERSE's quarter-hourly loss profiles, which
change every year. Those profiles were left out for now, because they would be
another dataset to fetch and maintain.
