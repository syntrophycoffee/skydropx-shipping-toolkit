#!/usr/bin/env python3
"""
skydropx_quotes.py
-------------------
Compares shipping cost for ONE fixed package (your own standard product box/bag)
across a list of destinations, using the Skydropx quotation API.

Usage:
    python3 skydropx_quotes.py
    python3 skydropx_quotes.py --config config.json --destinations destinations.csv --output quotes_output.csv
    python3 skydropx_quotes.py --limit 2 --debug        # test on the first 2 rows, save raw API responses

Setup (one-time):
    1. Copy config.example.json -> config.json and fill in:
         - client_id / client_secret (from your Skydropx account)
         - origin address (your shipping-from address)
         - parcel (weight + dimensions of the fixed package you're comparing)
    2. Copy destinations_template.csv -> destinations.csv and fill in one row
       per destination you want a quote for. Only "label", "postal_code",
       "city", "state" and "country_code" are required; the rest are filled
       with harmless placeholder values if left blank (Skydropx's schema
       wants a full address, but only these fields affect the rate).
    3. Run the script. It writes quotes_output.csv with every rate returned
       for every destination.

Notes on the API:
    Skydropx's public API docs page is a JavaScript app, so the exact field
    names below were reconstructed from what could be extracted from it, not
    from a raw OpenAPI spec. Run once with `--limit 1 --debug` first: it saves
    the raw request/response JSON to a debug/ folder next to this script so
    you can confirm the field names match before running the full batch. If
    Skydropx rejects a request, the script prints the full error body it
    returned, which usually names the field that's wrong.
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
        sys.exit(
            f"Config file not found: {path}\n"
            f"Copy config.example.json to {path.name} and fill it in first."
        )
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


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
            # transient network blip (read timeout, connection reset, DNS hiccup, etc.)
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
    """Merge a destination CSV row into a full address object, using config
    defaults / harmless placeholders for anything not supplied per-row."""
    addr = dict(PLACEHOLDER_ADDRESS_DEFAULTS)
    addr.update({k: v for k, v in base_address.items() if v})
    for key in ("street1", "street_number", "postal_code", "area_level1",
                "area_level2", "area_level3", "country_code", "name",
                "phone", "email"):
        if row_fields.get(key):
            addr[key] = row_fields[key]
    return addr


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
    """Poll until Skydropx marks the quotation is_completed=True.

    IMPORTANT: do NOT stop early just because `rates` is non-empty --
    Skydropx assigns each carrier a rate_id (with a null `total`) before
    it finishes pricing, so a non-empty `rates` array is not proof the
    quote is done. Under load (large batches) this used to cause the
    script to grab a half-priced snapshot. Only `is_completed: true` (or
    running out of attempts) ends the wait.
    """
    url = f"{base_url}/api/v1/quotations/{quotation_id}"
    status, resp = None, {}
    for attempt in range(1, POLL_MAX_ATTEMPTS + 1):
        status, resp = http_json("GET", url, token=token)
        if debug_dir:
            _dump_debug(debug_dir, label, f"03_poll_{attempt:02d}", {"status": status, "body": resp})
        if status != 200:
            return status, resp
        data = resp.get("data", resp)
        is_completed = data.get("is_completed")
        if is_completed:
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
        reader = csv.DictReader(f)
        rows = [dict(row) for row in reader]
    if not rows:
        sys.exit(f"No destination rows found in {path}")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--destinations", default="destinations.csv")
    parser.add_argument("--output", default="quotes_output.csv")
    parser.add_argument("--limit", type=int, default=None, help="Only quote the first N destinations (for testing).")
    parser.add_argument("--debug", action="store_true", help="Save raw request/response JSON for each destination.")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    base_url = cfg.get("base_url", DEFAULT_BASE_URL)
    origin = cfg["origin"]
    parcel = cfg["parcel"]
    parcel_info = {
        "weight": parcel.get("weight"),
        "weight_unit": parcel.get("weight_unit", "kg"),
        "height": parcel.get("height"),
        "width": parcel.get("width"),
        "length": parcel.get("length"),
        "dimension_unit": parcel.get("dimension_unit", "cm"),
    }
    products = cfg.get("products")  # only needed for international shipments

    dest_rows = read_destinations(Path(args.destinations))
    if args.limit:
        dest_rows = dest_rows[: args.limit]

    debug_dir = None
    if args.debug:
        debug_dir = Path("debug")
        debug_dir.mkdir(exist_ok=True)
        log(f"Debug mode on: raw request/response JSON will be saved under {debug_dir}/")

    token = get_access_token(cfg)

    fieldnames = ["destination", "weight", "weight_unit", "height", "width", "length",
                  "dimension_unit", "quotation_id", "carrier", "service", "total",
                  "currency", "eta_days", "import_duty_amount", "rate_id",
                  "shipment_creation_type", "error"]
    out_path = Path(args.output)
    out_file = out_path.open("w", encoding="utf-8", newline="")
    writer = csv.DictWriter(out_file, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    out_file.flush()

    def emit(row_dict):
        writer.writerow(row_dict)
        out_file.flush()

    all_rows = []
    for i, row in enumerate(dest_rows, start=1):
        label = (row.get("label") or f"destination_{i}").strip()
        destination = fill_address(row, {})
        log(f"[{i}/{len(dest_rows)}] Quoting for '{label}' "
            f"({destination.get('postal_code')}, {destination.get('area_level2')}, "
            f"{destination.get('area_level1')}, {destination.get('country_code')})...")

        is_international = destination.get("country_code", "").upper() != origin.get("country_code", "").upper()
        dest_products = products if is_international else None

        time.sleep(REQUEST_DELAY_SECONDS)
        quotation_id, status, resp = create_quotation(
            base_url, token, origin, destination, parcel, dest_products, debug_dir, label
        )
        if not quotation_id:
            log(f"  -> Failed to create quotation (HTTP {status}). "
                f"Response: {json.dumps(resp, ensure_ascii=False)[:500]}")
            row_out = {"destination": label, **parcel_info, "error": f"create_failed HTTP {status}"}
            all_rows.append(row_out)
            emit(row_out)
            continue

        time.sleep(REQUEST_DELAY_SECONDS)
        status, resp = poll_quotation(base_url, token, quotation_id, debug_dir, label)
        rates = extract_rates(resp) if status == 200 else []
        if not rates:
            log(f"  -> No rates returned (HTTP {status}). "
                f"Response: {json.dumps(resp, ensure_ascii=False)[:500]}")
            row_out = {"destination": label, **parcel_info, "error": f"no_rates HTTP {status}"}
            all_rows.append(row_out)
            emit(row_out)
            continue

        log(f"  -> {len(rates)} rate(s) returned.")
        for r in rates:
            row_out = {"destination": label, **parcel_info, "quotation_id": quotation_id, **r}
            all_rows.append(row_out)
            emit(row_out)

    out_file.close()
    log(f"\nDone. Wrote {len(all_rows)} row(s) to {args.output}")


def write_output(path, rows):
    fieldnames = ["destination", "weight", "weight_unit", "height", "width", "length",
                  "dimension_unit", "quotation_id", "carrier", "service", "total",
                  "currency", "eta_days", "import_duty_amount", "rate_id",
                  "shipment_creation_type", "error"]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


if __name__ == "__main__":
    main()
