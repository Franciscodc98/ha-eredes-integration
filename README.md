# E-REDES Manual Sync for Home Assistant

A Home Assistant custom integration that imports electricity consumption from the
[E-REDES Balcão Digital](https://balcaodigital.e-redes.pt) portal (the Portuguese
distribution network operator) into Home Assistant's long-term statistics, for the
**Energy dashboard**.

This is a fork of [mrfyda/ha-eredes](https://github.com/mrfyda/ha-eredes) by
[@mrfyda](https://github.com/mrfyda) (MIT), reworked into a **manual sync**: no polling
and no sensors. You paste a fresh token when you want to catch up, and the integration
imports exactly the range that is missing.

## Why manual

E-REDES login is protected by reCAPTCHA, so the access token (`aat`) has to be copied
from the browser by hand. That token was measured to expire about **91 minutes** after
login and cannot be renewed (see [docs/adr/0001](docs/adr/0001-manual-token-paste-auth.md)).
An hourly-polling integration is therefore guaranteed to break about two hours after
every paste. Since E-REDES publishes consumption with a ~24h delay anyway, a one-shot
sync per paste loses nothing.

## How it works

- The first sync imports the available history (365 days by default, or less if the
  contract is more recent).
- Each later sync re-reads from two days before the last stored hour and appends only
  the hours that are missing. Running it twice in a row adds nothing.
- Everything is fetched first, in 31-day blocks, and written in a single step. If
  anything fails (expired token, captcha challenge, network error, unexpected
  response), **nothing is written** and you get a clear error.
- The newest hour is dropped if E-REDES has not published all four quarter-hours yet;
  the next sync picks it up whole. Incomplete hours inside the series are imported but
  reported.

## Installation

### HACS

1. Open HACS in Home Assistant
2. Click the three dots menu and select "Custom repositories"
3. Add `https://github.com/Franciscodc98/ha-eredes-integration` with category "Integration"
4. Click "Install"
5. Restart Home Assistant

### Manual

1. Copy `custom_components/eredes` to your Home Assistant `config/custom_components/` directory
2. Restart Home Assistant

## Configuration

1. Go to **Settings** > **Devices & Services** > **Add Integration** > "E-REDES"
2. Enter your **CPE** (e.g. `PT0002000012345678AB`)

Only the CPE is stored. No token is ever saved.

Upgrading from the polling version (1.x) migrates the entry automatically: the stored
token is deleted, the old Daily Energy / Power sensors and their device are removed, and
the `eredes:energy_…` statistic is kept, so the next sync continues it.

## Syncing

### Getting a token

1. Log into [balcaodigital.e-redes.pt](https://balcaodigital.e-redes.pt) in your browser
2. Open the developer tools (F12)
3. Go to **Application** > **Cookies** > `https://balcaodigital.e-redes.pt`
4. Copy the value of the `aat` cookie (a bare value, `aat=…` or the full Cookie header
   all work)

Sync right away: the token expires shortly after login.

### Option A — "Sync now" form (recommended)

**Settings** > **Devices & Services** > **E-REDES** > **Configure**. Paste the token,
optionally tick **Dry run**, and submit. The summary shows what was imported.

### Option B — the `eredes.sync` action

```yaml
action: eredes.sync
data:
  token: "eyJ..."
  # cpe: PT0002000012345678AB   # only needed with several CPEs configured
  # dry_run: true               # fetch and compute, write nothing
  # history_days: 365           # first import only
```

> **Warning.** Home Assistant itself records the data of every action call — including
> this token — as a `call_service` event in its database, and logs it when debug logging
> is enabled. The "Sync now" form does not have this problem. If you use the action, add
> this to `configuration.yaml`:
>
> ```yaml
> recorder:
>   exclude:
>     event_types:
>       - call_service
> ```
>
> The token stops working ~91 minutes after login, but it should not be stored at all.

The action returns a response (use **Developer tools** > **Actions** to see it):

| Field | Meaning |
|---|---|
| `readings_fetched`, `first_reading`, `last_reading` | 15-minute readings received |
| `hours_written`, `first_hour_written`, `last_hour_written` | Hours written (or, in a dry run, that would be written) |
| `kwh_added`, `final_sum_kwh` | Energy appended and the cumulative total afterwards |
| `incomplete_interior_hours`, `incomplete_interior_hour_starts` | Imported hours with fewer than 4 readings |
| `missing_hours` | Hours with no reading at all between imported hours |
| `dropped_partial_final_hour`, `dropped_partial_final_hour_start` | Whether the newest, still-incomplete hour was left for later |
| `reopened_last_hour` | Whether a partial last hour left by version 1.x was completed |
| `dry_run`, `window_start`, `window_end`, `blocks_requested`, `statistic_id` | What was asked for |

### Errors

| Error | Meaning |
|---|---|
| Token refused (401) | The token expired. Log in again and paste a fresh one. |
| reCAPTCHA challenge (403) | E-REDES wants a captcha solved. This is **not** a token problem; try later. |
| HTTP error on the oldest block | Possibly a range before your contract start; try a smaller `history_days`. |
| Others | Network or unexpected response. Nothing was written; just run it again. |

## Energy Dashboard Setup

The data lands in the long-term statistic **E-REDES Energy (`<CPE suffix>`)**
(id `eredes:energy_<cpe suffix>`):

1. Go to **Settings** > **Dashboards** > **Energy**
2. Under "Grid consumption", click **Add consumption**
3. Select **E-REDES Energy (`<CPE suffix>`)**

## Tariff periods, peak power and plan comparison

Every sync also derives, from the same 15-minute data:

- **Energy per tariff period** (vazio, cheias, ponta) for both BTN cycles: the
  **daily cycle** (same hours every day) and the **weekly cycle** (weekdays, Saturdays
  and Sundays differ; weekends are mostly vazio). Statistic ids:
  `eredes:energy_<sfx>_daily_vazio`, `..._weekly_ponta`, etc.
- **Power**: `eredes:power_<sfx>`, the average power of each 15-minute interval
  (energy x 4). Its monthly maximum is what the portal shows as *potência máxima
  tomada*. A 15-minute average hides the short peaks that trip the breaker, so use it
  to see a trend before considering a lower contracted power, not as proof.
- **Estimated cost per tariff plan**: `eredes:cost_<sfx>_<plan id>`.

The first sync after upgrading re-reads the history once, to fill the new series.

### Tariff plans

On the E-REDES integration page, use **Add tariff plan** once per offer you want to
compare: your current contract, the regulated tariff, another supplier's bi-hourly
offer, and so on. Each plan has:

- the option (simple, bi-hourly or tri-hourly) and the cycle;
- the power price per day;
- the energy prices in EUR/kWh, **without VAT**;
- an optional **discount on energy** (for example 15 %; the default is 0).

Not sure which cycle you would pick? Add the same offer once per cycle.

Taxes and levies (VAT rates, reduced-rate kWh per 30 days, IEC, DGEG, CAV) are shared
by all plans, under **Configure > Taxes and levies**. The defaults match a 2026 invoice
for a contract up to 6.9 kVA; check them against yours. Changing a plan or a tax
recomputes every cost series at once, with no token needed.

To check the model, create a plan identical to your current contract and compare one
month with your invoice. Prices are entered by hand: the integration only talks to the
E-REDES portal. Regulated prices are published by ERSE.

### Market-indexed offers (OMIE)

A plan's **energy pricing** can be *Indexed to OMIE* instead of fixed. Each sync then
also downloads the public OMIE day-ahead prices for Portugal (from `www.omie.es`, the
only other host the integration contacts; no data of yours is sent). It values every
15-minute reading at its own market price and stores the result as
`eredes:market_<sfx>`. An indexed plan is estimated, per 15 minutes, as:

`OMIE price x (1 + losses) x K1 + K2 + TAR energy`, plus a daily fixed charge.

K1, K2 and losses come from the offer's standard sheet (*ficha normalizada*); TAR is the
ERSE network access tariff for the year. Example, from EDP's "Eletricidade Indexada
Horária" sheet for 2026 at 5.75 kVA, simple option:

| Field | Value |
|---|---|
| Power charge per day | 0.4035 (0.1171 fixed + 0.2864 TAR power) |
| K1 | 1.08 |
| K2 | 0.0185 |
| Network losses | 16.4 (EDP's published average) |
| TAR energy | 0.0607 (bi-hourly: vazio 0.0158, fora de vazio 0.0835; tri-hourly: vazio 0.0158, cheias 0.0412, ponta 0.2452) |

If OMIE cannot be reached, the sync still imports your consumption; the market series
catches up on the next sync.

### Dashboard

[`docs/dashboard.yaml`](docs/dashboard.yaml) is a ready-made view with:

- monthly consumption per period (stacked), for each cycle;
- the estimated monthly cost of each plan, side by side (indexed plans included);
- the monthly peak 15-minute power;
- the last 30 days per period.

Replace the placeholders as its comments explain.

> The period tables are those in force before ERSE Directive 3/2026. New periods
> reach BTN bi- and tri-hourly meters between July and December 2027; until the
> integration is updated, estimates after that date use the old tables.

## Known Limitations

| Limitation | Description |
|------------|-------------|
| Manual token | Due to reCAPTCHA, tokens must be copied from the browser |
| Data delay | E-REDES publishes consumption with roughly a 24h delay |
| Append-only | Hours already imported are not re-pulled if E-REDES corrects them later |
| Resolution | 15-minute data, imported as hourly statistics |
| Import only | Only the `A+` register (consumption); no injection/solar export |
| Estimates | Plan costs exclude discounts other than on energy, social tariff, adjustments, and invoices not aligned to calendar months |

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements_test.txt

pytest tests/ -v
ruff check .
mypy custom_components/eredes --strict
```

Home Assistant does not run natively on Windows; use WSL or Linux for the tests.

## License

MIT License - see [LICENSE](LICENSE) for details. Original work by
[@mrfyda](https://github.com/mrfyda/ha-eredes).

## Disclaimer

This integration is not affiliated with or endorsed by E-REDES. Use at your own risk.
