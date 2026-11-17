# Tariff periods, power and cost estimates are derived locally

To compare tariff offers, the integration derives three kinds of external statistic
from the 15-minute load curve it already fetches, and estimates what each user-defined
tariff plan would have cost. Nothing new is requested from the portal, and the
manual, token-per-sync model ([0005](0005-manual-sync-replaces-polling.md)) is
unchanged.

## What is derived

- **Energy per tariff period**: `eredes:energy_<sfx>_<cycle>_<period>` for both BTN
  cycles (`daily`, `weekly`) and the three periods (`vazio`, `cheias`, `ponta`), so
  six series. The bi-hourly split needs no series of its own, because its vazio is
  the tri-hourly vazio and fora de vazio is cheias plus ponta. Each **15-minute
  reading** is classified before aggregation, because some boundaries (10:30,
  09:15, 12:15) fall inside an hour. Classification uses the Lisbon wall clock;
  winter or summer follows the legal time at that instant.
- **Power**: `eredes:power_<sfx>`, a mean-type series holding the mean, min and max of
  each hour's 15-minute average power (energy × 4). The portal's "potência máxima
  tomada" is understood to be the monthly maximum of this value. That is unverified,
  and it can be checked against the portal's monthly chart. A 15-minute average hides
  the second-scale peaks that trip a breaker, so this shows a trend; it does not prove
  that a lower contracted power would suffice.
- **Cost per plan**: `eredes:cost_<sfx>_<plan id>`, in the instance currency.

Derived series are appended like the total: after each series' own last hour, and
never past the total's newest complete hour. A series that does not exist yet (after
an upgrade) widens the sync window to the full history once.

## Period tables

The tables are the BTN periods in force before ERSE **Directive 3/2026** (19 August
2026). They were taken from a supplier's publication, because ERSE's own PDFs carry
the tables as images. That directive changes the BTN periods: bi- and tri-hourly
meters are re-parametrised gradually **between 1 July and 31 December 2027**. Its new
tables were not readable either and are **not implemented**. Before mid-2027, the
tables need a dated second version and a way to set the switch date. BTN holidays
are not special-cased.

A meter with three registers reports per-period index readings on the invoice. Those
readings are the way to validate the classification, and to tell which cycle the
meter is set to.

## Cost model

Plans are **config subentries**: option (simple, bi-hourly or tri-hourly), cycle,
power price per day, energy prices excluding VAT, and an optional discount on energy
only. Taxes are the entry's options (VAT rates, the reduced-rate kWh per 30 days, VAT
on power, IEC, DGEG, CAV). Their defaults match a real 2026 invoice for a contract up
to 6.9 kVA, which the model reproduced to the cent.

The estimate is computed per Lisbon calendar month, as an invoice would be:

- energy × price × (1 − discount);
- power × the days that have data;
- IEC per kWh, plus DGEG and CAV once a month;
- VAT at the reduced rate on `round(threshold × days / 30)` kWh and the normal rate on
  the rest.

With several periods, the reduced share is applied **in proportion** to each period's
consumption. That is an assumption: the law caps the kWh, but how invoices split them
across periods was not confirmed. One secondary source claims a fixed 68/32 split for
bi-hourly.

Hourly cost rows are written so the Energy and statistics cards can aggregate them:
power on the first hour of each day, monthly levies on the first hour of the month.
Costs are recomputed from the stored series, never from the portal. This happens
after each sync, and through the entry's update listener whenever a plan or a tax
changes. Only rows that changed are rewritten, and the series of deleted plans are
cleared.

## Considered Options

- **Keep the raw readings in `.storage` and derive everything from them** — rejected
  for now: duplicate storage that grows by about a megabyte a year. If the period
  tables change, the series can be cleared and re-synced instead.
- **Price entities and template sensors** — rejected: the Energy dashboard cannot
  price imported external statistics, and templates cannot backfill history.
- **Fetch regulated or market prices automatically** — out of scope for now. The
  integration only talks to `balcaodigital.e-redes.pt`. A day-ahead market (OMIE)
  plan would need another host and is a separate decision.
