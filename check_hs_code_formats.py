#!/usr/bin/env python3
"""
check_hs_code_formats.py
--------------------------
Diagnostic only -- NOT for real shipments. If Skydropx keeps rejecting your
product's customs code for a given destination country with:

    "No existe el código harmonizado del producto."

there are two very different possible explanations, and the error message
alone doesn't tell you which one you're looking at:

  (a) the VALUE/FORMAT of the code is wrong (right idea, wrong digits or
      punctuation) -- in which case some other format of the same heading
      might get through, or
  (b) that destination country simply has NO version of your product
      registered at all in Skydropx's catalog for your account, regardless
      of format -- in which case every variant you try will fail
      identically, no matter how it's punctuated.

This script tells them apart by trying a batch of structurally different
format variants against ONE destination country, plus a "control" value:
a code for a COMPLETELY different, unrelated product that you already know
Skydropx accepts for that same country (e.g. from a working quote, or from
whatever reference/catalog data your account has). If the control succeeds
while every variant of your actual product's code fails, that's strong
evidence for (b): the problem is a missing catalog entry, not a formatting
mistake on your end -- and no amount of reformatting will fix it. You'll
need the destination-country-specific code from Skydropx support (or a
customs broker), not a guess.

IMPORTANT: Edit CANDIDATE_CODES and CONTROL_CODE below for your own product
and account before running this. The values shipped here are placeholders
for illustration only -- never use any output from this script as a real
customs declaration. Only a confirmed, destination-country-specific code
from Skydropx or a customs broker belongs on an actual shipment.

Usage:
    python3 check_hs_code_formats.py --country DE
    python3 check_hs_code_formats.py --country JP --label "Tokyo"
    python3 check_hs_code_formats.py --country DE --debug
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
POLL_INTERVAL_SECONDS = 3
POLL_MAX_ATTEMPTS = 40

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

# ---------------------------------------------------------------------------
# EDIT ME: replace these with your own product's heading and formats to try.
# The ones below are placeholders only, chosen purely to illustrate the
# different ways a customs code can be punctuated/padded -- they are NOT
# validated against any real product or account.
# ---------------------------------------------------------------------------
CANDIDATE_CODES = [
    ("0000",          "bare 4-digit WCO heading, no subheading"),
    ("0000.00",       "6-digit WCO subheading, dotted"),
    ("000000",        "6-digit WCO subheading, no dot"),
    ("0000.0000",     "8-digit, padded"),
    ("00000000",      "8-digit, no dot"),
    ("0000.000000",   "10-digit, dotted -- this is your normal fallback/guess code; include it here as a baseline control"),
    ("0000000000",    "10-digit, no dot"),
    ("0000.00.00.00", "10-digit, dotted every 2 digits (alternate punctuation style)"),
    ("0000.90",       "6-digit catch-all 'other' heading, dotted"),
    ("0000.900000",   "10-digit catch-all 'other', dotted"),
]

# EDIT ME: a code for a DIFFERENT, unrelated product that you already know
# is accepted by Skydropx for this same destination country -- e.g. pull one
# from a past successful quote, or from any reference catalog your account
# has access to. This is the control: if it succeeds while every candidate
# above fails, the problem is a missing catalog entry for YOUR product in
# THIS country, not a formatting issue.
CONTROL_CODE = ("0000.00", "CONTROL: a code for an unrelated product you already know this country accepts -- "
                            "replace this with a real known-good value for your account before running. "
                            "If this succeeds while every candidate above fails, that confirms the gap is "
                            "a missing catalog entry for your product, not a format problem.")


def log(msg):
    print(msg, flush=True)


def load_config(path):
    if not path.exists():
        sys.exit(f"Config file not found: {path}")
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
            last_exc = e
            if attempt < max_retries:
                wait = 3 * attempt
                log(f"  [network hiccup: {e!r}; retry {attempt}/{max_retries - 1} in {wait}s]")
                time.sleep(wait)
            else:
                log(f"  [network error after {max_retries} attempts: {e!r} -- giving up]")
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


def build_parcel(weight, cfg_parcel):
    return {
        "weight": weight,
        "weight_unit": cfg_parcel.get("weight_unit", "kg"),
        "height": cfg_parcel.get("height", 1),
        "width": cfg_parcel.get("width", 20),
        "length": cfg_parcel.get("length", 10),
        "dimension_unit": cfg_parcel.get("dimension_unit", "cm"),
    }


def build_products_for(products_template, hs_code):
    if not products_template:
        return None
    out = []
    for p in products_template:
        p2 = dict(p)
        p2["hs_code"] = hs_code
        out.append(p2)
    return out


def build_quotation_body(origin, destination, parcel, products):
    body = {"quotation": {"address_from": origin, "address_to": destination, "parcels": [parcel]}}
    if products:
        body["quotation"]["products"] = products
    return body


def _dump_debug(debug_dir, label, name, payload):
    safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
    out = debug_dir / f"{safe_label}__{name}.json"
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


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


def find_destination_row(destinations_path, country_code, label_filter=None):
    with open(destinations_path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    matches = [r for r in rows if (r.get("country_code") or "").strip().upper() == country_code.upper()]
    if label_filter:
        narrowed = [r for r in matches if label_filter.lower() in r.get("label", "").lower()]
        if narrowed:
            matches = narrowed
    if not matches:
        sys.exit(f"No destination row found for country_code={country_code!r} in {destinations_path}")
    return matches[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--destinations", default="destinations_sample_international.csv")
    parser.add_argument("--country", default="DE", help="Destination country_code to probe (default: DE).")
    parser.add_argument("--label", default=None, help="Substring to pick a specific city if the country has several rows (default: first match).")
    parser.add_argument("--weight", type=float, default=1.0)
    parser.add_argument("--output", default="hs_code_format_probe.csv")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    base_url = cfg.get("base_url", DEFAULT_BASE_URL)
    origin = cfg["origin"]
    cfg_parcel = cfg.get("parcel", {})
    products_template = cfg.get("products")
    if not products_template:
        sys.exit("config.json has no 'products' section -- required for international quotes.")

    row = find_destination_row(Path(args.destinations), args.country, args.label)
    destination = fill_address(row, origin)
    parcel = build_parcel(args.weight, cfg_parcel)

    all_candidates = list(CANDIDATE_CODES) + [CONTROL_CODE]

    log(f"Probing hs_code formats against: {row.get('label')} "
        f"({row.get('postal_code')}, {row.get('area_level2')}, {args.country.upper()}) at {args.weight}kg")
    log(f"{len(all_candidates)} code variant(s) to try (including 1 control). THIS IS DIAGNOSTIC ONLY -- "
        f"none of these values are confirmed for real customs use. Edit CANDIDATE_CODES/CONTROL_CODE "
        f"in this script before running against your own product.\n")

    debug_dir = None
    if args.debug:
        debug_dir = Path("debug")
        debug_dir.mkdir(exist_ok=True)

    token = get_access_token(cfg)

    fieldnames = ["country_code", "destination", "hs_code_tried", "format_note",
                  "create_status", "result", "rate_count", "error_detail"]
    with open(args.output, "w", newline="", encoding="utf-8") as out_file:
        writer = csv.DictWriter(out_file, fieldnames=fieldnames)
        writer.writeheader()

        for i, (code, note) in enumerate(all_candidates, start=1):
            products = build_products_for(products_template, code)
            debug_label = f"{args.country.upper()}__format_probe__{i:02d}_{code}"
            log(f"[{i}/{len(all_candidates)}] Trying hs_code={code!r} ({note})...")

            quotation_id, status, resp = create_quotation(
                base_url, token, origin, destination, parcel, products, debug_dir, debug_label
            )

            row_out = {
                "country_code": args.country.upper(),
                "destination": row.get("label"),
                "hs_code_tried": code,
                "format_note": note,
                "create_status": status,
                "result": "",
                "rate_count": 0,
                "error_detail": "",
            }

            if quotation_id is None:
                err = json.dumps(resp, ensure_ascii=False)
                row_out["result"] = "create_failed"
                row_out["error_detail"] = err
                log(f"  -> REJECTED (HTTP {status}): {err}")
            else:
                poll_status, poll_resp = poll_quotation(base_url, token, quotation_id, debug_dir, debug_label)
                data = poll_resp.get("data", poll_resp) if isinstance(poll_resp, dict) else {}
                rates = data.get("rates") or poll_resp.get("rates") or []
                if rates:
                    row_out["result"] = "ACCEPTED"
                    row_out["rate_count"] = len(rates)
                    log(f"  -> ACCEPTED: {len(rates)} rate(s) returned. *** This format got through -- "
                        f"still needs confirmation from your carrier before trusting it for a real shipment. ***")
                else:
                    row_out["result"] = "accepted_but_no_rates"
                    row_out["error_detail"] = json.dumps(poll_resp, ensure_ascii=False)[:500]
                    log(f"  -> quotation created but no rates returned (poll status {poll_status}).")

            writer.writerow(row_out)
            out_file.flush()
            time.sleep(0.6)

    log(f"\nDone. Wrote {len(all_candidates)} result(s) to {args.output}")
    log("Reminder: any 'ACCEPTED' result here is a lead to verify with your carrier, not a confirmed code.")


if __name__ == "__main__":
    main()
