"""
BD EEE Alert — scraper.py  (v2 — deep-scan edition)
====================================================
Improvements over v1:
  • Follows circular/PDF links found on notice boards
  • Extracts text from PDFs (pdfplumber → pdfminer fallback)
  • OCR for image-based circulars (pytesseract + pdf2image for scanned PDFs)
  • Playwright fallback for JS-rendered pages (optional; skipped gracefully)
  • Richer keyword matching with Bengali transliteration hints
  • Configurable depth so we don't spider the whole internet

Run:
    pip install requests beautifulsoup4 lxml pdfplumber pdf2image pytesseract pillow
    # For JS pages (optional):
    pip install playwright && playwright install chromium
    python scraper.py

Writes: data/jobs.json
"""

import requests
from bs4 import BeautifulSoup
import json, os, hashlib, re, io, time, tempfile
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
import logging

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

# ── Optional heavy deps (graceful fallback) ─────────────────────────────────
try:
    import pdfplumber
    HAS_PDFPLUMBER = True
except ImportError:
    HAS_PDFPLUMBER = False
    log.warning("  [WARN] pdfplumber not installed — PDF text extraction limited")

try:
    import pytesseract
    from PIL import Image
    HAS_TESSERACT = True
except ImportError:
    HAS_TESSERACT = False
    log.warning("  [WARN] pytesseract/Pillow not installed — image OCR disabled")

try:
    from pdf2image import convert_from_bytes
    HAS_PDF2IMAGE = True
except ImportError:
    HAS_PDF2IMAGE = False

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

# ── Keywords ─────────────────────────────────────────────────────────────────
EEE_KEYWORDS = [
    "eee", "electrical", "electronic", "lecturer", "assistant professor",
    "associate professor", "professor", "faculty", "vlsi", "pcb",
    "semiconductor", "embedded", "power system", "telecom", "ict",
    "engineer", "trainee officer", "technologist", "circuit",
    "microelectronics", "power electronics", "signal processing",
    "communication engineering", "control system", "instrumentation",
    "renewable energy", "solar", "substation", "transmission", "distribution",
    "bsc in eee", "b.sc in eee", "b.sc. in eee", "dept. of eee",
    "department of eee", "dept of electrical",
    # Bangla transliterations (common in mixed notices)
    "bidyut", "tড়িৎ",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}

TIMEOUT = 20
MAX_PDF_SIZE_MB = 10   # skip PDFs larger than this
MAX_CIRCULAR_DEPTH = 1  # how many levels deep to follow links from a notice page

# ── Helpers ───────────────────────────────────────────────────────────────────

def is_eee_relevant(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in EEE_KEYWORDS)

def make_id(org: str, title: str) -> str:
    return hashlib.md5((org + title).encode()).hexdigest()[:10]

def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def is_pdf_url(url: str) -> bool:
    return url.lower().split("?")[0].endswith(".pdf")

