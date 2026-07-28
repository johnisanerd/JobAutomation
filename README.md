# Job Automation Pipeline

This project is a Python-based job application automation system. It finds relevant software roles, scores them against a candidate profile, creates tailored application material, helps submit applications, tracks outcomes, and optionally sends recruiter outreach.

The pipeline is split into five agents:

1. **Agent 1 - Scout**: fetches jobs from ATS boards and job APIs, scores them, and writes queues.
2. **Agent 2 - Writer**: creates tailored resumes and cover letters for selected jobs.
3. **Agent 3 - Submitter**: opens application pages with Playwright and attempts to submit or prepare applications.
4. **Agent 4 - Tracker**: updates an Excel tracker and can inspect Gmail for application updates.
5. **Agent 5 - Recruiter**: finds recruiter contacts and sends outreach emails through Gmail.

There is also a resumable runner and a local dashboard so failed steps can be restarted without repeating the whole workflow.

## Important Safety Note

Do not commit personal credentials or generated application data to GitHub.

This repository should exclude:

- `.env`
- `credentials.json`
- `token.json`
- `sessions/`
- `logs/`
- `outputs/`
- `state/`

Those files can contain API keys, OAuth tokens, browser cookies, application history, screenshots, resumes, cover letters, and personal job data. They are intentionally listed in `.gitignore`.

## Project Structure

```text
agents/
  agent1_scout.py       # finds and scores jobs
  agent2_writer.py      # writes tailored resume/cover letter assets
  agent3_submitter.py   # browser automation for applications
  agent4_tracker.py     # Excel/Gmail tracking
  agent5_recruiter.py   # recruiter contact discovery and outreach

config/
  profile.py            # candidate profile and main customization file

core/
  dedupe.py             # application/job de-duplication helpers
  gmail_token.py        # Gmail token refresh helper

resume/
  main.tex              # base LaTeX resume
  AkshitSainiResume.tex # alternate/source resume file

orchestrator.py         # simple full 5-agent run
pipeline_runner.py      # resumable runner for one agent or all agents
web_dashboard.py        # browser dashboard at http://localhost:8888
dashboard.py            # terminal dashboard
gmail_auth.py           # one-time Gmail OAuth setup
setup_sessions.py       # saves browser login sessions for supported sites
guided_submit.py        # guided/manual mode for applications needing review
resume_pipeline.py      # resume from failed/unfinished steps
```

Runtime folders are created locally:

```text
outputs/   # generated queues, applications, PDFs, screenshots, Excel tracker
logs/      # agent and orchestrator logs
state/     # resumable runner progress
sessions/  # saved browser login cookies
```

These runtime folders should stay local and are ignored by Git.

## Requirements

- Python 3.11 or newer. The original machine used Python 3.12.
- Playwright browsers for Agent 3.
- API keys depending on which agents/features you use.
- LaTeX installed locally if you want Agent 2 to compile PDF resumes.

Install Python dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install
```

If `python3` points to the wrong interpreter on your machine, replace it with your Python path.

## Environment Variables

Create a local `.env` file in the project root. Do not commit it. You can start by copying `.env.example`:

```bash
cp .env.example .env
```

Minimum useful setup:

```env
ANTHROPIC_API_KEY=your_anthropic_key
```

Optional keys:

```env
RAPIDAPI_KEY=your_rapidapi_key
APIFY_API_TOKEN=your_apify_token
ADZUNA_APP_ID=your_adzuna_app_id
ADZUNA_APP_KEY=your_adzuna_app_key
REED_API_KEY=your_reed_key
JOOBLE_API_KEY=your_jooble_key
MUSE_API_KEY=your_muse_key

HUNTER_API_KEY=your_hunter_key
APOLLO_API_KEY=your_apollo_key

