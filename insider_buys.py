import requests
import xml.etree.ElementTree as ET
import os
from datetime import date, timedelta

HEADERS = {"User-Agent": "StockInc thomasga1@comcast.net"}
SLACK_WEBHOOK = os.environ["SLACK_WEBHOOK_URL"]

def get_recent_form4s():
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    feed = requests.get(
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4"
        "&dateb=&owner=include&count=40&search_text=&output=atom",
        headers=HEADERS, timeout=15
    )
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(feed.text)
    return root.findall("a:entry", ns), ns, yesterday

def get_filing_xml(idx_htm_url):
    idx_json_url = idx_htm_url.replace("-index.htm", "-index.json")
    jr = requests.get(idx_json_url, headers=HEADERS, timeout=10)
    if jr.status_code != 200:
        return None
    primary = jr.json().get("primary_document", "")
    if not primary or not primary.endswith(".xml"):
        return None
    base = "/".join(idx_htm_url.split("/")[:-1])
    xr = requests.get(f"{base}/{primary}", headers=HEADERS, timeout=10)
    return xr.text if xr.status_code == 200 else None

def parse_purchases(xml_text):
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return []
    company = root.findtext(".//issuerName") or "?"
    ticker  = root.findtext(".//issuerTradingSymbol") or "?"
    insider = root.findtext(".//rptOwnerName") or "?"
    title   = root.findtext(".//officerTitle") or root.findtext(".//rptOwnerRelationship") or "?"
    results = []
    for txn in root.findall(".//nonDerivativeTransaction"):
        if (txn.findtext(".//transactionCode") or "") != "P":
            continue
        shares = float(txn.findtext(".//transactionShares/value") or 0)
        price  = float(txn.findtext(".//transactionPricePerShare/value") or 0)
        total  = shares * price
        after  = float(txn.findtext(".//sharesOwnedFollowingTransaction/value") or 0)
        if total < 100_000:
            continue
        position = "New position" if round(after) == round(shares) else f"Added (now holds {after:,.0f} shares)"
        results.append({"company": company, "ticker": ticker, "insider": insider,
                        "title": title, "shares": shares, "price": price,
                        "total": total, "position": position})
    return results

def post_to_slack(message):
    requests.post(SLACK_WEBHOOK, json={"text": message}, timeout=10)

# --- Main ---
entries, ns, yesterday = get_recent_form4s()
all_txns = []

for entry in entries:
    if (entry.findtext("a:updated", "", ns) or "")[:10] < yesterday:
        continue
    link = entry.find("a:link", ns)
    if link is None:
        continue
    xml_text = get_filing_xml(link.get("href", ""))
    if xml_text:
        all_txns.extend(parse_purchases(xml_text))

ranked = sorted(all_txns, key=lambda x: x["total"], reverse=True)

if not ranked:
    post_to_slack(f"*SEC Form 4 — {date.today()}*\nNo insider purchases over $100k in the last 24 hours.")
else:
    lines = [f"*:chart_with_upwards_trend: SEC Form 4 Insider Purchases >$100k — {date.today()}*\n"]
    for i, t in enumerate(ranked, 1):
        lines.append(
            f"*#{i} {t['company']} ({t['ticker']})*\n"
            f"  Insider: {t['insider']} — {t['title']}\n"
            f"  Bought: {t['shares']:,.0f} shares @ ${t['price']:.2f} = *${t['total']:,.0f}*\n"
            f"  Position: {t['position']}"
        )
    post_to_slack("\n\n".join(lines))
    print(f"Done — posted {len(ranked)} purchases to Slack.")
