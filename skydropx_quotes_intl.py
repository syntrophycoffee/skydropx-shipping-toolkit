#!/usr/bin/env python3
"""
skydropx_quotes_intl.py
------------------------
Like skydropx_quotes.py, but for international destinations where each
destination COUNTRY needs its own hs_code (Skydropx validates hs_code
against the destination country's own tariff nomenclature, not a single
universal code -- see hs_code_by_country.csv).

Usage:
    python3 skydropx_quotes_intl.py
    python3 skydropx_quotes_intl.py --weight 1.0 --output quotes_intl_1kg.csv
    python3 skydropx_quotes_intl.py --limit 3 --debug   # sanity-check a few rows first

    # Quote MULTIPLE weight brackets in a single run -- one script execution,
    # one output file, a "weight" column tells the brackets apart. Use this
    # instead of running the script 3 separate times for 1/2/3kg:
    python3 skydropx_quotes_intl.py --destinations destinations_confirmed_working.csv \
        --weights 2,3 --output quotes_intl_2_3kg.csv

    # Test EVERY country in the destinations file, not just the ones with a
    # confirmed code: countries not in hs_code_by_country.csv fall back to
    # the plain 6-digit WCO heading (see --fallback-hs-code below). Results
    # are tagged "confirmed" vs "fallback_guess" in the output so you can
    # tell which rows to trust as-is and which still need a real code.
    python3 skydropx_quotes_intl.py --weight 1.0 --fallback-hs-code 0901.210000 --output quotes_intl_1kg_all.csv

Setup:
    1. config.json -- same file used by skydropx_quotes.py (origin address,
       credentials, base_url). The parcel weight in config.json is IGNORED
       here in favor of --weight/--weights, so you don't have to edit
       config.json between brackets. The products[0] template (description,
       price, quantity, and the *origin* country_code -- e.g. "MX", the
       origin country of the product) is reused as-is; only hs_code
       is swapped in per destination.
    2. hs_code_by_country.csv -- one row per destination country you have
       a confirmed hs_code for: country_code,hs_code,description_en,source_note
       Add a row here as Skydropx support sends you more codes. A country
       NOT in this file is skipped (logged, not quoted) -- it's not silently
       given the wrong code.
    3. destinations_international_pending.csv (or --destinations) -- the
       full candidate list. Only rows whose country_code has a match in
       hs_code_by_country.csv are actually quoted. Point --destinations at
       a smaller, curated file (e.g. destinations_confirmed_working.csv) to
       avoid re-testing destinations already known to fail for reasons
       unrelated to weight (bad hs_code, no carrier coverage, etc).

Before running the full batch, run with --limit 3 --debug once and check
the printed "will quote" summary matches what you expect -- getting the
hs_code wrong doesn't just risk a failed quote, it risks a wrong customs
declaration on a real shipment later.
"""

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_OAUTH_URL = "https://app.skydropx.com/api/v1/oauth/token"
DEFAULT_BASE_URL = "https://api-pro.skydropx.com"

# Skydropx allows ~2 requests/second per the docs; stay comfortably under that.
REQUEST_DELAY_SECONDS = 0.6
POLL_INTERVAL_SECONDS = 3
POLL_MAX_ATTEMPTS = 40  # up to 120s per destination -- batches queue up server-side

PLACEHOLDER_ADDRESS_DEFAULTS = {
    "name": "Test Recipient",
    "company": "",
    "street1": "N/A",
    "street_number": "1",
    "apartment_number": "",
    "area_level3": "Centro",
    "phone": "0000000000",
    "email": "quotes@example.com",
    "reference": "",
}


def log(msg):
    print(msg, flush=True)


def load_config(path):
    if not path.exists():
        sys.exit(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_hs_map(path):
    if not path.exists():
        sys.exit(f"HS code map not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    hs_map = {}
    for r in rows:
        cc = (r.get("country_code") or "").strip().upper()
        code = (r.get("hs_code") or "").strip()
        if cc and code:
            hs_map[cc] = r
    if not hs_map:
        sys.exit(f"No usable rows in {path}")
    return hs_map


def http_json(method, url, token=None, body=None, form=False, max_retries=3):
    headers = {
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        ),
    }
    data = None
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"raw_error": raw}
            return e.code, parsed
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_exc = e
            if attempt < max_retries:
                wait = 3 * attempt
                log(f"  [network hiccup: {e!r}; retry {attempt}/{max_retries - 1} in {wait}s]")
                time.sleep(wait)
            else:
                log(f"  [network error after {max_retries} attempts: {e!r} -- giving up on this request]")
    return 0, {"raw_error": f"network_error after {max_retries} attempts: {last_exc!r}"}


