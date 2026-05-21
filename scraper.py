"""
BD EEE Alert — scraper.py
Run: python scraper.py
Writes: data/jobs.json
"""

import requests
from bs4 import BeautifulSoup
import json
import os
import hashlib
import re
from datetime import datetime, timezone

# ── Keywords that flag a notice as EEE-relevant ──
EEE_KEYWORDS = [
    "eee", "electrical", "electronic", "lecturer", "assistant professor",
    "associate professor", "professor", "faculty", "vlsi", "pcb", "semiconductor",
    "embedded", "power system", "telecom", "ict", "engineer", "trainee officer",
    "technologist", "circuit", "microelectronics",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}

TIMEOUT = 15

def is_eee_relevant(text):
    t = text.lower()
    return any(kw in t for kw in EEE_KEYWORDS)

def make_id(org, title):
    raw = (org + title).encode()
    return hashlib.md5(raw).hexdigest()[:10]

def today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def fetch(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        return BeautifulSoup(r.text, "lxml")
    except Exception as e:
        print(f"  [WARN] Could not fetch {url}: {e}")
        return None

# ── Per-source scrapers ─────────────────────────────────────────────────────

def scrape_buet():
    jobs = []
    soup = fetch("https://www.buet.ac.bd/web/#/notice")
    # BUET uses a JS-rendered SPA — static scraping won't get notices.
    # We use the public notice RSS/API endpoint instead.
    soup2 = fetch("https://www.buet.ac.bd/web/api/public/notice?page=1&limit=30")
    if soup2:
        # The API returns JSON-like text inside the html tag
        pass
    # Try the plain notice page
    soup3 = fetch("https://www.buet.ac.bd/web/notice")
    if soup3:
        for a in soup3.find_all("a", href=True):
            t = a.get_text(strip=True)
            if len(t) > 15 and is_eee_relevant(t):
                href = a["href"]
                if not href.startswith("http"):
                    href = "https://www.buet.ac.bd" + href
                jobs.append({
                    "id": make_id("BUET", t),
                    "title": t,
                    "org": "BUET",
                    "category": "govt_uni",
                    "tags": ["Faculty", "EEE", "Govt Uni"],
                    "deadline": "See circular",
                    "posted": today_str(),
                    "is_new": True,
                    "location": "Dhaka",
                    "apply_url": href,
                    "source_url": "https://www.buet.ac.bd/web/#/notice",
                })
    print(f"  BUET: {len(jobs)} relevant notices")
    return jobs

def scrape_generic_notice(org, url, base_url, category, tags, location):
    """Generic scraper for university notice boards."""
    jobs = []
    soup = fetch(url)
    if not soup:
        return jobs
    for a in soup.find_all("a", href=True):
        t = a.get_text(strip=True)
        if len(t) < 10:
            continue
        if is_eee_relevant(t):
            href = a["href"]
            if not href.startswith("http"):
                href = base_url.rstrip("/") + "/" + href.lstrip("/")
            jobs.append({
                "id": make_id(org, t),
                "title": t,
                "org": org,
                "category": category,
                "tags": tags,
                "deadline": "See circular",
                "posted": today_str(),
                "is_new": True,
                "location": location,
                "apply_url": href,
                "source_url": url,
            })
    print(f"  {org}: {len(jobs)} relevant notices")
    return jobs

def scrape_bdjobs():
    """Scrape bdjobs.com search results for EEE/electrical jobs."""
    jobs = []
    urls = [
        "https://jobs.bdjobs.com/jobsearch.asp?txtsearch=electrical+eee&fcat=2",
        "https://jobs.bdjobs.com/jobsearch.asp?txtsearch=lecturer+eee&fcat=2",
        "https://jobs.bdjobs.com/jobsearch.asp?txtsearch=vlsi+semiconductor&fcat=2",
    ]
    seen = set()
    for url in urls:
        soup = fetch(url)
        if not soup:
            continue
        for row in soup.select("div.job-tittle, .JobTitle, .job-title-text, h2.title"):
            t = row.get_text(strip=True)
            if len(t) < 10 or t in seen:
                continue
            seen.add(t)
            # Find parent link
            parent_a = row.find_parent("a") or row.find("a")
            href = "#"
            if parent_a and parent_a.get("href"):
                href = parent_a["href"]
                if not href.startswith("http"):
                    href = "https://jobs.bdjobs.com" + href
            jobs.append({
                "id": make_id("bdjobs", t),
                "title": t,
                "org": "bdjobs.com",
                "category": "industry",
                "tags": ["EEE", "Industry"],
                "deadline": "See circular",
                "posted": today_str(),
                "is_new": True,
                "location": "Bangladesh",
                "apply_url": href,
                "source_url": url,
            })
    print(f"  bdjobs.com: {len(jobs)} relevant listings")
    return jobs

def scrape_walton():
    jobs = []
    soup = fetch("https://career.waltonbd.com/")
    if not soup:
        return jobs
    for a in soup.find_all("a", href=True):
        t = a.get_text(strip=True)
        if len(t) > 10 and is_eee_relevant(t):
            href = a["href"]
            if not href.startswith("http"):
                href = "https://career.waltonbd.com" + href
            jobs.append({
                "id": make_id("Walton", t),
                "title": t,
                "org": "Walton Hi-Tech Industries",
                "category": "semiconductor",
                "tags": ["Industry", "Electronics"],
                "deadline": "See circular",
                "posted": today_str(),
                "is_new": True,
                "location": "Gazipur",
                "apply_url": href,
                "source_url": "https://career.waltonbd.com/",
            })
    print(f"  Walton: {len(jobs)} relevant listings")
    return jobs

def scrape_bracu():
    return scrape_generic_notice(
        "BRAC University",
        "https://www.bracu.ac.bd/about/offices/human-resource/job-openings",
        "https://www.bracu.ac.bd",
        "private_uni",
        ["Lecturer", "EEE", "Private Uni"],
        "Dhaka",
    )

def scrape_nsu():
    return scrape_generic_notice(
        "North South University",
        "https://www.northsouth.edu/faculty-staff/job-opening.html",
        "https://www.northsouth.edu",
        "private_uni",
        ["Lecturer", "EEE", "Private Uni"],
        "Dhaka",
    )

def scrape_ruet():
    return scrape_generic_notice(
        "RUET",
        "https://www.ruet.ac.bd/notices",
        "https://www.ruet.ac.bd",
        "govt_uni",
        ["Faculty", "EEE", "Govt Uni"],
        "Rajshahi",
    )

def scrape_cuet():
    return scrape_generic_notice(
        "CUET",
        "https://www.cuet.ac.bd/notices",
        "https://www.cuet.ac.bd",
        "govt_uni",
        ["Faculty", "EEE", "Govt Uni"],
        "Chittagong",
    )

def scrape_kuet():
    return scrape_generic_notice(
        "KUET",
        "https://www.kuet.ac.bd/index.php/notice-board",
        "https://www.kuet.ac.bd",
        "govt_uni",
        ["Faculty", "EEE", "Govt Uni"],
        "Khulna",
    )

def scrape_duet():
    return scrape_generic_notice(
        "DUET",
        "https://www.duet.ac.bd/notice",
        "https://www.duet.ac.bd",
        "govt_uni",
        ["Faculty", "EEE", "Govt Uni"],
        "Gazipur",
    )

def scrape_sust():
    return scrape_generic_notice(
        "SUST",
        "https://www.sust.edu/notices",
        "https://www.sust.edu",
        "govt_uni",
        ["Faculty", "EEE", "Govt Uni"],
        "Sylhet",
    )

def scrape_diu():
    return scrape_generic_notice(
        "Daffodil International University",
        "https://daffodilvarsity.edu.bd/career",
        "https://daffodilvarsity.edu.bd",
        "private_uni",
        ["Lecturer", "EEE", "Private Uni"],
        "Dhaka",
    )

def scrape_breb():
    return scrape_generic_notice(
        "BREB",
        "https://www.breb.gov.bd/site/notices",
        "https://www.breb.gov.bd",
        "govt_engineer",
        ["Govt Engineer", "Electrical"],
        "Bangladesh",
    )

def scrape_desco():
    return scrape_generic_notice(
        "DESCO",
        "https://www.desco.org.bd/careers",
        "https://www.desco.org.bd",
        "govt_engineer",
        ["Govt Engineer", "Electrical"],
        "Dhaka",
    )

def scrape_dpdc():
    return scrape_generic_notice(
        "DPDC",
        "https://www.dpdc.org.bd/home/career",
        "https://www.dpdc.org.bd",
        "govt_engineer",
        ["Govt Engineer", "Electrical"],
        "Dhaka",
    )

def scrape_bb():
    return scrape_generic_notice(
        "Bangladesh Bank",
        "https://www.bb.org.bd/aboutus/career.php",
        "https://www.bb.org.bd",
        "bank",
        ["Bank", "Trainee"],
        "Dhaka",
    )

# ── Main ────────────────────────────────────────────────────────────────────

def deduplicate(jobs):
    seen = set()
    out = []
    for j in jobs:
        if j["id"] not in seen:
            seen.add(j["id"])
            out.append(j)
    return out

def load_existing():
    path = os.path.join("data", "jobs.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            d = json.load(f)
            return d.get("jobs", [])
    except Exception:
        return []

def run():
    os.makedirs("data", exist_ok=True)
    print("[SCRAPER] Starting BD EEE Alert scraper...")

    existing = load_existing()
    existing_ids = {j["id"] for j in existing}

    scrapers = [
        scrape_buet,
        scrape_ruet,
        scrape_cuet,
        scrape_kuet,
        scrape_duet,
        scrape_sust,
        scrape_bracu,
        scrape_nsu,
        scrape_diu,
        scrape_bdjobs,
        scrape_walton,
        scrape_breb,
        scrape_desco,
        scrape_dpdc,
        scrape_bb,
    ]

    fresh = []
    for fn in scrapers:
        try:
            fresh.extend(fn())
        except Exception as e:
            print(f"  [ERROR] {fn.__name__}: {e}")

    fresh = deduplicate(fresh)

    # Mark truly new vs already known
    new_count = 0
    for j in fresh:
        if j["id"] not in existing_ids:
            j["is_new"] = True
            new_count += 1
        else:
            j["is_new"] = False

    # Merge: new first, then existing that aren't in fresh
    fresh_ids = {j["id"] for j in fresh}
    merged = fresh + [j for j in existing if j["id"] not in fresh_ids]

    # Keep at most 200 jobs
    merged = merged[:200]

    out = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "new_this_run": new_count,
        "jobs": merged,
    }

    path = os.path.join("data", "jobs.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"\n[DONE] {len(fresh)} jobs scraped, {new_count} new. Written to {path}")

if __name__ == "__main__":
    run()