GMAIL_ACCESS_TOKEN=filled_by_gmail_auth
GOOGLE_SHEETS_TOKEN=optional_google_sheets_token
SPREADSHEET_ID=optional_google_sheet_id
```

For browser login automation in `setup_sessions.py`, add only the credentials you actually need:

```env
STRIPE_EMAIL=you@example.com
STRIPE_PASSWORD=your_password
LINKEDIN_EMAIL=you@example.com
LINKEDIN_PASSWORD=your_password
OKTA_EMAIL=you@example.com
OKTA_PASSWORD=your_password
AIRBNB_EMAIL=you@example.com
AIRBNB_PASSWORD=your_password
DATABRICKS_EMAIL=you@example.com
DATABRICKS_PASSWORD=your_password
```

## Gmail Setup

Agents 4 and 5 use Gmail for reading application updates and sending recruiter outreach.

1. Create OAuth credentials in Google Cloud Console for an installed/desktop app.
2. Download the credentials JSON.
3. Save it locally as `credentials.json` in the project root.
4. Run:

```bash
python3 gmail_auth.py
```

This creates `token.json` and updates `.env` with `GMAIL_ACCESS_TOKEN`. Both files must stay private.

## Main Customization Points

Most personal changes happen in `config/profile.py`.

Update:

- `PROFILE["name"]`, email, phone, location, LinkedIn, GitHub, visa status.
- `PROFILE["target_roles"]` and `PROFILE["target_locations"]`.
- `PROFILE["salary_min_eur"]` and `PROFILE["salary_max_eur"]`.
- `PROFILE["score_threshold"]` to control which jobs are considered strong enough.
- `PROFILE["skills"]`, `experience`, `education`, `projects`, `certifications`, and `key_achievements`.
- `SEARCH_QUERIES` for search terms and locations.
- `GREENHOUSE_SLUGS`, `ASHBY_SLUGS`, and `LEVER_SLUGS` to change company sources.
- `agent3_skip_companies` to avoid submitting to companies already handled.
- `max_roles_per_company` to limit repeated applications to the same company.

Resume changes happen in:

```text
resume/main.tex
resume/AkshitSainiResume.tex
```

If Agent 2 needs to pull resume content from GitHub, update these constants in `config/profile.py`:

```python
GITHUB_RESUME_REPO = "your-username/your-resume-repo"
GITHUB_RESUME_FILE = "main.tex"
GITHUB_BRANCH = "main"
```

## How to Run

Run the complete pipeline once:

```bash
python3 orchestrator.py
```

Run it daily at 07:30 Dublin time:

```bash
python3 orchestrator.py --schedule
```

Use the resumable runner:

```bash
python3 pipeline_runner.py all
python3 pipeline_runner.py agent1
python3 pipeline_runner.py agent2
python3 pipeline_runner.py agent3
python3 pipeline_runner.py agent4
python3 pipeline_runner.py agent5
```

Restart progress for a step:

```bash
python3 pipeline_runner.py agent3 --restart
```

Resume from unfinished work:

```bash
python3 resume_pipeline.py
```

Run guided/manual submission for applications that need human review:

```bash
python3 guided_submit.py
```

## Dashboard

Start the web dashboard:

```bash
python3 web_dashboard.py
```

Then open:

```text
http://localhost:8888
```

The dashboard can show agent progress, logs, application counts, and tracker status.

For a terminal dashboard:

```bash
python3 dashboard.py
```

## Browser Sessions

Some sites require login or have auth walls. Save browser sessions with:

```bash
python3 setup_sessions.py
```

For automatic login using `.env` credentials:

```bash
python3 setup_sessions.py --auto
```

Saved browser sessions go into `sessions/`, which is ignored by Git because it contains login cookies.

## Outputs

Generated files are written under `outputs/`.

Common files:

- `outputs/tracker_queue.jsonl`: jobs found by Scout.
- `outputs/agent2_queue.jsonl`: jobs queued for Writer.
- `outputs/agent3_queue.jsonl`: jobs queued for Submitter.
- `outputs/agent4_submitted.jsonl`: submitted/review-needed application records.
- `outputs/Akshit_Applications.xlsx`: Excel tracker.
- `outputs/applications/<company_jobid>/`: generated application JSON, cover letter, tailored resume TeX, and PDF.
- `outputs/screenshots/`: application page screenshots.

These are local run artifacts and should not be committed unless you intentionally want to share sample data after removing private information.

## Putting This on GitHub

From the project root:

```bash
git init
git add README.md .gitignore .env.example requirements.txt agents config core resume *.py
git status
git commit -m "Initial commit"
```

Create an empty GitHub repository, then connect and push:

```bash
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/JobAutomation.git
git push -u origin main
```

Before pushing, run:

```bash
git status --short
```

Make sure `.env`, `credentials.json`, `token.json`, `outputs/`, `logs/`, `state/`, and `sessions/` are not staged.

## Suggested First Changes for a New User

1. Replace the candidate data in `config/profile.py`.
2. Replace the LaTeX resume files in `resume/`.
3. Create a private `.env` with their own API keys.
4. Run `python3 gmail_auth.py` if they want Gmail tracking/outreach.
5. Run `python3 pipeline_runner.py agent1` first to test job discovery.
6. Inspect generated queues in `outputs/` before running application submission.
7. Use `guided_submit.py` for anything that requires human verification or CAPTCHA.

## Notes

This project automates parts of the job application workflow, but it should still be supervised. Many application sites have anti-bot checks, custom forms, and legal/ethical terms around automation. Use guided mode when needed, review generated resumes and cover letters before submitting, and avoid sending outreach that has not been checked.
