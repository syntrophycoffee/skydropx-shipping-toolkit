#!/usr/bin/env python3
"""
repoll_quotations.py
---------------------
Fixes an incomplete quotes CSV produced before the polling bug in
skydropx_quotes.py was fixed. Instead of re-creating every quotation
(which burns a fresh batch of API calls), this re-uses the quotation_id
already stored in each row and re-polls just the ones that came back
with blank prices, waiting properly for is_completed=true this time.

Usage:
    python3 repoll_quotations.py --config config.json --input quotes_usa_working.csv --output quotes_usa_fixed.csv
"""
import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

POLL_INTERVAL_SECONDS = 3
POLL_MAX_ATTEMPTS = 40
REQUEST_DELAY_SECONDS = 0.6

DEFAULT_OAUTH_URL = "https://app.skydropx.com/api/v1/oauth/token"
DEFAULT_BASE_URL = "https://api-pro.skydropx.com"


def log(msg):
    print(msg, flush=True)


def http_json(method, url, token=None, body=None, form=False):
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


def get_access_token(cfg):
    oauth_url = cfg.get("oauth_url", DEFAULT_OAUTH_URL)
    body = {
        "grant_type": "client_credentials",
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
    }
    status, resp = http_json("POST", oauth_url, body=body, form=True)
    if status not in (200, 201) or "access_token" not in resp:
        sys.exit(f"Failed to get access token (HTTP {status}): {resp}")
    log(f"Authenticated OK (token expires in {resp.get('expires_in', '?')}s).")
    return resp["access_token"]


def poll_quotation(base_url, token, quotation_id):
    url = f"{base_url}/api/v1/quotations/{quotation_id}"
    status, resp = None, {}
    for attempt in range(1, POLL_MAX_ATTEMPTS + 1):
        status, resp = http_json("GET", url, token=token)
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
            "currency": r.get("currency") or r.get("currency_code") or "",
            "eta_days": r.get("eta_days") or r.get("days") or r.get("delivery_estimate") or "",
            "import_duty_amount": r.get("import_duty_amount") or "",
            "rate_id": r.get("id") or "",
            "shipment_creation_type": r.get("shipment_creation_type") or "",
            "error_messages": r.get("error_messages") or "",
        })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    args = ap.parse_args()

    cfg = json.load(open(args.config, encoding="utf-8"))
    base_url = cfg.get("base_url", DEFAULT_BASE_URL)

    with open(args.input, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    if "error_messages" not in fieldnames:
        fieldnames = list(fieldnames) + ["error_messages"]

    by_dest = defaultdict(list)
    for row in rows:
        by_dest[row["destination"]].append(row)

    needs_fix = {}
    for dest, drows in by_dest.items():
        if any(not r["total"].strip() for r in drows):
            qid = drows[0]["quotation_id"]
            if qid:
                needs_fix[dest] = qid

    log(f"{len(by_dest)} destinations total, {len(needs_fix)} need a repoll.")
    if not needs_fix:
        log("Nothing to fix -- copying input to output unchanged.")
        with open(args.output, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        return

    token = get_access_token(cfg)

    fixed_rates = {}
    still_incomplete = []
    for i, (dest, qid) in enumerate(needs_fix.items(), 1):
        log(f"[{i}/{len(needs_fix)}] Re-polling {dest} ({qid}) ...")
        status, resp = poll_quotation(base_url, token, qid)
        if status != 200:
            log(f"  -> HTTP {status}, skipping (kept old rows). {resp}")
            still_incomplete.append(dest)
            time.sleep(REQUEST_DELAY_SECONDS)
            continue
        data = resp.get("data", resp)
        new_rates = extract_rates(resp)
        priced = sum(1 for r in new_rates if str(r["total"]).strip())
        log(f"  -> is_completed={data.get('is_completed')}, {priced}/{len(new_rates)} rates priced.")
        if not data.get("is_completed"):
            still_incomplete.append(dest)
        fixed_rates[dest] = new_rates
        time.sleep(REQUEST_DELAY_SECONDS)

    # Rebuild the output: keep original rows for untouched destinations,
    # replace rows for repolled destinations with the fresh rate list
    # (carrying over the shared per-destination fields from row 0).
    out_rows = []
    for dest, drows in by_dest.items():
        if dest in fixed_rates:
            base = drows[0]
            for r in fixed_rates[dest]:
                new_row = {k: base.get(k, "") for k in fieldnames}
                new_row.update({
                    "carrier": r["carrier"],
                    "service": r["service"],
                    "total": r["total"],
                    "currency": r["currency"],
                    "eta_days": r["eta_days"],
                    "import_duty_amount": r["import_duty_amount"],
                    "rate_id": r["rate_id"],
                    "shipment_creation_type": r["shipment_creation_type"],
                    "error_messages": r.get("error_messages", ""),
                    "error": base.get("error", ""),
                })
                out_rows.append(new_row)
        else:
            for r in drows:
                r.setdefault("error_messages", "")
                out_rows.append(r)

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(out_rows)

    log(f"Wrote {args.output}.")
    if still_incomplete:
        log(f"Still incomplete after max poll attempts ({POLL_MAX_ATTEMPTS}x{POLL_INTERVAL_SECONDS}s): {still_incomplete}")
        log("These may need a fresh quotation (old quotation_id may have gone stale) -- rerun skydropx_quotes.py just for these rows if so.")


if __name__ == "__main__":
    main()
