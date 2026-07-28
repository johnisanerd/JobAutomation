"""
AGENT 2 — APPLICATION WRITER (Local .tex version)
Reads each job from Agent 1's queue.
1. Analyses the JD with Claude (keywords, ATS terms, focus area)
2. Tailors resume content — rewrites summary, reorders/rephrases bullets
3. Patches your local .tex file with tailored content
4. Compiles .tex → PDF using pdflatex (installed locally)
5. Generates a custom cover letter
6. Saves everything and queues for Agent 3
"""

import asyncio, json, logging, os, re, shutil, subprocess, tempfile
from datetime import datetime
from pathlib import Path
import anthropic
from dotenv import load_dotenv

load_dotenv()

from config.profile import PROFILE

log = logging.getLogger("agent2_writer")
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [WRITER] %(message)s",
    handlers=[logging.FileHandler("logs/agent2.log"), logging.StreamHandler()])

ANTHROPIC_KEY  = os.getenv("ANTHROPIC_API_KEY", "YOUR_ANTHROPIC_KEY")
client         = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# ── Path to your .tex file (drop it here once) ────────────────────────────────
TEX_SOURCE     = Path("resume/main.tex")
TEX_BACKUP     = Path("resume/main.tex.backup")

# ── LaTeX compiler — resume uses fontspec so xelatex is required ─────────────
# Try system PATH first, then TinyTeX user install location
_TINYTEX = Path.home() / "Library/TinyTeX/bin/universal-darwin"
def _find_compiler() -> str | None:
    for name in ("xelatex", "pdflatex"):
        if shutil.which(name):
            return name
        tinytex_path = _TINYTEX / name
        if tinytex_path.exists():
            return str(tinytex_path)
    return None
LATEX_CMD = _find_compiler()

# ─────────────────────────────────────────────────────────────────────────────
# STEP 0 — VERIFY SETUP
# ─────────────────────────────────────────────────────────────────────────────
def check_setup():
    Path("resume").mkdir(exist_ok=True)
    if not TEX_SOURCE.exists():
        raise FileNotFoundError(
            f"\n❌ Resume .tex not found at {TEX_SOURCE}\n"
            "👉 Copy your Overleaf .tex file to: job_automation/resume/main.tex\n"
            "   (Download from Overleaf: Menu → Source → Download .zip → extract main.tex)"
        )
    # Backup original once
    if not TEX_BACKUP.exists():
        shutil.copy(TEX_SOURCE, TEX_BACKUP)
        log.info(f"Original resume backed up → {TEX_BACKUP}")

    if not LATEX_CMD:
        log.warning(
            "xelatex/pdflatex not found. "
            "Install TinyTeX (no sudo needed): "
            "curl -sL https://yihui.org/tinytex/install-bin-unix.sh | sh\n"
            "Agent 2 will save tailored .tex but skip PDF compilation until installed."
        )
        return False
    log.info(f"LaTeX compiler: {LATEX_CMD}")
    return True

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — ANALYSE JD
# ─────────────────────────────────────────────────────────────────────────────
def analyse_jd(job: dict) -> dict:
    prompt = f"""Analyse this job description. Return JSON only, no markdown.

JOB TITLE: {job['title']}
COMPANY: {job['company']}
DESCRIPTION: {job['description'][:3000]}

Return exactly:
{{
  "required_skills": ["top 6 technical skills explicitly mentioned"],
  "nice_to_have": ["up to 4 bonus skills"],
  "key_responsibilities": ["top 4 responsibilities in their exact words"],
  "company_values": ["1-2 culture signals from the JD"],
  "ats_keywords": ["10 exact keywords to include in resume for ATS pass"],
  "role_level": "junior|mid|senior|lead",
  "focus": "fullstack|backend|frontend|ml|devops|general",
  "tone": "startup|corporate|technical|product"
}}"""
    msg = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=600,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = msg.content[0].text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
    return json.loads(raw)

# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — TAILOR RESUME CONTENT VIA CLAUDE
# ─────────────────────────────────────────────────────────────────────────────
def tailor_resume_content(job: dict, jd: dict) -> dict:
    exp_text = "\n".join(
        f"[{e['title']} @ {e['company']}]\n" + "\n".join(f"- {b}" for b in e["bullets"])
        for e in PROFILE["experience"]
    )
    prompt = f"""Tailor this software engineer's resume for the specific job below.
RULES:
- Keep ALL dates, titles, company names, education EXACTLY unchanged
- Only rewrite: (1) summary, (2) bullet points — reorder + rephrase to emphasise match
- Weave in ATS keywords naturally — do NOT keyword-stuff
- Do NOT invent any experience. Stay 100% truthful.
- Keep bullet count the same per role

TARGET JOB: {job['title']} at {job['company']}
REQUIRED SKILLS: {', '.join(jd.get('required_skills', []))}
ATS KEYWORDS: {', '.join(jd.get('ats_keywords', []))}
JOB FOCUS: {jd.get('focus', 'general')} | TONE: {jd.get('tone', 'technical')}

ORIGINAL SUMMARY:
{PROFILE['summary']}

ORIGINAL EXPERIENCE:
{exp_text}

Return JSON only:
{{
  "tailored_summary": "2-3 sentences, mirrors JD language, mentions Stamp 1G visa",
  "experience": [
    {{
      "company": "exact company name",
      "bullets": ["most relevant bullet first", "bullet 2", "...all original bullets rephrased/reordered"]
    }}
  ]
}}"""
    msg = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = msg.content[0].text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
    return json.loads(raw)

# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — PATCH .TEX FILE
# ─────────────────────────────────────────────────────────────────────────────
def patch_tex(tailored: dict, job: dict) -> str:
    """
    Read the original backed-up .tex, patch summary + bullets, return new content.

    Supports two patching strategies:
    A) Marker-based  — if you add %% SUMMARY_START / END comments (recommended)
    B) Regex-based   — works on standard Jake's Resume / moderncv templates automatically
    """
    tex = TEX_BACKUP.read_text(encoding="utf-8")
    summary  = tailored["tailored_summary"]
    exp_map  = {e["company"].lower(): e["bullets"] for e in tailored.get("experience", [])}

    # ── Strategy A: marker-based ──────────────────────────────────────────────
    if "%% SUMMARY_START" in tex:
        tex = re.sub(
            r"%%\s*SUMMARY_START.*?%%\s*SUMMARY_END",
            f"%% SUMMARY_START\n{summary}\n%% SUMMARY_END",
            tex, flags=re.DOTALL
        )
    else:
        # ── Strategy B: common LaTeX resume templates ─────────────────────────
        # Jake's Resume / resume.cls style: \resumeSubheading + \resumeItemListStart
        # Replace the summary/objective section
        tex = re.sub(
            r"(\\section\{[Ss]ummary\}|\\section\{[Oo]bjective\}|\\section\*\{[Ss]ummary\})"
            r"(.*?)(\\section)",
            lambda m: m.group(1) + "\n\\begin{justify}\n" + summary + "\n\\end{justify}\n\n" + m.group(3),
            tex, flags=re.DOTALL
        )
        # Also try \resumeSummary or plain paragraph after \textbf{Summary}
        if summary not in tex:
            tex = re.sub(
                r"(\\textbf\{Summary\}|\\textbf\{Profile\})(.*?)(?=\\vspace|\\section|\\medskip)",
                lambda m: m.group(1) + "\\\\\n" + summary + "\n",
                tex, flags=re.DOTALL
            )

    # ── Patch experience bullets ───────────────────────────────────────────────
    for company_key, bullets in exp_map.items():
        # Find the company block in .tex (case-insensitive partial match)
        company_pattern = re.compile(
            r"(" + re.escape(company_key.split()[0].capitalize()) + r".*?"
            r"\\resumeItemListStart)(.*?)(\\resumeItemListEnd)",
            re.DOTALL | re.IGNORECASE
        )
        bullets_tex = "\n".join(
            f"          \\resumeItem{{{b}}}" for b in bullets
        )
        def replacer(m, bt=bullets_tex):
            return m.group(1) + "\n" + bt + "\n          " + m.group(3)
        tex = company_pattern.sub(replacer, tex)

    return tex

# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — COMPILE .TEX → PDF
# ─────────────────────────────────────────────────────────────────────────────
def compile_tex(tex_content: str, output_dir: Path, job_slug: str) -> Path | None:
    """Write patched .tex to a temp dir, compile with pdflatex, return PDF path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp    = Path(tmpdir)
        tex_fp = tmp / "resume.tex"
        tex_fp.write_text(tex_content, encoding="utf-8")

        # Copy any auxiliary files (cls, sty, images) from resume/ folder
        for f in Path("resume").iterdir():
            if f.suffix in (".cls", ".sty", ".png", ".jpg", ".pdf") and f.is_file():
                shutil.copy(f, tmp / f.name)

        result = subprocess.run(
            [LATEX_CMD, "-interaction=nonstopmode", "-halt-on-error", "resume.tex"],
            cwd=tmpdir, capture_output=True, text=True, timeout=90
        )

        if result.returncode != 0:
            log.error(f"LaTeX compile failed:\n{result.stdout[-800:]}")
            return None

        pdf_src  = tmp / "resume.pdf"
        pdf_dest = output_dir / f"resume_{job_slug}.pdf"
        shutil.copy(pdf_src, pdf_dest)
        log.info(f"PDF compiled → {pdf_dest}")
        return pdf_dest

# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 — GENERATE COVER LETTER
# ─────────────────────────────────────────────────────────────────────────────
def generate_cover_letter(job: dict, jd: dict) -> str:
    achievements = "\n".join(f"- {a}" for a in PROFILE["key_achievements"][:5])
    prompt = f"""Write a cover letter for {PROFILE['name']} applying for {job['title']} at {job['company']}.