def get_access_token(cfg):
    oauth_url = cfg.get("oauth_url", DEFAULT_OAUTH_URL)
    body = {
        "grant_type": "client_credentials",
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
    }
    if cfg.get("scope"):
        body["scope"] = cfg["scope"]
    status, resp = http_json("POST", oauth_url, body=body, form=True)
    if status not in (200, 201) or "access_token" not in resp:
        sys.exit(
            f"Failed to get an access token (HTTP {status}).\n"
            f"Response: {json.dumps(resp, indent=2, ensure_ascii=False)}\n"
            f"Check client_id/client_secret and oauth_url in your config."
        )
    log(f"Authenticated OK (token expires in {resp.get('expires_in', '?')}s).")
    return resp["access_token"]


def fill_address(row_fields, base_address):
    addr = dict(PLACEHOLDER_ADDRESS_DEFAULTS)
    addr.update({k: v for k, v in base_address.items() if v})
    for key in ("street1", "street_number", "postal_code", "area_level1",
                "area_level2", "area_level3", "country_code", "name",
                "phone", "email"):
        if row_fields.get(key):
            addr[key] = row_fields[key]
    return addr


def build_products_for(products_template, hs_code):
    """Deep-copy the config's products list, swapping in the destination-
    country-specific hs_code. Everything else (description, quantity,
    price, weight, and the product's own *origin* country_code -- e.g.
    "MX", the ORIGIN country of the product, NOT the destination) is
    left untouched."""
    if not products_template:
        return None
    out = []
    for p in products_template:
        p2 = dict(p)
        p2["hs_code"] = hs_code
        out.append(p2)
    return out


def build_parcel(weight, cfg_parcel):
    return {
        "weight": weight,
        "weight_unit": cfg_parcel.get("weight_unit", "kg"),
        "height": cfg_parcel.get("height", 1),
        "width": cfg_parcel.get("width", 20),
        "length": cfg_parcel.get("length", 10),
        "dimension_unit": cfg_parcel.get("dimension_unit", "cm"),
    }


def build_quotation_body(origin, destination, parcel, products):
    body = {
        "quotation": {
            "address_from": origin,
            "address_to": destination,
            "parcels": [parcel],
        }
    }
    if products:
        body["quotation"]["products"] = products
    return body


def create_quotation(base_url, token, origin, destination, parcel, products, debug_dir, label):
    url = f"{base_url}/api/v1/quotations"
    body = build_quotation_body(origin, destination, parcel, products)
    status, resp = http_json("POST", url, token=token, body=body)
    if debug_dir:
        _dump_debug(debug_dir, label, "01_create_request", body)
        _dump_debug(debug_dir, label, "02_create_response", {"status": status, "body": resp})
    if status not in (200, 201):
        return None, status, resp
    quotation_id = resp.get("id") or resp.get("data", {}).get("id")
    if not quotation_id:
        return None, status, resp
    return quotation_id, status, resp


def poll_quotation(base_url, token, quotation_id, debug_dir, label):
    """Poll until Skydropx marks the quotation is_completed=True. Do NOT
    stop early just because `rates` is non-empty -- Skydropx assigns each
    carrier a rate_id (with a null total) before pricing finishes."""
    url = f"{base_url}/api/v1/quotations/{quotation_id}"
    status, resp = None, {}
    for attempt in range(1, POLL_MAX_ATTEMPTS + 1):
        status, resp = http_json("GET", url, token=token)
        if debug_dir:
            _dump_debug(debug_dir, label, f"03_poll_{attempt:02d}", {"status": status, "body": resp})
        if status != 200:
            return status, resp
        data = resp.get("data", resp)
        if data.get("is_completed"):
            return status, resp
        time.sleep(POLL_INTERVAL_SECONDS)
    return status, resp


def extract_rates(resp):
    data = resp.get("data", resp) if isinstance(resp, dict) else {}
    rates = data.get("rates") or resp.get("rates") or []
    rows = []
    for r in rates:
        rows.append({
            "carrier": r.get("carrier_name") or r.get("provider_name") or r.get("provider") or "",
            "service": r.get("service") or r.get("service_level_name") or r.get("service_level") or "",
            "total": r.get("total") or r.get("amount") or r.get("price") or "",
            "currency": r.get("currency") or "",
            "eta_days": r.get("eta_days") or r.get("days") or r.get("delivery_estimate") or "",
            "import_duty_amount": r.get("import_duty_amount") or "",
            "rate_id": r.get("id") or "",
            "shipment_creation_type": r.get("shipment_creation_type") or "",
        })
    return rows


