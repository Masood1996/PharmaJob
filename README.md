# Pharma Job Radar
A PWA + scheduled monitor for Masoud's 60-company pharma job search across Germany, Poland, Romania and Qatar.

## What it does
- Tracks 60 target companies and career URLs.
- Matches QC, QA, GMP, laboratory, sterile/aseptic/injectable and quality-system roles.
- Stores application status in browser localStorage.
- Can be installed as a PWA on Android.
- A scheduled GitHub Actions workflow can scan career pages every 6 hours and email high-confidence new matches.

## Run the web app locally
From this folder:
`python -m http.server 8080 --directory web`
Then open `http://localhost:8080`.

The company JSON is served from `../data/companies.json`, so use a simple local web server rather than opening index.html directly.

## Automated monitoring
The monitor is `monitor/monitor.py`. Install `monitor/requirements.txt`, then run it. For GitHub Actions, add repository secrets:
- SMTP_HOST
- SMTP_PORT
- SMTP_USER
- SMTP_PASS
- SMTP_FROM
- ALERT_TO

The workflow scans every 6 hours. Career pages with bot protection or JavaScript-only job boards may need a company-specific adapter; the dashboard still keeps the career link for manual checking.

## Android
`android/` is a minimal Android Studio WebView wrapper that loads the hosted web app. Build it with Android Studio. A precompiled APK cannot be produced in this environment because an Android SDK/Gradle toolchain is not installed.


FIX:
# Job Radar path fix

Replace these files in the repository:

- `docs/data/companies.json`
- `monitor/monitor.py`
- `.github/workflows/scan.yml`

The scanner now reads and writes only under `docs/data/`.

Expected live data paths:
- `docs/data/companies.json`
- `docs/data/jobs.json`

GitHub Pages should continue publishing from `/docs`.

The `sponsor` value is a prioritization heuristic from the original tracker, not a guarantee that the company will sponsor a visa.

