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
import ftplib
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


def upload_via_ftp(local_path: str, remote_path: str, host: str, user: str, password: str, use_tls: bool = True):
    """Upload the JSON file to Hostinger via FTP (or FTPS if use_tls)."""
    ftp_cls = ftplib.FTP_TLS if use_tls else ftplib.FTP
    ftp = ftp_cls(timeout=30)
    ftp.connect(host, 21)
    ftp.login(user, password)
    if use_tls:
        ftp.prot_p()  # secure the data connection too
    remote_dir = os.path.dirname(remote_path).replace("\\", "/")
    if remote_dir:
        try:
            ftp.cwd(remote_dir)
        except ftplib.error_perm as e:
            print(f"Couldn't cd into '{remote_dir}'. FTP login starts at: {ftp.pwd()}")
            print("Contents of that starting directory:")
            try:
                ftp.retrlines("LIST")
            except Exception as list_err:
                print(f"(couldn't list directory: {list_err})")
            raise SystemExit(f"550 error: {e}")
    filename = os.path.basename(remote_path)
    with open(local_path, "rb") as f:
        ftp.storbinary(f"STOR {filename}", f)
    ftp.quit()


def list_ftp_dir(remote_dir: str, host: str, user: str, password: str, use_tls: bool = True):
    """Connect and print the contents of remote_dir — pure discovery helper, no file transfer."""
    ftp_cls = ftplib.FTP_TLS if use_tls else ftplib.FTP
    ftp = ftp_cls(timeout=30)
    ftp.connect(host, 21)
    ftp.login(user, password)
    if use_tls:
        ftp.prot_p()
    if remote_dir:
        ftp.cwd(remote_dir)
    print(f"Listing '{remote_dir or ftp.pwd()}':")
    ftp.retrlines("LIST")
    ftp.quit()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="prices.json", help="Output JSON path (default: prices.json)")
    parser.add_argument("--days", type=int, default=2, help="Number of days from today to fetch (default: 2 = today + tomorrow)")
    parser.add_argument("--token", default=None, help="ENTSO-E security token (else reads ENTSOE_API_TOKEN env var)")
    parser.add_argument("--upload", action="store_true", help="Upload the result to Hostinger via FTP afterward")
    parser.add_argument("--ftp-remote-path", default="prices.json", help="Remote path/filename on the FTP server (default: prices.json in the login's home dir)")
    parser.add_argument("--no-tls", action="store_true", help="Use plain FTP instead of FTPS (only if your host doesn't support FTPS)")
    parser.add_argument("--list-path", default=None, help="Instead of fetching/uploading, just list this remote FTP directory and exit (for path discovery)")
    args = parser.parse_args()

    if args.list_path is not None:
        host = os.environ.get("HOSTINGER_FTP_HOST")
        user = os.environ.get("HOSTINGER_FTP_USER")
        password = os.environ.get("HOSTINGER_FTP_PASSWORD")
        missing = [n for n, v in [("HOSTINGER_FTP_HOST", host), ("HOSTINGER_FTP_USER", user), ("HOSTINGER_FTP_PASSWORD", password)] if not v]
        if missing:
            sys.exit(f"--list-path requires these env vars to be set: {', '.join(missing)}")
        list_ftp_dir(args.list_path, host, user, password, use_tls=not args.no_tls)
        return

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

    if args.upload:
        host = os.environ.get("HOSTINGER_FTP_HOST")
        user = os.environ.get("HOSTINGER_FTP_USER")
        password = os.environ.get("HOSTINGER_FTP_PASSWORD")
        missing = [n for n, v in [("HOSTINGER_FTP_HOST", host), ("HOSTINGER_FTP_USER", user), ("HOSTINGER_FTP_PASSWORD", password)] if not v]
        if missing:
            sys.exit(f"--upload requires these env vars to be set: {', '.join(missing)}")
        upload_via_ftp(args.out, args.ftp_remote_path, host, user, password, use_tls=not args.no_tls)
        print(f"Uploaded {args.out} -> {args.ftp_remote_path} on {host}")


if __name__ == "__main__":
    main()