def _dump_debug(debug_dir, label, name, payload):
    safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
    out = debug_dir / f"{safe_label}__{name}.json"
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def read_destinations(path):
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = [dict(row) for row in csv.DictReader(f)]
    if not rows:
        sys.exit(f"No destination rows found in {path}")
    return rows


def parse_weights(args):
    """--weights (comma-separated, e.g. "1,2,3") takes priority over the
    single --weight when both are given, so one run can cover several
    brackets and write them all to one output file."""
    if args.weights:
        try:
            values = [float(w.strip()) for w in args.weights.split(",") if w.strip()]
        except ValueError:
            sys.exit(f"--weights must be a comma-separated list of numbers, got: {args.weights!r}")
        if not values:
            sys.exit("--weights parsed to an empty list.")
        return values
    return [args.weight]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--destinations", default="destinations_international_pending.csv")
    parser.add_argument("--hs-map", default="hs_code_by_country.csv")
    parser.add_argument("--output", default="quotes_intl_1kg.csv")
    parser.add_argument("--weight", type=float, default=1.0, help="Parcel weight in kg (overrides config.json's parcel.weight). Ignored if --weights is given.")
    parser.add_argument("--weights", default=None, help="Comma-separated list of parcel weights in kg to quote in ONE run, e.g. '1,2,3' -- all brackets land in the same --output file (a 'weight' column tells them apart). Overrides --weight.")
    parser.add_argument("--limit", type=int, default=None, help="Only quote the first N eligible destinations per weight bracket (for testing).")
    parser.add_argument("--debug", action="store_true", help="Save raw request/response JSON for each destination/weight.")
    parser.add_argument(
        "--fallback-hs-code", default=None,
        help=(
            "For any destination country NOT in --hs-map, try this code instead of skipping it. "
            "Example: a plain, unsubdivided 6-digit WCO heading for your product's general category -- "
            "used here only as an illustration of the pattern: some countries resolve a product down to "
            "a plain, unsubdivided WCO heading with no further national digits, so that bare heading is "
            "a reasonable thing to TRY first for other similar countries. It is NOT confirmed "
            "for them, though: a country whose schedule needs a longer, more specific code (like US, CA, "
            "CN and CO all did) will most likely just reject it cleanly (create_failed / no_rates), which "
            "is fine -- it tells you that country needs its own code from Skydropx. This is for RATE "
            "TESTING ONLY -- never use a fallback-guessed code as the customs declaration on a real "
            "shipment; get that confirmed by Skydropx or a broker first."
        ),
    )
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    base_url = cfg.get("base_url", DEFAULT_BASE_URL)
    origin = cfg["origin"]
    cfg_parcel = cfg.get("parcel", {})
    weight_list = parse_weights(args)

    products_template = cfg.get("products")
    if not products_template:
        sys.exit("config.json has no 'products' section -- required for international quotes.")

    hs_map = load_hs_map(Path(args.hs_map))
    all_dest_rows = read_destinations(Path(args.destinations))

    # eligible entries carry (row, hs_code, source) -- source is "confirmed" (from
    # --hs-map) or "fallback_guess" (from --fallback-hs-code, when a country has
    # no confirmed code). Skipped rows have no code at all and are left out.
    # This does not depend on weight, so it's computed once and reused for every
    # weight bracket below.
    eligible, skipped_countries = [], {}
    for row in all_dest_rows:
        cc = (row.get("country_code") or "").strip().upper()
        if cc in hs_map:
            eligible.append((row, hs_map[cc]["hs_code"], "confirmed"))
        elif args.fallback_hs_code:
            eligible.append((row, args.fallback_hs_code, "fallback_guess"))
        else:
            skipped_countries[cc] = skipped_countries.get(cc, 0) + 1

    if args.limit:
        eligible = eligible[: args.limit]

    log(f"Weight bracket(s) for this run: {', '.join(str(w) for w in weight_list)} kg")
    log(f"\n{len(eligible)} of {len(all_dest_rows)} destinations will be quoted (per weight bracket):")
    by_country = {}
    for row, hs_code, source in eligible:
        cc = row["country_code"].strip().upper()
        by_country.setdefault(cc, {"n": 0, "hs_code": hs_code, "source": source})
        by_country[cc]["n"] += 1
    for cc in sorted(by_country):
        info = by_country[cc]
        tag = "CONFIRMED" if info["source"] == "confirmed" else "fallback guess, unconfirmed"
        log(f"  {cc}: {info['n']} destination(s) -> hs_code {info['hs_code']}  [{tag}]")
    if skipped_countries:
        log(f"\n{sum(skipped_countries.values())} destinations SKIPPED (no hs_code, no --fallback-hs-code given) across "
            f"{len(skipped_countries)} countries: {', '.join(sorted(skipped_countries))}")
    total_planned = len(eligible) * len(weight_list)
    log(f"\nTotal destination x weight combinations this run: {total_planned}\n")

    debug_dir = None
    if args.debug:
        debug_dir = Path("debug")
        debug_dir.mkdir(exist_ok=True)
        log(f"Debug mode on: raw request/response JSON will be saved under {debug_dir}/")

    token = get_access_token(cfg)

    fieldnames = ["destination", "country_code", "hs_code_used", "hs_code_source", "weight", "weight_unit",
                  "height", "width", "length", "dimension_unit", "quotation_id",
                  "carrier", "service", "total", "currency", "eta_days",
                  "import_duty_amount", "rate_id", "shipment_creation_type", "error"]
    out_path = Path(args.output)
    out_file = out_path.open("w", encoding="utf-8", newline="")
    writer = csv.DictWriter(out_file, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    out_file.flush()

    def emit(row_dict):
        writer.writerow(row_dict)
        out_file.flush()

    total_rows = 0
    combo_index = 0
    for wt in weight_list:
        parcel = build_parcel(wt, cfg_parcel)
        log(f"\n=== Parcel weight: {parcel['weight']}{parcel['weight_unit']} "
            f"({parcel['length']}x{parcel['width']}x{parcel['height']}{parcel['dimension_unit']}) ===")

        for i, (row, hs_code, source) in enumerate(eligible, start=1):
            combo_index += 1
            cc = row["country_code"].strip().upper()
            label = (row.get("label") or f"destination_{i}").strip()
            debug_label = f"{label}__{parcel['weight']}kg"
            destination = fill_address(row, {})
            products = build_products_for(products_template, hs_code)

            log(f"[{combo_index}/{total_planned}] Quoting '{label}' at {parcel['weight']}kg "
                f"({destination.get('postal_code')}, {destination.get('area_level2')}, {cc}) "
                f"with hs_code {hs_code} [{source}]...")

            time.sleep(REQUEST_DELAY_SECONDS)
            quotation_id, status, resp = create_quotation(
                base_url, token, origin, destination, parcel, products, debug_dir, debug_label
            )
            base_row = {
                "destination": label, "country_code": cc, "hs_code_used": hs_code, "hs_code_source": source,
                "weight": parcel["weight"], "weight_unit": parcel["weight_unit"],
                "height": parcel["height"], "width": parcel["width"], "length": parcel["length"],
                "dimension_unit": parcel["dimension_unit"],
            }

            if not quotation_id:
                log(f"  -> Failed to create quotation (HTTP {status}). "
                    f"Response: {json.dumps(resp, ensure_ascii=False)[:500]}")
                row_out = {**base_row, "error": f"create_failed HTTP {status}"}
                total_rows += 1
                emit(row_out)
                continue

            time.sleep(REQUEST_DELAY_SECONDS)
            status, resp = poll_quotation(base_url, token, quotation_id, debug_dir, debug_label)
            rates = extract_rates(resp) if status == 200 else []
            if not rates:
                log(f"  -> No rates returned (HTTP {status}). "
                    f"Response: {json.dumps(resp, ensure_ascii=False)[:500]}")
                row_out = {**base_row, "quotation_id": quotation_id, "error": f"no_rates HTTP {status}"}
                total_rows += 1
                emit(row_out)
                continue

            log(f"  -> {len(rates)} rate(s) returned.")
            for r in rates:
                row_out = {**base_row, "quotation_id": quotation_id, **r}
                total_rows += 1
                emit(row_out)

    out_file.close()
    log(f"\nDone. Wrote {total_rows} row(s) across {len(weight_list)} weight bracket(s) to {args.output}")


if __name__ == "__main__":
    main()
