"""
BD EEE Alert — scraper.py  (v3 — stable, no Playwright)
=========================================================
- Removed Playwright (caused hanging timeouts)
- Hard timeout on every request (10s)
- PDF text extraction + OCR for scanned PDFs/images
- Skips unresponsive sources gracefully
- Per-scraper time limit so one bad source can't block everything
"""

import requests
from bs4 import BeautifulSoup
import json, os, hashlib, re, io, time, signal
from datetime import datetime, timezone
from urllib.parse import urljoin
import logging

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

# ── Optional deps (graceful fallback) ────────────────────────────────────────
try:
    import pdfplumber
    HAS_PDFPLUMBER = True
except ImportError:
    HAS_PDFPLUMBER = False
    log.warning("[WARN] pdfplumber not installed")

try:
    import pytesseract
    from PIL import Image
    HAS_TESSERACT = True
except ImportError:
    HAS_TESSERACT = False
    log.warning("[WARN] pytesseract/Pillow not installed — image OCR disabled")

try:
    from pdf2image import convert_from_bytes
    HAS_PDF2IMAGE = True
except ImportError:
    HAS_PDF2IMAGE = False

# ── Config ────────────────────────────────────────────────────────────────────
TIMEOUT        = 10          # seconds per HTTP request
MAX_PDF_MB     = 8           # skip PDFs larger than this
MAX_LINKS_PER_PAGE = 60      # don't follow more than this many links per source
SCRAPER_TIMEOUT = 45         # seconds budget per source scraper

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}

# ── Keywords ──────────────────────────────────────────────────────────────────
EEE_KEYWORDS = [
    "eee", "electrical", "electronic", "lecturer", "assistant professor",
    "associate professor", "professor", "faculty", "vlsi", "pcb",
    "semiconductor", "embedded", "power system", "telecom", "ict",
    "engineer", "trainee officer", "technologist", "circuit",
    "microelectronics", "power electronics", "signal processing",
    "communication engineering", "control system", "instrumentation",
    "renewable energy", "solar", "substation", "transmission", "distribution",
    "bsc in eee", "b.sc in eee", "dept. of eee", "department of eee",
    "dept of electrical", "electrical engineer", "eee department",
]

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
    return any(url.lower().split("?")[0].endswith(e)
               for e in (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff"))

# ── Timeout decorator ─────────────────────────────────────────────────────────
class TimeoutError(Exception): pass

def with_timeout(seconds):
    """Decorator: raises TimeoutError if function takes longer than `seconds`."""
    def decorator(fn):
        def wrapper(*args, **kwargs):
            def handler(signum, frame): raise TimeoutError(f"{fn.__name__} timed out")
            old = signal.signal(signal.SIGALRM, handler)
            signal.alarm(seconds)
            try:
                return fn(*args, **kwargs)
            finally:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, old)
        return wrapper
    return decorator

# ── Network ───────────────────────────────────────────────────────────────────
def fetch_html(url: str) -> BeautifulSoup | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        return BeautifulSoup(r.text, "lxml")
    except Exception as e:
        log.warning(f"  [WARN] fetch_html {url}: {e}")
        return None

def fetch_bytes(url: str, max_mb: float = MAX_PDF_MB) -> bytes | None:
    try:
        with requests.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True) as r:
            r.raise_for_status()
            chunks, total = [], 0
            for chunk in r.iter_content(65536):
                total += len(chunk)
                if total > max_mb * 1024 * 1024:
                    log.info(f"  [SKIP] {url} too large")
                    return None
                chunks.append(chunk)
            return b"".join(chunks)
    except Exception as e:
        log.warning(f"  [WARN] fetch_bytes {url}: {e}")
        return None

