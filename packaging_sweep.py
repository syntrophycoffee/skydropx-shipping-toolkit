#!/usr/bin/env python3
"""
packaging_sweep.py
-------------------
Answers: "what's the cheapest Skydropx rate for a given package, and how does
that change if I repackage it?"

Skydropx has no "give me your lowest price" endpoint — every quote is priced
against one specific origin + destination + parcel (weight/dimensions), and
carriers apply their own weight/DIM-weight brackets underneath that. So
finding "the lowest quote" means: create a quotation, look at the `rates`
array it returns, and take the minimum `total` — that's the cheapest carrier
option for that exact shipment. There's no shortcut around calling the API.

This script automates that for a PACKAGING EXPERIMENT: it holds a destination
(or every destination in a CSV) fixed and runs a whole list of candidate
parcel weights/dimensions past the API, reporting the cheapest AND fastest
rate for each. Run it against the destinations you actually ship to and
you'll see where the carriers' price brackets sit -- e.g. it's common for a
rate to jump sharply once a package crosses a weight or dimensional-weight
threshold, even by a few grams or a centimeter.

Usage:
    # One destination:
    python3 packaging_sweep.py --destination-label "Mexico City" --parcels parcels.json
    python3 packaging_sweep.py --postal-code 06700 --city "Ciudad de Mexico" \\
        --state "Ciudad de Mexico" --country MX --parcels parcels.json

    # Every destination in a CSV (e.g. all 32 Mexican states):
    python3 packaging_sweep.py --all-destinations --destinations destinations_domestic.csv \\
        --parcels parcels.json --output packaging_sweep_all_states.csv

Setup:
    Copy parcels_template.json -> parcels.json and edit the list of package
    weight/dimension combos you want to compare (see that file for the
    format). Reuses config.json from skydropx_quotes.py for credentials,
    origin address, and (for international destinations) the products/
    customs block.
"""

import argparse
import csv
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from skydropx_quotes import (  # noqa: E402
    DEFAULT_BASE_URL,
    REQUEST_DELAY_SECONDS,
    create_quotation,
    extract_rates,
    get_access_token,
    load_config,
    log,
    poll_quotation,
)


def destination_from_row(row):
    return {
        "name": "Test Recipient",
        "street1": "N/A",
        "street_number": "1",
        "postal_code": row["postal_code"],
        "area_level1": row["area_level1"],
        "area_level2": row["area_level2"],
        "area_level3": row.get("area_level3") or "Centro",
        "country_code": row["country_code"],
    }, row["label"]


