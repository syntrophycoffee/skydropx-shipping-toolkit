#!/usr/bin/env python3
"""
check_product_catalog.py
-------------------------
Queries Skydropx's own product catalog (GET /api/v1/products) instead of
guessing hs_codes by trial-and-error quotation calls. The idea: if Skydropx
says they "activated the hs_code in all countries", that should show up as
registered products/catalog entries we can read directly, rather than
something we have to rediscover one failed quotation at a time.

Writes:
    catalog_raw.json      -- the full, unmodified API response(s), paginated
                              or not, exactly as returned (for debugging if
                              the schema doesn't match what this script guesses).
    catalog_products.csv  -- best-effort flattened view: one row per product
                              entry found, with every top-level field as a
                              column (nested dicts/lists are JSON-dumped into
                              their cell rather than dropped, so nothing is
                              silently lost).

Usage:
    python3 check_product_catalog.py
    python3 check_product_catalog.py --config config.json
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_OAUTH_URL = "https://app.skydropx.com/api/v1/oauth/token"
DEFAULT_BASE_URL = "https://api-pro.skydropx.com"


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
    import time
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


def extract_list(payload):
    """Skydropx (like a lot of Rails/JSON:API-ish backends) might return a
    bare list, {"data": [...]}, or {"products": [...]}. Try the common
    shapes rather than assuming one."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "products", "items", "results"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return None


def extract_next_page(payload, current_page):
    """Look for common pagination hints. Returns the next page number, or
    None if there's no evidence of more pages."""
    if not isinstance(payload, dict):
        return None
    meta = payload.get("meta") or {}
    links = payload.get("links") or {}
    if isinstance(meta, dict):
        total_pages = meta.get("total_pages") or meta.get("totalPages")
        if total_pages is not None:
            return current_page + 1 if current_page < int(total_pages) else None
        current = meta.get("current_page") or meta.get("page")
        total = meta.get("total") or meta.get("total_count")
        per_page = meta.get("per_page") or meta.get("pageSize")
        if current is not None and total is not None and per_page:
            if int(current) * int(per_page) < int(total):
                return current_page + 1
    if isinstance(links, dict) and links.get("next"):
        return current_page + 1
    return None


def flatten_row(entry):
    row = {}
    if not isinstance(entry, dict):
        return {"value": json.dumps(entry, ensure_ascii=False)}
    for k, v in entry.items():
        if isinstance(v, (dict, list)):
            row[k] = json.dumps(v, ensure_ascii=False)
        else:
            row[k] = v
    return row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--raw-output", default="catalog_raw.json")
    parser.add_argument("--csv-output", default="catalog_products.csv")
    parser.add_argument("--max-pages", type=int, default=20)
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    base_url = cfg.get("base_url", DEFAULT_BASE_URL)
    token = get_access_token(cfg)

    all_pages_raw = []
    all_entries = []
    page = 1
    seen_any_list = False

    while True:
        url = f"{base_url}/api/v1/products"
        if page > 1:
            url += f"?page={page}"
        log(f"GET {url}")
        status, payload = http_json("GET", url, token=token)
        all_pages_raw.append({"page": page, "status": status, "body": payload})

        if status not in (200, 201):
            log(f"  -> HTTP {status}. Response: {json.dumps(payload, indent=2, ensure_ascii=False)[:2000]}")
            break

        entries = extract_list(payload)
        if entries is None:
            log("  -> 200 OK but couldn't find a list of products in the response shape.")
            log(f"  -> Top-level keys: {list(payload.keys()) if isinstance(payload, dict) else type(payload)}")
            break

        seen_any_list = True
        log(f"  -> {len(entries)} product(s) on page {page}.")
        all_entries.extend(entries)

        next_page = extract_next_page(payload, page)
        if next_page is None or page >= args.max_pages:
            break
        page = next_page

    Path(args.raw_output).write_text(
        json.dumps(all_pages_raw, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    log(f"\nWrote full raw response(s) to {args.raw_output}")

    if not seen_any_list:
        log(
            "\nCould not locate a product list in the response -- check "
            f"{args.raw_output} by hand to see the actual shape Skydropx "
            "returned (the endpoint may need different params, or may not "
            "be the right one for a per-country hs_code catalog)."
        )
        return

    if not all_entries:
        log("\nEndpoint reachable but returned zero products.")
        return

    # Union of all field names across all entries, in first-seen order.
    fieldnames = []
    flat_rows = []
    for entry in all_entries:
        row = flatten_row(entry)
        flat_rows.append(row)
        for k in row:
            if k not in fieldnames:
                fieldnames.append(k)

    import csv
    with open(args.csv_output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in flat_rows:
            w.writerow(row)

    log(f"Wrote {len(flat_rows)} product row(s) to {args.csv_output}")
    log(f"Columns found: {fieldnames}")


if __name__ == "__main__":
    main()
