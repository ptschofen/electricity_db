#!/usr/bin/env python3
"""
Fetch day-ahead electricity prices for Austria from the ENTSO-E Transparency
Platform and write them to prices.json for the dashboard front end.

Setup (one-time):
  1. Register at https://transparency.entsoe.eu/
  2. Email transparency@entsoe.eu with subject "Restful API access",
     including the email address you registered with.
  3. Once approved, generate a security token under your account settings
     on the Transparency Platform.
  4. Set it as an environment variable:
       export ENTSOE_API_TOKEN="your-token-here"     (macOS/Linux)
       setx ENTSOE_API_TOKEN "your-token-here"        (Windows, new shells)

Usage:
  python fetch_entsoe_prices.py
  python fetch_entsoe_prices.py --out /path/to/prices.json
  python fetch_entsoe_prices.py --days 2   # today + tomorrow (default)

Run this once daily in the afternoon (after ~14:00 CEST), once the next
day's auction has cleared, so you pick up tomorrow's prices too.
"""

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import urllib.request
import urllib.parse
import urllib.error

API_URL = "https://web-api.tp.entsoe.eu/api"
AUSTRIA_ZONE = "10YAT-APG------L"
NS = {"ns": "urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3"}


def fetch_raw_xml(token: str, start: datetime, end: datetime) -> str:
    """Call the ENTSO-E RESTful API for day-ahead prices (A44)."""
    params = {
        "securityToken": token,
        "documentType": "A44",
        "businessType": "A62",  # workaround required since a 2026 API regression
        "in_Domain": AUSTRIA_ZONE,
        "out_Domain": AUSTRIA_ZONE,
        "periodStart": start.strftime("%Y%m%d%H%M"),
        "periodEnd": end.strftime("%Y%m%d%H%M"),
    }
    url = f"{API_URL}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise SystemExit(
            f"ENTSO-E API returned HTTP {e.code}.\n{body[:1000]}"
        )


def parse_prices(xml_text: str):
    """Parse the A44 XML document into a flat list of {start_utc, price_eur_mwh}."""
    root = ET.fromstring(xml_text)

    # An error/acknowledgement document has a different root tag.
    if root.tag.endswith("Acknowledgement_MarketDocument"):
        reason = root.find(".//ns:Reason/ns:text", NS)
        msg = reason.text if reason is not None else "Unknown rejection reason"
        raise SystemExit(f"ENTSO-E rejected the request: {msg}")

    points = []
    for ts in root.findall("ns:TimeSeries", NS):
        period = ts.find("ns:Period", NS)
        interval_start_str = period.find("ns:timeInterval/ns:start", NS).text
        resolution = period.find("ns:resolution", NS).text  # e.g. PT60M or PT15M
        minutes = 60 if resolution == "PT60M" else 15 if resolution == "PT15M" else None
        if minutes is None:
            raise SystemExit(f"Unexpected resolution '{resolution}' — update the script.")

        interval_start = datetime.strptime(
            interval_start_str, "%Y-%m-%dT%H:%MZ"
        ).replace(tzinfo=timezone.utc)

        for point in period.findall("ns:Point", NS):
            position = int(point.find("ns:position", NS).text)
            price = float(point.find("ns:price.amount", NS).text)
            point_start = interval_start + timedelta(minutes=minutes * (position - 1))
            points.append({
                "start_utc": point_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "interval_minutes": minutes,
                "price_eur_mwh": price,
            })

    points.sort(key=lambda p: p["start_utc"])
    return points


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="prices.json", help="Output JSON path (default: prices.json)")
    parser.add_argument("--days", type=int, default=2, help="Number of days from today to fetch (default: 2 = today + tomorrow)")
    parser.add_argument("--token", default=None, help="ENTSO-E security token (else reads ENTSOE_API_TOKEN env var)")
    args = parser.parse_args()

    token = args.token or os.environ.get("ENTSOE_API_TOKEN")
    if not token:
        sys.exit("No API token found. Set ENTSOE_API_TOKEN or pass --token.")

    now_utc = datetime.now(timezone.utc)
    start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    end = start + timedelta(days=args.days + 1)

    xml_text = fetch_raw_xml(token, start, end)
    points = parse_prices(xml_text)

    if not points:
        sys.exit("No price points parsed — check the token and date range.")

    output = {
        "zone": "AT",
        "unit": "EUR_per_MWh",
        "generated_at_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "prices": points,
    }

    with open(args.out, "w") as f:
        json.dump(output, f, indent=2)

    print(f"Wrote {len(points)} price points to {args.out}")
    print(f"Range: {points[0]['start_utc']} .. {points[-1]['start_utc']}")


if __name__ == "__main__":
    main()