TONE: Concise, professional, bold — open with an impact number. Human, not generic. Max 280 words.
STRUCTURE: 3 paragraphs — (1) hook with a number, (2) 2 specific proofs of fit, (3) short ask.
NO: "Dear Hiring Manager", "I am writing to express", bullet points, fluff.

CANDIDATE:
{achievements}
- MSc CS Distinction (1:1), Trinity College Dublin
- Stamp 1G — works in Ireland, no sponsorship needed
- Skills matching this role: {', '.join(jd.get('required_skills', [])[:4])}

COMPANY CONTEXT:
Role: {job['title']} | Company: {job['company']}
Values/culture: {', '.join(jd.get('company_values', ['innovation', 'quality']))}
Key responsibilities: {'; '.join(jd.get('key_responsibilities', [])[:2])}

Return plain text only — no JSON wrapper, no headers."""

    msg = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text.strip()

# ─────────────────────────────────────────────────────────────────────────────
# STEP 6 — SAVE PACKAGE + QUEUE AGENT 3
# ─────────────────────────────────────────────────────────────────────────────
def save_package(job: dict, cover_letter: str, tailored: dict,
                 pdf_path: Path | None, patched_tex: str):
    slug = f"{job['company'].lower().replace(' ','-')[:20]}_{job['id']}"
    out  = Path(f"outputs/applications/{slug}")
    out.mkdir(parents=True, exist_ok=True)

    # Cover letter
    (out / "cover_letter.txt").write_text(cover_letter)

    # Patched .tex (always saved even if PDF failed)
    (out / "resume_tailored.tex").write_text(patched_tex, encoding="utf-8")

    # PDF path for Agent 3
    pdf_str = str(pdf_path) if pdf_path else ""

    package = {
        **job,
        "cover_letter":      cover_letter,
        "tailored_summary":  tailored.get("tailored_summary", ""),
        "resume_pdf":        pdf_str,
        "resume_tex":        str(out / "resume_tailored.tex"),
        "status":            "ready_to_apply",
        "prepared_at":       datetime.now().isoformat(),
    }
    (out / "application.json").write_text(json.dumps(package, indent=2))

    # Queue for Agent 3
    with open("outputs/agent3_queue.jsonl", "a") as f:
        f.write(json.dumps(package) + "\n")

    log.info(f"Package saved → {out}")
    return str(out)

# ─────────────────────────────────────────────────────────────────────────────
# MAIN PROCESSOR
# ─────────────────────────────────────────────────────────────────────────────
async def process_job(job: dict):
    log.info(f"▶ Writing application: {job['title']} @ {job['company']}")
    try:
        jd       = analyse_jd(job)
        log.info(f"  JD analysed — focus: {jd.get('focus')} | keywords: {jd.get('ats_keywords',[])[:3]}")

        tailored = tailor_resume_content(job, jd)
        log.info("  Resume content tailored")

        patched_tex = patch_tex(tailored, job)
        log.info("  .tex patched")

        slug = f"{job['company'].lower().replace(' ','-')[:20]}_{job['id']}"
        out  = Path(f"outputs/applications/{slug}")
        out.mkdir(parents=True, exist_ok=True)

        pdf_path = compile_tex(patched_tex, out, slug) if LATEX_CMD else None
        if not LATEX_CMD:
            log.warning("  xelatex not found — .tex saved, PDF skipped")

        cover_letter = generate_cover_letter(job, jd)
        log.info("  Cover letter generated")

        save_package(job, cover_letter, tailored, pdf_path, patched_tex)
        log.info(f"✅ Done: {job['title']} @ {job['company']}")

    except Exception as e:
        log.error(f"❌ Failed [{job['title']} @ {job['company']}]: {e}", exc_info=True)

# ─────────────────────────────────────────────────────────────────────────────
# QUEUE RUNNER
# ─────────────────────────────────────────────────────────────────────────────
async def run_from_queue():
    check_setup()
    queue_path = Path("outputs/agent2_queue.jsonl")
    if not queue_path.exists() or not queue_path.read_text().strip():
        log.info("No jobs in queue."); return

    lines = queue_path.read_text().strip().splitlines()
    log.info(f"Processing {len(lines)} jobs from queue...")
    for line in lines:
        job = json.loads(line)
        await process_job(job)
        await asyncio.sleep(2)

    queue_path.write_text("")
    log.info(f"Queue drained: {len(lines)} jobs processed")

if __name__ == "__main__":
    asyncio.run(run_from_queue())
