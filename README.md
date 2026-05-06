# GSA Contract Tracker

A dashboard for tracking cancellations, terminations, and changes in GSA contract vehicles — with a primary focus on the **Multiple Award Schedule (MAS)** and **BPA** programs.

Data is sourced from **GSA eLibrary** (daily) and **USASpending.gov** (monthly). The dashboard surfaces cancellation-pending contracts (visible in USASpending before eLibrary reflects them), change history on the vendor roster, and the full terminations table with filtering by vehicle, category, agency, and set-aside type.

**Live dashboard:** *(Streamlit Community Cloud URL — add after deploying)*

---

## Features

- **Overview** — KPI summary, termination trend chart, breakdown by reason
- **Cancellation Pending** — Contracts with a USASpending termination record still showing active in eLibrary (~30–45 day lag)
- **Terminations** — Full filterable/downloadable terminations table (MAS/BPA contracts only)
- **Vendor Roster** — Active vendor list with termination flags and expiration dates
- **Recent Changes** — Daily eLibrary diff: new contracts, removals, SIN changes, field updates

---

## Data Sources

| Source | What | Frequency |
|---|---|---|
| [GSA eLibrary](https://gsaelibrary.gsa.gov/elib_contracts/schedule_MAS.csv) | MAS vendor roster | Daily |
| [GSA eLibrary](https://www.gsaelibrary.gsa.gov/ElibMain/home.do) | All contract vehicle CSVs (discovered dynamically) | Daily |
| [USASpending Bulk Archive](https://files.usaspending.gov/award_data_archive/) | Contract termination modifications (E/F/X) | Monthly |
| [GSA D2D](https://d2d.gsa.gov) | Schedule sales by vendor/contract/SIN | TBD |

---

## Quick Start (Local)

### Prerequisites

- Python 3.11+
- [Git for Windows](https://git-scm.com/download/win) (includes Git Bash)
- A GitHub account

### 1. Clone and set up

```bash
git clone https://github.com/YOUR_USERNAME/gsa-contract-tracker.git
cd gsa-contract-tracker

python -m venv .venv
source .venv/Scripts/activate    # Windows Git Bash
# or: .venv\Scripts\activate     # Windows CMD/PowerShell

pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.template .env
# Open .env and fill in any credentials (currently none required for core data)
```

### 3. Set up pre-commit hooks (one-time)

```bash
pre-commit install
detect-secrets scan > .secrets.baseline
```

### 4. Bootstrap the database

```bash
python -m pipeline.bootstrap
```

This creates `db/contracts.db` with the full schema. Safe to re-run.

### 5. Load data

```bash
# Pull MAS and BPA vendor data from GSA eLibrary
python -m pipeline.fetch_elib

# Pull termination records from USASpending (FY2025–2026)
# Note: this downloads large files — allow 30–60+ minutes on first run
python -m pipeline.fetch_terminations
```

### 6. Run the dashboard

```bash
streamlit run app/main.py
```

Open [http://localhost:8501](http://localhost:8501)

---

## Git Setup (First Time — Run from Git Bash)

If you cloned this fresh, git is already configured. If you're setting up from the source files for the first time:

```bash
cd gsa-contract-tracker

git init -b main
git config user.name  "Your Name"
git config user.email "your@email.com"

# Initialize the detect-secrets baseline (required before first commit)
pip install detect-secrets pre-commit
detect-secrets scan > .secrets.baseline
pre-commit install

git add .
git commit -m "feat: initial project scaffold"
```

Then create a new repo on GitHub (do **not** initialize it with a README), and:

```bash
git remote add origin https://github.com/YOUR_USERNAME/gsa-contract-tracker.git
git push -u origin main
```

---

## GitHub Actions Setup

The pipelines run automatically via GitHub Actions. Two one-time setup steps are required:

### Create a Personal Access Token (PAT)

1. GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)
2. Generate new token → scope: **repo** (full control)
3. Copy the token value

### Add the PAT as a Repository Secret

1. Your repo → Settings → Secrets and variables → Actions → New repository secret
2. Name: `GH_PAT`
3. Value: *(paste your token)*

That's it. The workflows will:
- Run daily at 9 AM UTC — fetch eLibrary data, detect changes, commit updated DB
- Run on the 1st of each month at 8 AM UTC — check for new USASpending archive data, pull terminations, commit updated DB

You can also trigger either workflow manually from the **Actions** tab on GitHub.

---

## Streamlit Community Cloud Deployment

1. Push the repo to GitHub (public)
2. Go to [share.streamlit.io](https://share.streamlit.io)
3. Connect your GitHub account → select this repo
4. Set **Main file path**: `app/main.py`
5. Deploy

No additional configuration required for core functionality. If D2D credentials are needed in the future, add them as Streamlit secrets.

---

## Project Structure

```
gsa-contract-tracker/
├── pipeline/
│   ├── bootstrap.py          # Creates SQLite DB and schema (first-run)
│   ├── fetch_elib.py         # Daily eLibrary fetch + change detection
│   ├── fetch_terminations.py # Monthly USASpending bulk archive pipeline
│   └── detect_changes.py     # Levenshtein-based diff logic
├── app/
│   ├── main.py               # Streamlit entry point (Overview page)
│   ├── db.py                 # All SQL queries
│   └── pages/
│       ├── 1_Cancellation_Pending.py
│       ├── 2_Terminations.py
│       ├── 3_Vendor_Roster.py
│       └── 4_Recent_Changes.py
├── config/
│   └── config.yaml           # All source URLs, thresholds, vehicle registry
├── db/
│   └── contracts.db          # SQLite database (managed by GitHub Actions)
├── .github/workflows/
│   ├── daily_refresh.yml     # eLibrary daily cron
│   └── monthly_refresh.yml   # USASpending monthly cron
├── .env.template             # Copy to .env and fill in credentials
├── .gitignore
├── .pre-commit-config.yaml   # Credential scanning + code quality hooks
└── requirements.txt
```

---

## Adding a New Contract Vehicle

1. Add a row to `contract_vehicles` section in `config/config.yaml` with `enabled: true`
2. Add the CSV URL
3. Run `python -m pipeline.bootstrap` (safe to re-run — uses INSERT OR IGNORE)
4. Run `python -m pipeline.fetch_elib --vehicle YOUR_CODE`

---

## Credential Safety

This project is designed to never store credentials in code or version control:

- All secrets go in `.env` (local) or GitHub repository secrets (Actions)
- `.env` is in `.gitignore` — it will never be committed
- `pre-commit` runs `detect-secrets` on every commit and blocks if credentials are detected
- Scripts fail loudly if required environment variables are missing — no silent fallbacks

---

## License

MIT