# ── Content extractors ────────────────────────────────────────────────────────
def extract_pdf_text(data: bytes) -> str:
    text = ""
    if HAS_PDFPLUMBER:
        try:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                for page in pdf.pages[:5]:
                    t = page.extract_text()
                    if t: text += t + "\n"
        except Exception as e:
            log.warning(f"  [WARN] pdfplumber: {e}")

    # Fallback: OCR scanned PDF
    if len(text.strip()) < 50 and HAS_PDF2IMAGE and HAS_TESSERACT:
        try:
            images = convert_from_bytes(data, dpi=150, first_page=1, last_page=3)
            for img in images:
                text += pytesseract.image_to_string(img, lang="eng+ben") + "\n"
        except Exception as e:
            log.warning(f"  [WARN] pdf OCR: {e}")
    return text

def extract_image_text(data: bytes) -> str:
    if not HAS_TESSERACT: return ""
    try:
        img = Image.open(io.BytesIO(data))
        return pytesseract.image_to_string(img, lang="eng+ben")
    except Exception as e:
        log.warning(f"  [WARN] image OCR: {e}")
        return ""

def scan_url(url: str) -> str:
    """Fetch a circular URL and return its text content."""
    if is_pdf_url(url):
        data = fetch_bytes(url)
        return extract_pdf_text(data) if data else ""
    elif is_image_url(url):
        data = fetch_bytes(url, max_mb=4)
        return extract_image_text(data) if data else ""
    else:
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True)
            ct = r.headers.get("content-type", "")
            if "pdf" in ct:
                data = b"".join(r.iter_content(65536))
                return extract_pdf_text(data)
            elif ct.startswith("image/"):
                data = b"".join(r.iter_content(65536))
                return extract_image_text(data)
            else:
                soup = BeautifulSoup(r.text, "lxml")
                return soup.get_text(" ", strip=True)[:3000]
        except Exception as e:
            log.warning(f"  [WARN] scan_url {url}: {e}")
    return ""

# ── Job builder ───────────────────────────────────────────────────────────────
def make_job(org, title, category, tags, location, apply_url, source_url):
    return {
        "id": make_id(org, title),
        "title": title[:200],
        "org": org,
        "category": category,
        "tags": tags,
        "deadline": extract_deadline(title),
        "posted": today_str(),
        "is_new": True,
        "location": location,
        "apply_url": apply_url,
        "source_url": source_url,
    }

def extract_deadline(text: str) -> str:
    for pat in [
        r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}",
        r"\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}",
        r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}",
    ]:
        m = re.search(pat, text, re.I)
        if m: return m.group(0)
    return "See circular"

def infer_title(content: str, org: str) -> str:
    for line in content.splitlines():
        line = line.strip()
        if 15 <= len(line) <= 200:
            return line
    return f"Recruitment Notice — {org}"

# ── Generic scraper ───────────────────────────────────────────────────────────
def scrape_generic(org, url, base_url, category, tags, location):
    jobs, seen = [], set()
    soup = fetch_html(url)
    if not soup:
        log.info(f"  {org}: 0 (unreachable)")
        return jobs

    links = []
    for a in soup.find_all("a", href=True):
        title = a.get_text(strip=True)
        href  = a["href"].strip()
        if not href or href.startswith("#") or "javascript" in href: continue
        if any(s in href for s in ["facebook","twitter","youtube","linkedin","mailto:","tel:"]): continue
        if not href.startswith("http"):
            href = urljoin(base_url, href)
        links.append((title, href))

    links = links[:MAX_LINKS_PER_PAGE]

    for title, href in links:
        jid = make_id(org, title or href)
        if jid in seen: continue

        # Fast path: title already mentions EEE
        if title and len(title) >= 10 and is_eee_relevant(title):
            seen.add(jid)
            jobs.append(make_job(org, title, category, tags, location, href, url))
            continue

        # Deep scan: open circular / PDF / image
        time.sleep(0.2)
        content = scan_url(href)
        if content and is_eee_relevant(content):
            display = title if len(title) >= 8 else infer_title(content, org)
            jid2 = make_id(org, display)
            if jid2 not in seen:
                seen.add(jid2)
                jobs.append(make_job(org, display, category, tags, location, href, url))

    log.info(f"  {org}: {len(jobs)} relevant notices")
    return jobs