def is_image_url(url: str) -> bool:
    return any(url.lower().split("?")[0].endswith(ext)
               for ext in (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff"))

# ── Network fetchers ──────────────────────────────────────────────────────────

def fetch_html(url: str) -> BeautifulSoup | None:
    """Static HTTP fetch → BeautifulSoup. Falls back to Playwright if empty."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        # Heuristic: if body text is very short, the page is JS-rendered
        body_text = soup.get_text(strip=True)
        if len(body_text) < 200 and HAS_PLAYWRIGHT:
            return fetch_html_playwright(url)
        return soup
    except Exception as e:
        log.warning(f"  [WARN] fetch_html {url}: {e}")
        if HAS_PLAYWRIGHT:
            return fetch_html_playwright(url)
        return None

def fetch_html_playwright(url: str) -> BeautifulSoup | None:
    """JS-rendered page via headless Chromium."""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            page.goto(url, timeout=30000, wait_until="networkidle")
            html = page.content()
            browser.close()
        return BeautifulSoup(html, "lxml")
    except Exception as e:
        log.warning(f"  [WARN] playwright {url}: {e}")
        return None

def fetch_bytes(url: str, max_mb: float = MAX_PDF_SIZE_MB) -> bytes | None:
    """Download raw bytes (for PDFs/images). Skips files that are too large."""
    try:
        with requests.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True) as r:
            r.raise_for_status()
            content_len = int(r.headers.get("content-length", 0))
            if content_len > max_mb * 1024 * 1024:
                log.info(f"  [SKIP] {url} too large ({content_len // (1024*1024)} MB)")
                return None
            chunks = []
            total = 0
            for chunk in r.iter_content(65536):
                total += len(chunk)
                if total > max_mb * 1024 * 1024:
                    log.info(f"  [SKIP] {url} exceeded {max_mb} MB during download")
                    return None
                chunks.append(chunk)
            return b"".join(chunks)
    except Exception as e:
        log.warning(f"  [WARN] fetch_bytes {url}: {e}")
        return None

# ── Text extractors ───────────────────────────────────────────────────────────

def extract_text_from_pdf(data: bytes) -> str:
    """Try pdfplumber first; fall back to OCR via pdf2image+tesseract."""
    text = ""

    if HAS_PDFPLUMBER:
        try:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                for page in pdf.pages[:6]:  # scan first 6 pages max
                    t = page.extract_text()
                    if t:
                        text += t + "\n"
        except Exception as e:
            log.warning(f"  [WARN] pdfplumber: {e}")

    # If pdfplumber got nothing (scanned PDF), try OCR
    if len(text.strip()) < 50 and HAS_PDF2IMAGE and HAS_TESSERACT:
        try:
            images = convert_from_bytes(data, dpi=200, first_page=1, last_page=4)
            for img in images:
                text += pytesseract.image_to_string(img, lang="eng+ben") + "\n"
        except Exception as e:
            log.warning(f"  [WARN] pdf OCR: {e}")

    return text

def extract_text_from_image(data: bytes) -> str:
    """OCR an image file."""
    if not HAS_TESSERACT:
        return ""
    try:
        img = Image.open(io.BytesIO(data))
        return pytesseract.image_to_string(img, lang="eng+ben")
    except Exception as e:
        log.warning(f"  [WARN] image OCR: {e}")
        return ""

# ── Deep circular scanner ─────────────────────────────────────────────────────

def scan_circular_url(url: str, base_url: str) -> str:
    """
    Given a link found on a notice board, retrieve its content and return
    extracted text. Handles: HTML pages, PDFs, images.
    Returns empty string on failure.
    """
    if is_pdf_url(url):
        data = fetch_bytes(url)
        if data:
            return extract_text_from_pdf(data)
    elif is_image_url(url):
        data = fetch_bytes(url, max_mb=5)
        if data:
            return extract_text_from_image(data)
    else:
        # It's probably an HTML page or a redirect to a PDF
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True)
            ct = r.headers.get("content-type", "")
            if "pdf" in ct:
                data = b"".join(r.iter_content(65536))
                return extract_text_from_pdf(data)
            elif ct.startswith("image/"):
                data = b"".join(r.iter_content(65536))
                return extract_text_from_image(data)
            else:
                soup = BeautifulSoup(r.text, "lxml")
                return soup.get_text(" ", strip=True)
        except Exception as e:
            log.warning(f"  [WARN] scan_circular_url {url}: {e}")
    return ""

# ── Generic notice-board scraper with deep scan ───────────────────────────────

def scrape_generic_notice(org, url, base_url, category, tags, location, depth=1):
    """
    1. Fetch the notice board page.
    2. For each <a> link: check title text.
       If title is EEE-relevant → add immediately.
       Otherwise (depth>0) → follow link, extract content, check content.
    """
    jobs = []
    seen_ids = set()
    soup = fetch_html(url)
    if not soup:
        return jobs

    links = []
    for a in soup.find_all("a", href=True):
        title = a.get_text(strip=True)
        href = a["href"].strip()
        if not href or href.startswith("#") or href.startswith("javascript"):
            continue
        if not href.startswith("http"):
            href = urljoin(base_url, href)
        # Filter obvious non-notice links (nav, footer, social)
        if any(s in href for s in ["facebook", "twitter", "youtube", "linkedin",
                                    "mailto:", "tel:", "javascript"]):
            continue
        links.append((title, href))

    for title, href in links:
        jid = make_id(org, title if title else href)
        if jid in seen_ids:
            continue

        if title and len(title) >= 10 and is_eee_relevant(title):
            seen_ids.add(jid)
            jobs.append(_make_job(org, title, category, tags, location, href, url))
            continue

        # Deep scan: open the link itself
        if depth > 0:
            # Small delay to be polite
            time.sleep(0.3)
            content = scan_circular_url(href, base_url)
            if content and is_eee_relevant(content):
                display_title = title if len(title) >= 5 else _infer_title(content, org)
                jid2 = make_id(org, display_title)
                if jid2 not in seen_ids:
                    seen_ids.add(jid2)
                    jobs.append(_make_job(org, display_title, category, tags,
                                          location, href, url))

    log.info(f"  {org}: {len(jobs)} relevant notices")
    return jobs

def _make_job(org, title, category, tags, location, apply_url, source_url):
    return {
        "id": make_id(org, title),
        "title": title[:200],
        "org": org,
        "category": category,
        "tags": tags,
        "deadline": _extract_deadline(title),
        "posted": today_str(),
        "is_new": True,
        "location": location,
        "apply_url": apply_url,
        "source_url": source_url,
    }

def _extract_deadline(text: str) -> str:
    """Try to pull a date from the title/content."""
    patterns = [
        r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}",
        r"\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}",
        r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            return m.group(0)
    return "See circular"

def _infer_title(content: str, org: str) -> str:
    """Extract a short title from the first meaningful line of content."""
    for line in content.splitlines():
        line = line.strip()
        if 15 <= len(line) <= 200:
            return line
    return f"Recruitment Notice — {org}"

# ── Per-source scrapers ───────────────────────────────────────────────────────

def scrape_buet():
    jobs = []
    urls_to_try = [
        "https://www.buet.ac.bd/web/notice",
        "https://www.buet.ac.bd/web/#/notice",
        "https://www.buet.ac.bd/web/",
    ]
    for url in urls_to_try:
        soup = fetch_html(url)
        if soup and len(soup.get_text(strip=True)) > 200:
            for a in soup.find_all("a", href=True):
                t = a.get_text(strip=True)
                href = a["href"]
                if not href.startswith("http"):
                    href = urljoin("https://www.buet.ac.bd", href)
                if len(t) >= 10 and is_eee_relevant(t):
                    jobs.append(_make_job("BUET", t, "govt_uni",
                                          ["Faculty", "EEE", "Govt Uni"],
                                          "Dhaka", href, url))
                elif len(t) >= 10:
                    # deep scan
                    time.sleep(0.2)
                    content = scan_circular_url(href, "https://www.buet.ac.bd")
                    if content and is_eee_relevant(content):
                        title = _infer_title(content, "BUET") if len(t) < 5 else t
                        jobs.append(_make_job("BUET", title, "govt_uni",
                                              ["Faculty", "EEE", "Govt Uni"],
                                              "Dhaka", href, url))
            break

    # Deduplicate within BUET
    seen, out = set(), []
    for j in jobs:
        if j["id"] not in seen:
            seen.add(j["id"])
            out.append(j)
    log.info(f"  BUET: {len(out)} relevant notices")
    return out

def scrape_bdjobs():
    jobs = []
    queries = [
        "electrical+eee", "lecturer+eee", "vlsi+semiconductor",
        "power+system+engineer", "embedded+engineer",
    ]
    seen = set()
    for q in queries:
        url = f"https://jobs.bdjobs.com/jobsearch.asp?txtsearch={q}&fcat=2"
        soup = fetch_html(url)
        if not soup:
            continue
        # bdjobs structures change; try multiple selectors
        for sel in ["div.job-tittle a", ".JobTitle a", "h2.title a",
                    "a[href*='jobdetails']", "a[href*='job-detail']"]:
            for a in soup.select(sel):
                t = a.get_text(strip=True)
                if len(t) < 8 or t in seen:
                    continue
                seen.add(t)
                href = a.get("href", "#")
                if href and not href.startswith("http"):
                    href = "https://jobs.bdjobs.com" + href
                # For bdjobs, scan the detail page for EEE relevance
                relevant = is_eee_relevant(t)
                if not relevant:
                    content = scan_circular_url(href, "https://jobs.bdjobs.com")
                    relevant = content and is_eee_relevant(content)
                if relevant:
                    jobs.append(_make_job("bdjobs.com", t, "industry",
                                          ["EEE", "Industry"], "Bangladesh",
                                          href, url))
        time.sleep(0.5)
    # Also try the newer bdjobs search endpoint
    for q in ["EEE", "Electrical Engineer", "Lecturer EEE"]:
        url2 = f"https://jobs.bdjobs.com/jobsearch.asp?txtsearch={q.replace(' ', '+')}"
        soup2 = fetch_html(url2)
        if soup2:
            for a in soup2.find_all("a", href=True):
                href = a["href"]
                t = a.get_text(strip=True)
                if "jobdetails" not in href and "job-detail" not in href:
                    continue
                if len(t) < 6 or t in seen:
                    continue
                seen.add(t)
                if not href.startswith("http"):
                    href = "https://jobs.bdjobs.com" + href
                if is_eee_relevant(t):
                    jobs.append(_make_job("bdjobs.com", t, "industry",
                                          ["EEE", "Industry"], "Bangladesh",
                                          href, url2))
        time.sleep(0.5)
    log.info(f"  bdjobs.com: {len(jobs)} relevant listings")
    return jobs

def scrape_walton():
    return scrape_generic_notice(
        "Walton Hi-Tech Industries",
        "https://career.waltonbd.com/",
        "https://career.waltonbd.com",
        "semiconductor",
        ["Industry", "Electronics"],
        "Gazipur",
    )

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
    jobs = scrape_generic_notice(
        "North South University",
        "https://www.northsouth.edu/faculty-staff/job-opening.html",
        "https://www.northsouth.edu",
        "private_uni",
        ["Lecturer", "EEE", "Private Uni"],
        "Dhaka",
    )
    # NSU also posts on their EEE dept page
    jobs += scrape_generic_notice(
        "North South University",
        "https://www.northsouth.edu/academics/school-of-engineering-and-physical-sciences/electrical-and-computer-engineering/",
        "https://www.northsouth.edu",
        "private_uni",
        ["Lecturer", "EEE", "Private Uni"],
        "Dhaka",
    )
    return jobs

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
    jobs = scrape_generic_notice(
        "CUET",
        "https://www.cuet.ac.bd/notices",
        "https://www.cuet.ac.bd",
        "govt_uni",
        ["Faculty", "EEE", "Govt Uni"],
        "Chittagong",
    )
    jobs += scrape_generic_notice(
        "CUET",
        "https://www.cuet.ac.bd/job-circular",
        "https://www.cuet.ac.bd",
        "govt_uni",
        ["Faculty", "EEE", "Govt Uni"],
        "Chittagong",
    )
    return jobs

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

def scrape_aiub():
    return scrape_generic_notice(
        "AIUB",
        "https://www.aiub.edu/career",
        "https://www.aiub.edu",
        "private_uni",
        ["Lecturer", "EEE", "Private Uni"],
        "Dhaka",
    )

def scrape_iut():
    return scrape_generic_notice(
        "IUT",
        "https://www.iutoic-dhaka.edu/notices",
        "https://www.iutoic-dhaka.edu",
        "govt_uni",
        ["Faculty", "EEE", "Govt Uni"],
        "Gazipur",
    )

def scrape_du_eee():
    """University of Dhaka — EEE / Applied Physics dept."""
    return scrape_generic_notice(
        "University of Dhaka",
        "https://www.du.ac.bd/notice",
        "https://www.du.ac.bd",
        "govt_uni",
        ["Faculty", "EEE", "Govt Uni"],
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
    jobs = scrape_generic_notice(
        "DESCO",
        "https://www.desco.org.bd/careers",
        "https://www.desco.org.bd",
        "govt_engineer",
        ["Govt Engineer", "Electrical"],
        "Dhaka",
    )
    jobs += scrape_generic_notice(
        "DESCO",
        "https://www.desco.org.bd/notice",
        "https://www.desco.org.bd",
        "govt_engineer",
        ["Govt Engineer", "Electrical"],
        "Dhaka",
    )
    return jobs

def scrape_dpdc():
    return scrape_generic_notice(
        "DPDC",
        "https://www.dpdc.org.bd/home/career",
        "https://www.dpdc.org.bd",
        "govt_engineer",
        ["Govt Engineer", "Electrical"],
        "Dhaka",
    )

def scrape_pgcb():
    return scrape_generic_notice(
        "PGCB",
        "https://www.pgcb.gov.bd/site/notices",
        "https://www.pgcb.gov.bd",
        "govt_engineer",
        ["Govt Engineer", "Electrical"],
        "Dhaka",
    )

def scrape_bpdb():
    return scrape_generic_notice(
        "BPDB",
        "https://www.bpdb.gov.bd/bpdb/index.php/site/notice_list",
        "https://www.bpdb.gov.bd",
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

def scrape_sonali_bank():
    return scrape_generic_notice(
        "Sonali Bank",
        "https://www.sonalibank.com.bd/career.php",
        "https://www.sonalibank.com.bd",
        "bank",
        ["Bank", "Trainee"],
        "Dhaka",
    )

# ── Main ──────────────────────────────────────────────────────────────────────

def deduplicate(jobs):
    seen, out = set(), []
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
            return json.load(f).get("jobs", [])
    except Exception:
        return []

def run():
    os.makedirs("data", exist_ok=True)
    log.info("[SCRAPER] Starting BD EEE Alert v2 scraper...")

    existing = load_existing()
    existing_ids = {j["id"] for j in existing}

    scrapers = [
        scrape_buet,
        scrape_ruet,
        scrape_cuet,
        scrape_kuet,
        scrape_duet,
        scrape_sust,
        scrape_iut,
        scrape_du_eee,
        scrape_bracu,
        scrape_nsu,
        scrape_diu,
        scrape_aiub,
        scrape_bdjobs,
        scrape_walton,
        scrape_breb,
        scrape_desco,
        scrape_dpdc,
        scrape_pgcb,
        scrape_bpdb,
        scrape_bb,
        scrape_sonali_bank,
    ]

    fresh = []
    for fn in scrapers:
        try:
            fresh.extend(fn())
        except Exception as e:
            log.error(f"  [ERROR] {fn.__name__}: {e}")

    fresh = deduplicate(fresh)

    new_count = 0
    for j in fresh:
        if j["id"] not in existing_ids:
            j["is_new"] = True
            new_count += 1
        else:
            j["is_new"] = False

    fresh_ids = {j["id"] for j in fresh}
    merged = fresh + [j for j in existing if j["id"] not in fresh_ids]
    merged = merged[:300]

    out = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "new_this_run": new_count,
        "jobs": merged,
    }

    path = os.path.join("data", "jobs.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    log.info(f"\n[DONE] {len(fresh)} jobs scraped, {new_count} new → {path}")

if __name__ == "__main__":
    run()
