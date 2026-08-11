
import asyncio
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "docs", "data")
COMP = os.path.join(DATA, "companies.json")
OUT = os.path.join(DATA, "jobs.json")

TIMEOUT = 30000
MAX_JOBS_PER_COMPANY = 80
MAX_STORED_JOBS = 600

# This is intentionally strict. A job is not shown just because its employer is a pharma company.
# It must look like a QC/QA/GMP/laboratory/quality role in the title or description.
TARGET_TITLE_PATTERNS = [
    r"\bquality control\b", r"\bquality assurance\b", r"\bquality specialist\b",
    r"\bquality associate\b", r"\bquality analyst\b", r"\bquality officer\b",
    r"\bquality manager\b", r"\bquality lead\b", r"\bquality operations\b",
    r"\bquality systems?\b", r"\bquality compliance\b", r"\bquality engineer\b",
    r"\bqc\b", r"\bqa\b", r"\bgmp\b", r"\bgxp\b",
    r"\blaboratory\b", r"\blab analyst\b", r"\blab technician\b",
    r"\bmicrobiology\b", r"\bmicrobiologist\b", r"\banalytical\b",
    r"\bvalidation\b", r"\bqualification\b", r"\bsterility\b",
    r"\bsterile\b", r"\baseptic\b", r"\bin[- ]process control\b",
    r"\bbatch release\b", r"\bdeviation\b", r"\bcapa\b", r"\boos\b",
    r"\bdata integrity\b",
]
TARGET_DESC_TERMS = [
    "quality control", "quality assurance", "gmp", "gxp", "quality system",
    "quality management", "laboratory", "microbiology", "analytical",
    "validation", "qualification", "sterility", "aseptic", "sterile",
    "batch release", "deviation", "capa", "oos", "data integrity",
    "in-process control", "manufacturing quality", "pharmaceutical quality",
]
HARD_EXCLUDE_TITLE = [
    "sales", "marketing", "finance", "accounting", "human resources", "recruiter",
    "recruitment", "legal", "procurement", "purchasing", "software", "developer",
    "data scientist", "information technology", "it support", "cybersecurity",
    "commercial", "business development", "communications", "medical sales",
    "clinical sales", "field sales", "customer service", "supply chain planner",
]
LOCATION_HINTS = [
    "germany", "deutschland", "austria", "österreich", "poland", "polska",
    "romania", "qatar", "wien", "vienna", "berlin", "frankfurt", "munich",
    "münchen", "hamburg", "warsaw", "krakow", "kraków", "bucharest", "doha",
    "graz", "linz", "leoben", "ludwigshafen", "ingelheim", "biberach",
]
ATS_HOSTS = {
    "greenhouse": ("boards.greenhouse.io", "job-boards.greenhouse.io"),
    "lever": ("jobs.lever.co", "jobs.eu.lever.co"),
    "ashby": ("jobs.ashbyhq.com",),
    "workable": ("apply.workable.com",),
    "smartrecruiters": ("jobs.smartrecruiters.com",),
}
session = requests.Session()
session.headers.update({"User-Agent": "PharmaJobRadar/3.0 (+GitHub Actions)"})


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def clean(x):
    return re.sub(r"\s+", " ", str(x or "")).strip()


def norm(x):
    return clean(x).lower()


def jid(company, url):
    return hashlib.sha256(f"{company}|{url}".encode()).hexdigest()[:20]


def absolute(url, base):
    return urljoin(base, url)


def ats_from_url(url):
    host = (urlparse(url).hostname or "").lower()
    for ats, hosts in ATS_HOSTS.items():
        if any(host == h or host.endswith("." + h) for h in hosts):
            return ats
    return None


def greenhouse_token(url):
    p = urlparse(url)
    if p.hostname and "greenhouse.io" in p.hostname:
        parts = [x for x in p.path.split("/") if x]
        if parts:
            return parts[0]
    return None


def lever_site(url):
    p = urlparse(url)
    if p.hostname and ("lever.co" in p.hostname):
        parts = [x for x in p.path.split("/") if x]
        if parts:
            return parts[0]
    return None


def ashby_board(url):
    p = urlparse(url)
    if p.hostname and "ashbyhq.com" in p.hostname:
        parts = [x for x in p.path.split("/") if x]
        if parts:
            return parts[0]
    return None


def workable_account(url):
    p = urlparse(url)
    if p.hostname and "workable.com" in p.hostname:
        parts = [x for x in p.path.split("/") if x]
        if parts:
            return parts[0]
    return None


def smartrecruiters_company(url):
    p = urlparse(url)
    if p.hostname and "smartrecruiters.com" in p.hostname:
        parts = [x for x in p.path.split("/") if x]
        if parts:
            return parts[0]
    return None


