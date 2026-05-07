import requests
import xml.etree.ElementTree as ET
import os
import time
from datetime import date, timedelta

HEADERS = {"User-Agent": "StockInc thomasga1@comcast.net"}
SLACK_WEBHOOK = os.environ["SLACK_WEBHOOK_URL"]
AUM_THRESHOLD = 1_000_000

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
      