# ── Individual scrapers (each wrapped with 45-sec timeout) ────────────────────

def _make_scraper(org, url, base_url, category, tags, location):
    @with_timeout(SCRAPER_TIMEOUT)
    def scraper():
        return scrape_generic(org, url, base_url, category, tags, location)
    scraper.__name__ = org
    return scraper

SOURCES = [
    # (org, notice_url, base_url, category, tags, location)
    ("BUET",   "https://www.buet.ac.bd/web/notice",            "https://www.buet.ac.bd",         "govt_uni",      ["Faculty","EEE","Govt Uni"],     "Dhaka"),
    ("RUET",   "https://www.ruet.ac.bd/notices",               "https://www.ruet.ac.bd",          "govt_uni",      ["Faculty","EEE","Govt Uni"],     "Rajshahi"),
    ("CUET",   "https://www.cuet.ac.bd/notices",               "https://www.cuet.ac.bd",          "govt_uni",      ["Faculty","EEE","Govt Uni"],     "Chittagong"),
    ("KUET",   "https://www.kuet.ac.bd/index.php/notice-board","https://www.kuet.ac.bd",          "govt_uni",      ["Faculty","EEE","Govt Uni"],     "Khulna"),
    ("DUET",   "https://www.duet.ac.bd/notice",                "https://www.duet.ac.bd",          "govt_uni",      ["Faculty","EEE","Govt Uni"],     "Gazipur"),
    ("SUST",   "https://www.sust.edu/notices",                 "https://www.sust.edu",            "govt_uni",      ["Faculty","EEE","Govt Uni"],     "Sylhet"),
    ("IUT",    "https://www.iutoic-dhaka.edu/notices",         "https://www.iutoic-dhaka.edu",    "govt_uni",      ["Faculty","EEE","Govt Uni"],     "Gazipur"),
    ("BRAC University", "https://www.bracu.ac.bd/about/offices/human-resource/job-openings", "https://www.bracu.ac.bd", "private_uni", ["Lecturer","EEE","Private Uni"], "Dhaka"),
    ("North South University", "https://www.northsouth.edu/faculty-staff/job-opening.html", "https://www.northsouth.edu", "private_uni", ["Lecturer","EEE","Private Uni"], "Dhaka"),
    ("Daffodil International University", "https://daffodilvarsity.edu.bd/career", "https://daffodilvarsity.edu.bd", "private_uni", ["Lecturer","EEE","Private Uni"], "Dhaka"),
    ("AIUB",   "https://www.aiub.edu/career",                  "https://www.aiub.edu",            "private_uni",   ["Lecturer","EEE","Private Uni"], "Dhaka"),
    ("East West University", "https://www.ewubd.edu/job-circular", "https://www.ewubd.edu",       "private_uni",   ["Lecturer","EEE","Private Uni"], "Dhaka"),
    ("UAP",    "https://uap-bd.edu/career/",                   "https://uap-bd.edu",              "private_uni",   ["Lecturer","EEE","Private Uni"], "Dhaka"),
    ("Walton Hi-Tech", "https://career.waltonbd.com/",         "https://career.waltonbd.com",     "semiconductor", ["Industry","Electronics"],       "Gazipur"),
    ("Energypac", "https://energypacbd.com/career/",           "https://energypacbd.com",         "industry",      ["Industry","Electrical"],        "Dhaka"),
    ("Rahimafrooz", "https://www.rahimafrooz.com/career",      "https://www.rahimafrooz.com",     "industry",      ["Industry","Electronics"],       "Dhaka"),
    ("BREB",   "https://www.breb.gov.bd/site/notices",         "https://www.breb.gov.bd",         "govt_engineer", ["Govt Engineer","Electrical"],   "Bangladesh"),
    ("DESCO",  "https://www.desco.org.bd/careers",             "https://www.desco.org.bd",        "govt_engineer", ["Govt Engineer","Electrical"],   "Dhaka"),
    ("DPDC",   "https://www.dpdc.org.bd/home/career",          "https://www.dpdc.org.bd",         "govt_engineer", ["Govt Engineer","Electrical"],   "Dhaka"),
    ("PGCB",   "https://www.pgcb.gov.bd/site/notices",         "https://www.pgcb.gov.bd",         "govt_engineer", ["Govt Engineer","Electrical"],   "Dhaka"),
    ("BPDB",   "https://www.bpdb.gov.bd/bpdb/index.php/site/notice_list", "https://www.bpdb.gov.bd", "govt_engineer", ["Govt Engineer","Electrical"], "Dhaka"),
    ("Bangladesh Bank", "https://www.bb.org.bd/aboutus/career.php", "https://www.bb.org.bd",     "bank",          ["Bank","Trainee"],               "Dhaka"),
    ("Sonali Bank", "https://www.sonalibank.com.bd/career.php","https://www.sonalibank.com.bd",   "bank",          ["Bank","Trainee"],               "Dhaka"),
]

