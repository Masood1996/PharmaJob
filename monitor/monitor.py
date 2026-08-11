import hashlib
import json
import os
import re
import smtplib
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "docs", "data")
COMP = os.path.join(DATA, "companies.json")
OUT = os.path.join(DATA, "jobs.json")

USER_AGENT = "PharmaJobRadar/2.0 (+GitHub Actions)"
TIMEOUT = 25
MAX_LINKS_PER_COMPANY = 30
MAX_JOBS = 500

KEYWORDS = [
    "quality control", "quality assurance", "quality specialist",
    "quality operations", "quality systems", "quality compliance",
    "qc specialist", "qc analyst", "qa specialist", "qa analyst",
    "laboratory analyst", "laboratory specialist", "microbiology",
    "gmp", "good manufacturing practice", "sterile", "aseptic",
    "injectable", "validation", "qualification", "batch release",
    "deviation", "capa", "oos", "data integrity", "in-process control",
]

JOB_HINTS = [
    "job", "jobs", "vacancy", "vacancies", "position", "positions",
    "apply", "stellenangebot", "stellenangebote", "karriere",
    "career", "bewerben", "offene-stellen", "jobangebot",
]

GENERIC_WORDS = {
    "career", "careers", "jobs", "job", "home", "about", "contact",
    "benefits", "locations", "location", "culture", "teams", "team",
    "search", "login", "sign in", "privacy", "terms", "imprint",
    "deutsch", "english", "français", "francais", "cookie",
    "open positions", "all positions", "view jobs", "job search",
    "career opportunities", "learn more", "read more",
}

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def clean(value):
    return re.sub(r"\s+", " ", value or "").strip()


def norm(value):
    return clean(value).lower()


def make_id(company, url):
    return hashlib.sha256(f"{company}|{url}".encode("utf-8")).hexdigest()[:16]


def get(url):
    response = session.get(url, timeout=TIMEOUT, allow_redirects=True)
    response.raise_for_status()
    return response


def jsonld_objects(soup):
    objects = []
    for tag in soup.find_all("script", type=lambda x: x and "ld+json" in x):
        raw = tag.string or tag.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if isinstance(data, list):
            objects.extend(data)
        else:
            objects.append(data)
    return objects


def walk_job_postings(obj):
    """Yield JobPosting dictionaries from JSON-LD, including @graph."""
    if isinstance(obj, dict):
        typ = obj.get("@type")
        if typ == "JobPosting" or (isinstance(typ, list) and "JobPosting" in typ):
            yield obj
        graph = obj.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                yield from walk_job_postings(item)
    elif isinstance(obj, list):
        for item in obj:
            yield from walk_job_postings(item)


def extract_jsonld_jobs(soup, base_url):
    jobs = []
    for obj in jsonld_objects(soup):
        for job in walk_job_postings(obj):
            title = clean(job.get("title") or job.get("name"))
            if not title:
                continue
            url = job.get("url") or base_url
            url = urljoin(base_url, url)
            jobs.append({
                "title": title,
                "url": url,
                "description": clean(BeautifulSoup(job.get("description", ""), "html.parser").get_text(" ")),
                "datePosted": job.get("datePosted"),
                "validThrough": job.get("validThrough"),
                "source": "JobPosting JSON-LD",
            })
    return jobs


def likely_job_title(title, url):
    t = norm(title)
    u = norm(url)

    if not t or len(t) < 4 or len(t) > 180:
        return False

    if t in GENERIC_WORDS:
        return False

    # Explicit job/role terms are strong evidence.
    role_words = [
        "quality", "qc", "qa", "gmp", "laboratory", "microbiology",
        "analyst", "specialist", "scientist", "technician", "engineer",
        "manager", "operator", "validation", "qualification", "production",
        "manufacturing", "pharma", "pharmaceutical", "sterility",
        "compliance", "assurance", "control", "batch",
    ]
    has_role = any(w in t for w in role_words)
    has_job_hint = any(w in t or w in u for w in JOB_HINTS)

    # Reject obvious navigation labels.
    if any(x == t for x in GENERIC_WORDS):
        return False

    return has_role and (has_job_hint or len(t.split()) >= 2)


def extract_candidate_links(soup, base_url):
    candidates = []
    seen = set()

    for a in soup.find_all("a", href=True):
        title = clean(a.get_text(" "))
        href = urljoin(base_url, a.get("href"))
        if not href.startswith(("http://", "https://")):
            continue

        # Ignore obvious non-job links.
        parsed = urlparse(href)
        if parsed.scheme not in ("http", "https"):
            continue

        key = href.split("#", 1)[0]
        if key in seen:
            continue

        if likely_job_title(title, href):
            seen.add(key)
            candidates.append((title, key))

    return candidates[:MAX_LINKS_PER_COMPANY]


def score(title, description, roles, priority):
    blob = norm(f"{title} {description}")
    points = 0

    for keyword in KEYWORDS:
        if keyword in blob:
            points += 6

    for role in roles:
        if norm(role) in blob:
            points += 12

    if any(x in blob for x in ("quality control", "quality assurance", "qc specialist", "qa specialist")):
        points += 18

    if "gmp" in blob or "good manufacturing practice" in blob:
        points += 10

    if any(x in blob for x in ("sterile", "aseptic", "injectable", "microbiology")):
        points += 8

    if priority == "S":
        points += 4
    elif priority == "A":
        points += 2

    return min(100, points)