def load_all_destinations(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = [row for row in csv.DictReader(f) if row.get("label", "").strip()]
    if not rows:
        sys.exit(f"No destination rows found in {path}")
    return [destination_from_row(row) for row in rows]


def load_destination_from_csv(path, label):
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("label", "").strip().lower() == label.strip().lower():
                return destination_from_row(row)
    sys.exit(f"No row with label '{label}' found in {path}")


def build_destination(args):
    if args.destination_label:
        return load_destination_from_csv(args.destinations, args.destination_label)
    missing = [n for n, v in [("--postal-code", args.postal_code), ("--city", args.city),
                              ("--state", args.state), ("--country", args.country)] if not v]
    if missing:
        sys.exit(f"Missing required destination fields: {', '.join(missing)} "
                  f"(or use --destination-label / --all-destinations instead).")
    dest = {
        "name": "Test Recipient",
        "street1": "N/A",
        "street_number": "1",
        "postal_code": args.postal_code,
        "area_level1": args.state,
        "area_level2": args.city,
        "area_level3": args.area_level3,
        "country_code": args.country,
    }
    return dest, f"{args.city}, {args.state}"


def price_of(r):
    try:
        return float(r["total"])
    except (TypeError, ValueError):
        return float("inf")


def eta_of(r):
    try:
        return float(r["eta_days"])
    except (TypeError, ValueError):
        return float("inf")


def sweep_one_destination(destination, dest_label, parcels, cfg, base_url, origin, dest_products, token, debug_dir):
    """Run every parcel in `parcels` against one destination. Returns a list of result rows."""
    results = []
    log(f"=== {dest_label} ({destination['postal_code']}, {destination['area_level2']}, "
        f"{destination['area_level1']}, {destination['country_code']}) ===")
    for i, p in enumerate(parcels, start=1):
        label = p.get("label") or f"parcel_{i}"
        parcel = {
            "weight": p["weight"],
            "height": p["height"],
            "width": p["width"],
            "length": p["length"],
            "weight_unit": p.get("weight_unit", "kg"),
            "dimension_unit": p.get("dimension_unit", "cm"),
        }
        log(f"[{i}/{len(parcels)}] {label}: {parcel['weight']}{parcel['weight_unit']}, "
            f"{parcel['height']}x{parcel['width']}x{parcel['length']}{parcel['dimension_unit']}...")

        time.sleep(REQUEST_DELAY_SECONDS)
        quotation_id, status, resp = create_quotation(
            base_url, token, origin, destination, parcel, dest_products, debug_dir, label
        )
        if not quotation_id:
            log(f"  -> Failed (HTTP {status}): {json.dumps(resp, ensure_ascii=False)[:300]}")
            results.append({"destination": dest_label, "parcel_label": label, **parcel, "error": f"create_failed HTTP {status}"})
            continue

        time.sleep(REQUEST_DELAY_SECONDS)
        status, resp = poll_quotation(base_url, token, quotation_id, debug_dir, label)
        rates = extract_rates(resp) if status == 200 else []
        if not rates:
            log(f"  -> No rates (HTTP {status}).")
            results.append({"destination": dest_label, "parcel_label": label, **parcel, "error": f"no_rates HTTP {status}"})
            continue

        cheapest = min(rates, key=price_of)
        fastest = min(rates, key=eta_of)
        log(f"  -> cheapest: {cheapest['carrier']} {cheapest['service']} "
            f"= {cheapest['total']} {cheapest['currency']} ({cheapest['eta_days']}d)  |  "
            f"fastest: {fastest['carrier']} {fastest['service']} "
            f"= {fastest['total']} {fastest['currency']} ({fastest['eta_days']}d)")
        results.append({
            "destination": dest_label, "parcel_label": label, **parcel,
            "cheapest_carrier": cheapest["carrier"],
            "cheapest_service": cheapest["service"],
            "cheapest_total": cheapest["total"],
            "cheapest_eta_days": cheapest["eta_days"],
            "currency": cheapest["currency"],
            "fastest_carrier": fastest["carrier"],
            "fastest_service": fastest["service"],
            "fastest_total": fastest["total"],
            "fastest_eta_days": fastest["eta_days"],
            "num_rates_returned": len(rates),
        })
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--parcels", default="parcels.json")
    parser.add_argument("--output", default="packaging_sweep_output.csv")
    parser.add_argument("--destination-label", help="Look up this label in --destinations (e.g. 'Mexico City').")
    parser.add_argument("--all-destinations", action="store_true",
                        help="Run the parcel sweep against EVERY row in --destinations, not just one.")
    parser.add_argument("--destinations", default="destinations.csv")
    parser.add_argument("--postal-code")
    parser.add_argument("--city")
    parser.add_argument("--state")
    parser.add_argument("--country")
    parser.add_argument("--area-level3", default="Centro",
                        help="District/colonia for the --postal-code path (default: Centro).")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--append", action="store_true",
                        help="Append to --output instead of overwriting (skip header if file already has one).")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    base_url = cfg.get("base_url", DEFAULT_BASE_URL)
    origin = cfg["origin"]
    products = cfg.get("products")

    if args.all_destinations:
        destinations = load_all_destinations(args.destinations)
    else:
        destinations = [build_destination(args)]

    parcels_path = Path(args.parcels)
    if not parcels_path.exists():
        sys.exit(f"Parcels file not found: {parcels_path}\nCopy parcels_template.json to {parcels_path.name} first.")
    parcels = json.loads(parcels_path.read_text(encoding="utf-8"))

    debug_dir = None
    if args.debug:
        debug_dir = Path("debug")
        debug_dir.mkdir(exist_ok=True)

    token = get_access_token(cfg)

    all_results = []
    for n, (destination, dest_label) in enumerate(destinations, start=1):
        if len(destinations) > 1:
            log(f"\n--- Destination {n}/{len(destinations)} ---")
        is_international = destination["country_code"].upper() != origin.get("country_code", "").upper()
        dest_products = products if is_international else None
        all_results.extend(
            sweep_one_destination(destination, dest_label, parcels, cfg, base_url, origin, dest_products, token, debug_dir)
        )

    fieldnames = ["destination", "parcel_label", "weight", "weight_unit", "height", "width", "length", "dimension_unit",
                  "cheapest_carrier", "cheapest_service", "cheapest_total", "cheapest_eta_days", "currency",
                  "fastest_carrier", "fastest_service", "fastest_total", "fastest_eta_days",
                  "num_rates_returned", "error"]
    out_path = Path(args.output)
    write_header = not (args.append and out_path.exists() and out_path.stat().st_size > 0)
    mode = "a" if args.append else "w"
    with open(out_path, mode, encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if write_header:
            w.writeheader()
        for r in all_results:
            w.writerow(r)

    log(f"\nDone. Wrote {len(all_results)} row(s) to {args.output}")
    log("Sort/group that file by destination + parcel_label to compare cheapest vs. fastest, "
        "and watch for jumps between weight rows -- that's a carrier bracket boundary.")


if __name__ == "__main__":
    main()
