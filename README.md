# Skydropx International Shipping Rate Toolkit

A small, dependency-free Python toolkit for getting and comparing
international shipping quotes from [Skydropx](https://skydropx.com)'s
quotation API — built while shipping a single physical retail product to
dozens of countries, and extracted here (with the business-specific data
removed) because the problems it solves aren't specific to any one product.

No third-party packages required — everything runs with `python3` alone
(standard library only: `urllib`, `csv`, `json`, `argparse`).

## Why this exists

Skydropx's quotation API looks simple at first: give it an origin, a
destination, a parcel, and it returns carrier rates. In practice, shipping
the *same* product to 40+ countries surfaces a few sharp edges that aren't
obvious from the docs:

- **The customs HS code is validated per destination country, against that
  country's own catalog entry for your product — not against a universal
  tariff nomenclature.** A code that works perfectly for one country can be
  flatly rejected for another, even when both countries' real-world customs
  schedules use that exact code. There's no way to tell from the error
  message alone whether the code's *format* is wrong or whether your
  product simply isn't registered for that country at all — `check_hs_code_formats.py`
  exists specifically to tell those two cases apart.
- **A rounded, generic postal code can silently under-report carrier
  availability.** A city-center code like `510000` for Guangzhou can make a
  carrier look unavailable, while a real, specific district code like
  `510630` unlocks it. This bit us more than once across different
  countries before we recognized it as a pattern — worth testing before
  concluding a carrier genuinely doesn't serve a city.
- **Quotations are asynchronous and look "done" before they are.** Skydropx
  assigns each carrier a rate ID immediately, with the actual price filled
  in a few seconds later. Every script here polls until the API's own
  `is_completed` flag is true rather than stopping as soon as `rates` is
  non-empty — an easy mistake that produces silently blank prices.
- **There's no "give me your cheapest rate" endpoint.** Every quote is
  priced against one exact origin + destination + parcel. Comparing
  packaging options or finding the cheapest bracket means running the API
  across a deliberate sweep of weights/dimensions and taking the minimum —
  `packaging_sweep.py` automates that.

## What's included

| Script | What it does |
|---|---|
| `skydropx_quotes.py` | Quotes ONE fixed parcel across a list of destinations (e.g. domestic). |
| `skydropx_quotes_intl.py` | Same idea for international destinations, where each destination country needs its own confirmed HS code (see `hs_code_by_country.example.csv`). Supports multiple weight brackets in a single run (`--weights 1,2,3`), and an explicit fallback-code mode for finding out which countries *might* accept a guessed code (never use that output as a real customs value — it's for discovery only). |
| `check_product_catalog.py` | Queries Skydropx's product catalog endpoint directly, instead of discovering what's registered one failed quote at a time. |
| `check_hs_code_formats.py` | Diagnostic: probes one destination country with several structurally different formats of a customs code, plus a control value, to tell apart "wrong format" from "nothing registered for this country at all." |
| `repoll_quotations.py` | Recovery utility: re-polls quotation IDs from a previous run that came back with blank prices, without re-creating (and re-billing API calls for) every quotation from scratch. |
| `packaging_sweep.py` | Answers "what's the cheapest rate for this parcel, and how does that change if I repackage it?" — sweeps a list of candidate weights/dimensions against one or many destinations and reports the cheapest and fastest carrier option for each. |

## Setup

```bash
cp config.example.json config.json          # then edit config.json
cp destinations_template.csv destinations.csv   # then edit with your own destinations
```

Fill in `config.json`:

- `client_id` / `client_secret` — from your own Skydropx account.
- `origin` — your real ship-from address.
- `parcel` — weight (kg) and dimensions (cm) of the package you're quoting.
- `products` — used for international quotes (customs/duty calculation).
  `hs_code` here is just a placeholder — **never guess a real one.** Get the
  correct, country-specific code from Skydropx support or a licensed
  customs broker before shipping anything for real. Every script in this
  repo treats a non-confirmed code as a rate-testing experiment only, and
  says so loudly in its own output.

`destinations.csv` / `destinations_sample_international.csv` — one
destination per row. Only `label`, `postal_code`, `area_level2` (city),
`area_level1` (state/province), and `country_code` affect the quote; the
rest can stay blank — the scripts fill them with harmless placeholder
values, since the API wants a full address shape even though only those
fields change the price.

`hs_code_by_country.example.csv` — one confirmed `hs_code` per destination
country you ship to. A country not in this file is skipped by
`skydropx_quotes_intl.py` (logged, not silently guessed) unless you pass
`--fallback-hs-code` explicitly.

## Running

Start small and with `--debug` so you can see the raw request/response and
confirm the API accepted the shape you sent:

```bash
python3 skydropx_quotes.py --limit 1 --debug
```

Then run the full batch:

```bash
python3 skydropx_quotes.py
python3 skydropx_quotes_intl.py --destinations destinations_sample_international.csv --weights 1,2,3
```

Each script's own `--help` has the full usage, including the fallback-code
discovery mode and the diagnostic scripts.

## Notes

- Skydropx OAuth tokens are valid ~2 hours and the API allows roughly
  2 requests/second; the scripts pace themselves to stay under that and
  re-authenticate once per run.
- Rates are only valid for a limited window per Skydropx's docs — re-run
  for fresh pricing rather than reusing old output.
- `config.json` and any real `destinations*.csv` you create hold your
  credentials and business data — `.gitignore` already excludes them; don't
  remove that exclusion.
- Nothing here constitutes customs or trade-compliance advice. HS/tariff
  codes determine duties and legal declarations; always get the
  country-specific code confirmed by your carrier or a customs broker
  before using it on a real shipment.

## License

MIT — see `LICENSE`.