def title_relevant(title):
    t = norm(title)
    if len(t) < 4 or len(t) > 180:
        return False
    if any(x in t for x in HARD_EXCLUDE_TITLE):
        return False
    return any(re.search(p, t, re.I) for p in TARGET_TITLE_PATTERNS)


def relevance(title, description, location, roles):
    t, d, loc = norm(title), norm(description), norm(location)
    if any(x in t for x in HARD_EXCLUDE_TITLE):
        return 0

    title_hits = sum(bool(re.search(p, t, re.I)) for p in TARGET_TITLE_PATTERNS)
    desc_hits = sum(1 for x in TARGET_DESC_TERMS if x in d)
    role_hits = sum(1 for r in roles if norm(r) and norm(r) in f"{t} {d}")
    location_bonus = 5 if any(x in loc for x in LOCATION_HINTS) else 0

    score = title_hits * 24 + min(desc_hits, 8) * 5 + role_hits * 8 + location_bonus

    # Strong gate: title should be directly relevant OR description must contain
    # multiple quality terms. This removes navigation pages and unrelated jobs.
    if title_hits >= 1:
        score += 20
    elif desc_hits < 3:
        return 0

    return min(score, 100)


def normalize_job(company, raw, source):
    title = clean(raw.get("title") or raw.get("name"))
    url = raw.get("url") or raw.get("absolute_url") or raw.get("hostedUrl") or raw.get("applyUrl")
    if not title or not url:
        return None

    description = clean(
        BeautifulSoup(str(raw.get("description") or raw.get("content") or ""), "html.parser").get_text(" ")
    )
    location = clean(raw.get("location") or raw.get("location_name") or raw.get("locations") or "")
    if isinstance(raw.get("location"), dict):
        location = clean(raw["location"].get("name") or raw["location"].get("address") or "")
    if isinstance(raw.get("location"), list):
        location = clean(" ".join(
            x.get("name", "") if isinstance(x, dict) else str(x) for x in raw["location"]
        ))

    score = relevance(title, description, location, company.get("roles", []))
    if score < 45:
        return None

    return {
        "title": title,
        "url": url,
        "description": description[:4000],
        "location": location,
        "datePosted": raw.get("datePosted") or raw.get("publishedAt") or raw.get("createdAt"),
        "updatedAt": raw.get("updated_at") or raw.get("updatedAt"),
        "source": source,
        "score": score,
    }


def get_json(url, params=None):
    r = session.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def greenhouse_jobs(career_url):
    token = greenhouse_token(career_url)
    if not token:
        return []
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs", {"content": "true"})
    return data.get("jobs", [])


def lever_jobs(career_url):
    site = lever_site(career_url)
    if not site:
        return []
    for base in ("https://api.lever.co/v0/postings", "https://api.eu.lever.co/v0/postings"):
        try:
            r = session.get(f"{base}/{site}", params={"mode": "json", "limit": 100}, timeout=TIMEOUT)
            if r.ok:
                return r.json()
        except Exception:
            pass
    return []


def ashby_jobs(career_url):
    board = ashby_board(career_url)
    if not board:
        return []
    r = session.get(
        f"https://api.ashbyhq.com/posting-api/job-board/{board}",
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    return data.get("jobs", [])


def workable_jobs(career_url):
    account = workable_account(career_url)
    if not account:
        return []
    for url in [
        f"https://apply.workable.com/api/v1/widget/accounts/{account}",
        f"https://apply.workable.com/api/v1/widget/accounts/{account}/jobs",
    ]:
        try:
            r = session.get(url, timeout=TIMEOUT)
            if r.ok:
                data = r.json()
                return data.get("jobs", data if isinstance(data, list) else [])
        except Exception:
            pass
    return []


def smartrecruiters_jobs(career_url):
    company = smartrecruiters_company(career_url)
    if not company:
        return []
    jobs, offset = [], 0
    while offset < 500:
        data = get_json(
            f"https://api.smartrecruiters.com/v1/companies/{company}/postings",
            {"limit": 100, "offset": offset},
        )
        page = data.get("content", [])
        jobs.extend(page)
        if len(page) < 100:
            break
        offset += 100
    return jobs


async def rendered_page(page, url):
    await page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT)
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    # Let React/Next/Workday-style hydration finish.
    await page.wait_for_timeout(1500)
    return await page.content(), await page.locator("body").inner_text(timeout=5000)


