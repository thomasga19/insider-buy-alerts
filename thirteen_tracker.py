import requests
import xml.etree.ElementTree as ET
import os
import time
from datetime import date, timedelta

HEADERS = {"User-Agent": "StockInc thomasga1@comcast.net"}
SLACK_WEBHOOK = os.environ["SLACK_WEBHOOK_URL"]
AUM_THRESHOLD = 1_000_000  # $1B in thousands (EDGAR reports values in thousands)

def get_recent_filings(days=7):
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    urls = []
    for start in [0, 40, 80]:
        r = requests.get(
            f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=13F-HR"
            f"&dateb=&owner=include&count=40&start={start}&search_text=&output=atom",
            headers=HEADERS, timeout=15
        )
        if r.status_code != 200:
            break
        ns = {"a": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(r.text)
        entries = root.findall("a:entry", ns)
        if not entries:
            break
        for entry in entries:
            if (entry.findtext("a:updated", "", ns) or "")[:10] < cutoff:
                continue
            link = entry.find("a:link", ns)
            if link is not None:
                urls.append(link.get("href", ""))
        time.sleep(0.3)
    return urls

def get_index(idx_htm_url):
    jr = requests.get(idx_htm_url.replace("-index.htm", "-index.json"), headers=HEADERS, timeout=10)
    if jr.status_code != 200:
        return None
    return jr.json()

def find_xml_docs(idx_data, base_url):
    primary, infotable = None, None
    primary_name = idx_data.get("primary_document", "")
    for doc in idx_data.get("documents", []):
        name = doc.get("document", "")
        desc = doc.get("description", "").lower()
        dtype = doc.get("type", "")
        if not name.endswith(".xml"):
            continue
        if name == primary_name or dtype == "13F-HR":
            primary = f"{base_url}/{name}"
        elif "info" in desc or "table" in desc or (name != primary_name and primary):
            if not infotable:
                infotable = f"{base_url}/{name}"
    return primary, infotable

def parse_cover(xml_url):
    r = requests.get(xml_url, headers=HEADERS, timeout=10)
    if r.status_code != 200:
        return None, 0
    try:
        root = ET.fromstring(r.text)
    except Exception:
        return None, 0
    name = (root.findtext(".//filingManager/name") or
            root.findtext(".//name") or "Unknown")
    try:
        total = int((root.findtext(".//tableValueTotal") or "0").replace(",", ""))
    except Exception:
        total = 0
    return name.strip(), total

def parse_holdings(xml_url):
    r = requests.get(xml_url, headers=HEADERS, timeout=10)
    if r.status_code != 200:
        return {}
    try:
        root = ET.fromstring(r.text)
    except Exception:
        return {}
    ns = root.tag[1:root.tag.index("}")] if root.tag.startswith("{") else ""
    pre = f"{{{ns}}}" if ns else ""
    holdings = {}
    for row in root.findall(f".//{pre}infoTable"):
        def f(tag):
            return (row.findtext(f"{pre}{tag}") or row.findtext(tag) or "").strip()
        cusip = f("cusip")
        name  = f("nameOfIssuer")
        try:
            value = int(f("value").replace(",", ""))
        except Exception:
            value = 0
        shares_el = row.find(f".//{pre}sshPrnamt") or row.find(".//sshPrnamt")
        try:
            shares = int(shares_el.text.replace(",", "")) if shares_el is not None else 0
        except Exception:
            shares = 0
        if cusip:
            holdings[cusip] = {"name": name, "value": value, "shares": shares}
    return holdings

def get_previous_holdings(cik):
    r = requests.get(f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json",
                     headers=HEADERS, timeout=10)
    if r.status_code != 200:
        return {}
    data = r.json()
    recent = data.get("filings", {}).get("recent", {})
    forms      = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    prev_13fs  = [acc for form, acc in zip(forms, accessions) if form == "13F-HR"]
    if len(prev_13fs) < 2:
        return {}
    acc = prev_13fs[1]
    acc_clean = acc.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_clean}"
    jr = requests.get(f"{base}/{acc}-index.json", headers=HEADERS, timeout=10)
    if jr.status_code != 200:
        return {}
    _, infotable = find_xml_docs(jr.json(), base)
    return parse_holdings(infotable) if infotable else {}

def compare(current, previous):
    new, increased, exited = [], [], []
    for cusip, d in current.items():
        if cusip not in previous:
            new.append(d | {"cusip": cusip})
        elif current[cusip]["value"] > previous[cusip]["value"] > 0:
            pct = (current[cusip]["value"] - previous[cusip]["value"]) / previous[cusip]["value"] * 100
            increased.append(d | {"cusip": cusip, "pct": pct, "prev": previous[cusip]["value"]})
    for cusip, d in previous.items():
        if cusip not in current:
            exited.append(d)
    new.sort(key=lambda x: x["value"], reverse=True)
    increased.sort(key=lambda x: x["pct"], reverse=True)
    return new[:5], increased[:5], exited

def format_block(fund_name, new, increased, exited, total_new):
    lines = [f"*{fund_name}*  |  ${total_new / 1_000:.1f}M in new positions"]
    if new:
        lines.append("  *New positions (top 5):*")
        for p in new:
            lines.append(f"    • {p['name']} — ${p['value'] / 1_000:.1f}M")
    if increased:
        lines.append("  *Most increased (top 5):*")
        for p in increased:
            lines.append(f"    • {p['name']} +{p['pct']:.0f}%  (${p['prev']/1_000:.1f}M → ${p['value']/1_000:.1f}M)")
    if exited:
        names = ", ".join(p["name"] for p in exited[:5])
        more  = f" +{len(exited)-5} more" if len(exited) > 5 else ""
        lines.append(f"  *Exited:* {names}{more}")
    return "\n".join(lines)

def post_to_slack(msg):
    requests.post(SLACK_WEBHOOK, json={"text": msg}, timeout=10)

# --- Main ---
print("Fetching recent 13F-HR filings...")
filing_urls = get_recent_filings(days=7)
print(f"Found {len(filing_urls)} filings in the last 7 days")

results = []
for url in filing_urls:
    try:
        idx = get_index(url)
        if not idx:
            continue
        base = "/".join(url.split("/")[:-1])
        cik  = url.split("/")[-3].lstrip("0")
        primary, infotable = find_xml_docs(idx, base)
        if not primary:
            continue
        fund_name, total_value = parse_cover(primary)
        if total_value < AUM_THRESHOLD:
            print(f"Skip {fund_name} — ${total_value/1_000:.0f}M AUM")
            continue
        print(f"Processing {fund_name} (${total_value/1_000:.0f}M)...")
        if not infotable:
            continue
        current  = parse_holdings(infotable)
        previous = get_previous_holdings(cik)
        new, increased, exited = compare(current, previous)
        total_new = sum(p["value"] for p in new)
        results.append({"name": fund_name, "total_new": total_new,
                        "new": new, "increased": increased, "exited": exited})
        time.sleep(0.3)
    except Exception as e:
        print(f"Error on {url}: {e}")

results.sort(key=lambda x: x["total_new"], reverse=True)

if not results:
    post_to_slack(f"*13F-HR Monitor — {date.today()}*\nNo qualifying funds (>$1B AUM) filed in the last 7 days.")
else:
    blocks = [f"*:bank: 13F Institutional Holdings — {date.today()}*\n_{len(results)} funds >$1B AUM filed this week_\n"]
    for r in results[:10]:
        blocks.append(format_block(r["name"], r["new"], r["increased"], r["exited"], r["total_new"]))
    post_to_slack("\n\n---\n\n".join(blocks))
    print(f"Posted {len(results)} funds to Slack.")