def scrape_bdjobs():
    """bdjobs.com — search multiple EEE queries."""
    jobs, seen = [], set()
    queries = ["electrical+eee", "lecturer+eee", "vlsi+semiconductor",
               "power+system+engineer", "embedded+engineer", "EEE"]
    for q in queries:
        url = f"https://jobs.bdjobs.com/jobsearch.asp?txtsearch={q}"
        soup = fetch_html(url)
        if not soup: continue
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "jobdetails" not in href and "job-detail" not in href: continue
            t = a.get_text(strip=True)
            if len(t) < 6 or t in seen: continue
            seen.add(t)
            if not href.startswith("http"):
                href = "https://jobs.bdjobs.com" + href
            if is_eee_relevant(t):
                jobs.append(make_job("bdjobs.com", t, "industry",
                                     ["EEE","Industry"], "Bangladesh", href, url))
        time.sleep(0.5)
    log.info(f"  bdjobs.com: {len(jobs)} relevant listings")
    return jobs

# ── Main ──────────────────────────────────────────────────────────────────────
def dedupe(jobs):
    seen, out = set(), []
    for j in jobs:
        if j["id"] not in seen:
            seen.add(j["id"])
            out.append(j)
    return out

def load_existing():
    path = os.path.join("data", "jobs.json")
    if not os.path.exists(path): return []
    try:
        with open(path) as f: return json.load(f).get("jobs", [])
    except Exception: return []

def run():
    os.makedirs("data", exist_ok=True)
    log.info("[SCRAPER] BD EEE Alert v3 starting...")

    existing    = load_existing()
    existing_ids = {j["id"] for j in existing}
    fresh       = []

    for (org, url, base_url, cat, tags, loc) in SOURCES:
        fn = _make_scraper(org, url, base_url, cat, tags, loc)
        try:
            fresh.extend(fn())
        except TimeoutError:
            log.warning(f"  [TIMEOUT] {org} skipped after {SCRAPER_TIMEOUT}s")
        except Exception as e:
            log.error(f"  [ERROR] {org}: {e}")

    # bdjobs with its own timeout
    try:
        bdjobs_fn = with_timeout(SCRAPER_TIMEOUT)(scrape_bdjobs)
        fresh.extend(bdjobs_fn())
    except TimeoutError:
        log.warning("  [TIMEOUT] bdjobs.com skipped")
    except Exception as e:
        log.error(f"  [ERROR] bdjobs.com: {e}")

    fresh = dedupe(fresh)

    new_count = 0
    for j in fresh:
        if j["id"] not in existing_ids:
            j["is_new"] = True
            new_count += 1
        else:
            j["is_new"] = False

    fresh_ids = {j["id"] for j in fresh}
    merged    = fresh + [j for j in existing if j["id"] not in fresh_ids]
    merged    = merged[:300]

    out = {
        "last_updated":  datetime.now(timezone.utc).isoformat(),
        "new_this_run":  new_count,
        "jobs":          merged,
    }

    path = os.path.join("data", "jobs.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    log.info(f"\n[DONE] {len(fresh)} scraped, {new_count} new → {path}")

if __name__ == "__main__":
    run()