def extract_jsonld(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    out = []

    def walk(x):
        if isinstance(x, dict):
            typ = x.get("@type")
            if typ == "JobPosting" or (isinstance(typ, list) and "JobPosting" in typ):
                out.append(x)
            for y in x.get("@graph", []) if isinstance(x.get("@graph"), list) else []:
                walk(y)
        elif isinstance(x, list):
            for y in x:
                walk(y)

    for tag in soup.find_all("script", type=lambda x: x and "ld+json" in x):
        try:
            walk(json.loads(tag.string or tag.get_text()))
        except Exception:
            pass

    return out


def links_from_rendered(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        title = clean(a.get_text(" "))
        url = absolute(a["href"], base_url).split("#")[0]
        if url in seen or not title:
            continue
        seen.add(url)
        if title_relevant(title):
            out.append({"title": title, "url": url})
    return out[:MAX_JOBS_PER_COMPANY]


async def browser_scan(page, company):
    html, body = await rendered_page(page, company["careers"])
    jobs = extract_jsonld(html, company["careers"])

    # Inspect rendered job links. This is the fallback for Workday and other
    # JS/ATS pages that do not expose a simple public API.
    candidates = links_from_rendered(html, company["careers"])
    for c in candidates:
        if any((j.get("url") or "") == c["url"] for j in jobs):
            continue
        try:
            h, b = await rendered_page(page, c["url"])
            structured = extract_jsonld(h, c["url"])
            if structured:
                jobs.extend(structured)
            else:
                # Use rendered page text only as evidence, never as a job title
                # by itself. The link title must already have passed title_relevant.
                jobs.append({
                    "title": c["title"],
                    "url": c["url"],
                    "description": b[:4000],
                })
        except (PlaywrightTimeoutError, Exception):
            continue
    return jobs


def api_scan(company):
    url = company["careers"]
    ats = ats_from_url(url)
    try:
        if ats == "greenhouse":
            return greenhouse_jobs(url), "Greenhouse API"
        if ats == "lever":
            return lever_jobs(url), "Lever API"
        if ats == "ashby":
            return ashby_jobs(url), "Ashby API"
        if ats == "workable":
            return workable_jobs(url), "Workable API"
        if ats == "smartrecruiters":
            return smartrecruiters_jobs(url), "SmartRecruiters API"
    except Exception as e:
        print(f"API fallback failed for {company['name']}: {e}")
    return [], None


async def main():
    os.makedirs(DATA, exist_ok=True)
    companies = json.load(open(COMP, encoding="utf-8"))

    old = []
    if os.path.exists(OUT):
        try:
            old = json.load(open(OUT, encoding="utf-8"))
        except Exception:
            old = []
    oldmap = {x.get("id"): x for x in old if x.get("id")}

    found = []
    failures = 0

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="PharmaJobRadar/3.0 (+GitHub Actions)",
            locale="en-US",
        )
        page = await context.new_page()

        for company in companies:
            try:
                raw, source = api_scan(company)
                if raw:
                    print(f"API  {company['name']}: {len(raw)} postings ({source})")
                else:
                    raw = await browser_scan(page, company)
                    source = "Playwright rendered ATS"

                count = 0
                for r in raw[:MAX_JOBS_PER_COMPANY]:
                    item = normalize_job(company, r, source)
                    if not item:
                        continue

                    ident = jid(company["name"], item["url"])
                    previous = oldmap.get(ident, {})
                    item.update({
                        "id": ident,
                        "company": company["name"],
                        "country": company["country"],
                        "priority": company.get("priority", "B"),
                        "sponsor": company.get("sponsor", 0),
                        "status": previous.get("status", "new"),
                        "foundAt": previous.get("foundAt", now_iso()),
                        "lastSeenAt": now_iso(),
                    })
                    found.append(item)
                    count += 1

                print(f"     -> {count} relevant QC/QA/GMP jobs")
            except Exception as e:
                failures += 1
                print(f"WARN {company['name']}: {e}")

        await browser.close()

    # Do NOT retain every old irrelevant record forever.
    # Keep an old record only if it is already a relevant job and was seen recently.
    current_ids = {x["id"] for x in found}
    cutoff = time.time() - 45 * 24 * 3600

    for x in old:
        if x.get("id") in current_ids:
            continue
        try:
            ts = datetime.fromisoformat(x.get("lastSeenAt", "").replace("Z", "+00:00")).timestamp()
        except Exception:
            ts = 0
        if ts >= cutoff and int(x.get("score", 0)) >= 55:
            found.append(x)

    # Deduplicate by canonical URL.
    unique = {}
    for x in found:
        unique[x["url"].split("#")[0]] = x
    found = list(unique.values())

    found.sort(key=lambda x: (int(x.get("score", 0)), x.get("lastSeenAt", "")), reverse=True)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(found[:MAX_STORED_JOBS], f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(
        f"FINISHED: {len(companies)} companies, "
        f"{len(found)} relevant jobs stored, {failures} failures."
    )


if __name__ == "__main__":
    asyncio.run(main())