def parse_job_page(url, fallback_title):
    """Fetch a likely job link and extract structured JobPosting data when available."""
    try:
        response = get(url)
        soup = BeautifulSoup(response.text, "html.parser")
        structured = extract_jsonld_jobs(soup, response.url)

        if structured:
            job = structured[0]
            return {
                "title": job["title"],
                "url": job["url"],
                "description": job["description"],
                "datePosted": job.get("datePosted"),
                "validThrough": job.get("validThrough"),
                "source": job.get("source"),
            }

        # Fallback: use the anchor title only if the destination looks like a job page.
        page_title = clean(soup.title.get_text(" ")) if soup.title else ""
        chosen = page_title if likely_job_title(page_title, response.url) else fallback_title

        return {
            "title": chosen,
            "url": response.url,
            "description": clean(soup.get_text(" "))[:3000],
            "datePosted": None,
            "validThrough": None,
            "source": "Career-page link",
        }
    except Exception:
        return None


def scan_company(company):
    response = get(company["careers"])
    soup = BeautifulSoup(response.text, "html.parser")

    jobs = extract_jsonld_jobs(soup, response.url)

    # Also inspect likely job links. This catches many ATS pages that do not expose
    # all vacancies in the career page's HTML.
    for title, url in extract_candidate_links(soup, response.url):
        if any(j["url"].split("#", 1)[0] == url for j in jobs):
            continue

        parsed = parse_job_page(url, title)
        if parsed:
            jobs.append(parsed)

        # Keep the GitHub Action polite and avoid hammering a career site.
        time.sleep(0.15)

    # De-duplicate URLs within this company.
    unique = {}
    for job in jobs:
        url = job.get("url")
        title = clean(job.get("title"))
        if not url or not likely_job_title(title, url):
            continue
        unique[url] = job

    return list(unique.values())


def main():
    os.makedirs(DATA, exist_ok=True)

    with open(COMP, encoding="utf-8") as f:
        companies = json.load(f)

    old = []
    if os.path.exists(OUT):
        try:
            with open(OUT, encoding="utf-8") as f:
                old = json.load(f)
        except Exception:
            old = []

    oldmap = {item.get("id"): item for item in old if item.get("id")}
    found = []
    failures = 0

    for company in companies:
        try:
            jobs = scan_company(company)

            for job in jobs:
                jid = make_id(company["name"], job["url"])
                previous = oldmap.get(jid, {})

                item = {
                    **previous,
                    "id": jid,
                    "company": company["name"],
                    "country": company["country"],
                    "priority": company.get("priority", "B"),
                    "sponsor": company.get("sponsor", 0),
                    "title": job["title"],
                    "url": job["url"],
                    "score": score(
                        job["title"],
                        job.get("description", ""),
                        company.get("roles", []),
                        company.get("priority", "B"),
                    ),
                    "description": clean(job.get("description", ""))[:1000],
                    "source": job.get("source", "Career page"),
                    "datePosted": job.get("datePosted"),
                    "validThrough": job.get("validThrough"),
                    "lastSeenAt": now_iso(),
                    "status": previous.get("status", "new"),
                }

                if "foundAt" not in item:
                    item["foundAt"] = now_iso()

                found.append(item)

            print(f"OK   {company['name']}: {len(jobs)} job candidates")

        except Exception as exc:
            failures += 1
            print(f"WARN {company['name']}: {exc}")

    # Keep jobs from previous scans if their company was temporarily unreachable.
    by_id = {item["id"]: item for item in found}

    for item in old:
        if item.get("id") not in by_id:
            # Keep the historical record, but do not falsely update lastSeenAt.
            by_id[item["id"]] = item

    merged = list(by_id.values())

    # Prefer active/new high-score jobs, then recent sightings.
    merged.sort(
        key=lambda x: (
            x.get("status") == "new",
            int(x.get("score", 0)),
            x.get("lastSeenAt", ""),
        ),
        reverse=True,
    )

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(merged[:MAX_JOBS], f, ensure_ascii=False, indent=2)
        f.write("\n")

    # Alert only genuinely new high-confidence matches.
    new = [
        item for item in found
        if item["id"] not in oldmap and int(item.get("score", 0)) >= 55
    ]

    if new and os.getenv("SMTP_HOST") and os.getenv("ALERT_TO"):
        msg = EmailMessage()
        msg["Subject"] = f"Pharma Job Radar: {len(new)} new match(es)"
        msg["From"] = os.getenv("SMTP_FROM") or os.getenv("SMTP_USER")
        msg["To"] = os.getenv("ALERT_TO")

        body = "\n\n".join(
            f"{x['company']} ({x['country']})\n"
            f"{x['title']}\n{x['url']}\n"
            f"Match: {x['score']}%"
            for x in new
        )
        msg.set_content(body)

        with smtplib.SMTP(os.getenv("SMTP_HOST"), int(os.getenv("SMTP_PORT", "587"))) as smtp:
            smtp.starttls()
            smtp.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASS"))
            smtp.send_message(msg)

    print(
        f"Scanned {len(companies)} companies; "
        f"{len(found)} current candidates; "
        f"{len(merged[:MAX_JOBS])} records stored; "
        f"{len(new)} new high-confidence alerts; "
        f"{failures} company scan failures."
    )


if __name__ == "__main__":
    main()
