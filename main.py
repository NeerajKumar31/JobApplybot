"""JobApplybot — Automated LinkedIn Easy Apply with LLM resume tailoring.

Usage:
    python main.py                          # run with settings from .env
    python main.py --dry-run                # search + rewrite resumes, no Submit
    python main.py --max-jobs 5             # override max jobs for this run
    python main.py --query "Backend Engineer"
    python main.py --remote                 # filter to remote jobs only
    python main.py --dry-run --cover-letter # also generate cover letters
"""

import argparse
import asyncio
import random
import sys
from pathlib import Path

from loguru import logger
from playwright.async_api import async_playwright

from config import Settings
from src.linkedin import EasyApplyHandler, JobSearcher, LinkedInAuth
from src.linkedin.easy_apply import AlreadyAppliedError
from src.llm import CoverLetterGenerator, OllamaClient, ResumeRewriter
from src.models.applicant import Applicant
from src.models.job import Job, JobStatus
from src.utils import AppliedJobsTracker, generate_resume_pdf, setup_logger


# ── Per-job orchestration ──────────────────────────────────────────────────────


async def process_job(
    job: Job,
    base_resume: str,
    rewriter: ResumeRewriter,
    cover_gen: CoverLetterGenerator | None,
    easy_apply: EasyApplyHandler,
    applicant: Applicant,
    tracker: AppliedJobsTracker,
    data_dir: Path,
    dry_run: bool,
) -> None:
    """Handle a single job end-to-end: rewrite → PDF → (cover letter) → Easy Apply.

    Subtasks:
    1. Rewrite the base resume to match the job's ATS keywords via Ollama.
    2. Generate a tailored PDF from the rewritten Markdown.
    3. Optionally generate a cover letter if enabled in config.
    4. Click Easy Apply and drive the multi-step form to submission.
    5. Record the outcome in the tracker.
    """
    logger.info(f"Processing: {job.title} @ {job.company}")

    # ── Subtask 1: Tailor resume ───────────────────────────────────────────────
    try:
        tailored_md, keywords = await rewriter.rewrite(job, base_resume)
        job.keywords_extracted = keywords
    except Exception as e:
        logger.error(f"Resume rewrite failed [{job.job_id}]: {e}")
        job.mark_failed(f"resume_rewrite: {e}")
        tracker.record(job)
        return

    # ── Subtask 2: Generate PDF ────────────────────────────────────────────────
    pdf_path = data_dir / "resumes" / f"{job.job_id}.pdf"
    try:
        generate_resume_pdf(tailored_md, pdf_path)
    except Exception as e:
        logger.error(f"PDF generation failed [{job.job_id}]: {e}")
        job.mark_failed(f"pdf_gen: {e}")
        tracker.record(job)
        return

    # ── Subtask 3: Cover letter (optional) ────────────────────────────────────
    cover_letter = ""
    if cover_gen is not None:
        try:
            cover_letter = await cover_gen.generate(job, applicant)
        except Exception as e:
            logger.warning(f"Cover letter generation failed [{job.job_id}]: {e} — continuing")

    # ── Subtask 4: Easy Apply ──────────────────────────────────────────────────
    if dry_run:
        logger.info(f"[DRY RUN] Would apply to {job.title} using {pdf_path.name}")
        if cover_letter:
            logger.info(f"[DRY RUN] Cover letter preview: {cover_letter[:120]}…")
        job.mark_skipped("dry_run")
        tracker.record(job)
        return

    try:
        await easy_apply.apply(job, pdf_path, cover_letter=cover_letter)
        job.mark_applied(str(pdf_path))
        logger.success(f"Applied to {job.title} @ {job.company}")
    except AlreadyAppliedError:
        logger.info(f"Already applied to {job.job_id} — marking skipped")
        job.mark_skipped("already_applied")
    except Exception as e:
        logger.error(f"Easy Apply failed [{job.job_id}]: {e}")
        job.mark_failed(f"easy_apply: {e}")

    tracker.record(job)


# ── Main pipeline ──────────────────────────────────────────────────────────────


async def run(settings: Settings, dry_run: bool) -> None:
    """Full pipeline: session restore/login → search → rewrite → apply."""

    setup_logger(settings.data_dir)
    logger.info(
        f"JobApplybot starting | query='{settings.search_query}' "
        f"location='{settings.search_location}' dry_run={dry_run}"
    )

    base_resume = settings.resume_base_path.read_text(encoding="utf-8")
    tracker = AppliedJobsTracker(settings.data_dir)
    applicant = Applicant(
        name=settings.applicant_name,
        email=settings.applicant_email,
        phone=settings.applicant_phone,
        linkedin_url=settings.applicant_linkedin,
    )

    async with OllamaClient(settings.ollama_base_url, settings.ollama_model) as ollama:
        if not await ollama.check_health():
            logger.error(
                f"Ollama is not ready. Run `ollama serve` and ensure "
                f"model '{settings.ollama_model}' is pulled (`ollama pull {settings.ollama_model}`)."
            )
            sys.exit(1)

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=settings.headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                locale="en-US",
            )
            page = await context.new_page()

            # ── Step 1: Login (with session restore) ──────────────────────────
            auth = LinkedInAuth(page, context)
            await auth.login_with_session(
                settings.linkedin_email,
                settings.linkedin_password,
                settings.cookie_path,
            )

            # ── Step 2: Search with active filters ────────────────────────────
            searcher = JobSearcher(page)
            jobs = await searcher.search(
                query=settings.search_query,
                location=settings.search_location,
                max_jobs=settings.max_jobs,
                remote_filter=settings.remote_filter,
                date_posted=settings.date_posted,
                experience_level=settings.experience_level,
                job_type=settings.job_type,
            )
            logger.info(f"Found {len(jobs)} Easy Apply jobs")

            if not jobs:
                logger.warning("No jobs found — check your search query or filter settings")
                await browser.close()
                return

            # ── Step 3: Build helpers ──────────────────────────────────────────
            rewriter = ResumeRewriter(ollama)
            cover_gen = CoverLetterGenerator(ollama) if settings.generate_cover_letter else None
            easy_apply_handler = EasyApplyHandler(page, applicant, llm=ollama)

            # ── Step 4: Apply to each job ──────────────────────────────────────
            applied_count = 0
            for job in jobs:
                if tracker.already_processed(job.job_id):
                    logger.info(f"Skipping already-processed job: {job.job_id}")
                    continue

                if not job.is_easy_apply:
                    job.mark_skipped("not_easy_apply")
                    tracker.record(job)
                    continue

                await process_job(
                    job=job,
                    base_resume=base_resume,
                    rewriter=rewriter,
                    cover_gen=cover_gen,
                    easy_apply=easy_apply_handler,
                    applicant=applicant,
                    tracker=tracker,
                    data_dir=settings.data_dir,
                    dry_run=dry_run,
                )

                if job.status == JobStatus.APPLIED:
                    applied_count += 1

                # Polite delay between applications
                await asyncio.sleep(random.uniform(8, 18))

            await browser.close()

    # ── Final summary ──────────────────────────────────────────────────────────
    summary = tracker.summary()
    summary_str = " | ".join(
        f"{k}={v}" for k, v in summary.items() if k != "applied"
    )
    logger.info(f"Run complete | applied={applied_count} | {summary_str}")


# ── Status report ──────────────────────────────────────────────────────────────


def show_status() -> None:
    """Print a formatted summary of all tracked job applications and exit."""
    from collections import Counter
    import json
    from pathlib import Path

    data_file = Path("data/applied_jobs.json")
    if not data_file.exists():
        print("No applications recorded yet — run the bot first.")
        return

    records: dict = json.loads(data_file.read_text(encoding="utf-8"))
    if not records:
        print("Tracker file exists but contains no records.")
        return

    counts = Counter(r["status"] for r in records.values())
    total  = len(records)

    # ── Summary block ──────────────────────────────────────────────────────────
    print()
    print("=" * 52)
    print("  JobApplybot — Application Status")
    print("=" * 52)
    print(f"  Total tracked : {total}")
    print()
    status_icons = {
        "applied": "✅",
        "failed":  "❌",
        "skipped": "⏭ ",
        "found":   "🔍",
    }
    for status in ["applied", "failed", "skipped", "found"]:
        if counts.get(status):
            icon = status_icons.get(status, "  ")
            print(f"  {icon}  {status:<10}  {counts[status]}")
    print()

    # ── Applied jobs detail ────────────────────────────────────────────────────
    applied = [r for r in records.values() if r["status"] == "applied"]
    if applied:
        print(f"  Applied jobs ({len(applied)}):")
        print("  " + "-" * 48)
        for r in sorted(applied, key=lambda x: x.get("applied_at") or ""):
            date  = (r.get("applied_at") or "")[:10]
            title = r.get("title", "Unknown")[:35]
            co    = r.get("company", "Unknown")[:25]
            print(f"  {date}  {title:<35}  {co}")
    else:
        print("  No successful applications yet.")

    # ── Failed jobs detail ─────────────────────────────────────────────────────
    failed = [r for r in records.values() if r["status"] == "failed"]
    if failed:
        print()
        print(f"  Failed jobs ({len(failed)}):")
        print("  " + "-" * 48)
        for r in failed:
            title = r.get("title", "Unknown")[:35]
            err   = (r.get("error") or "")[:40]
            print(f"  {title:<35}  {err}")

    print("=" * 52)
    print()


# ── CLI ────────────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="JobApplybot — LinkedIn Easy Apply automation with LLM resume tailoring"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Search + rewrite but do NOT click Submit")
    parser.add_argument("--query", type=str, help="Override SEARCH_QUERY from .env")
    parser.add_argument("--location", type=str, help="Override SEARCH_LOCATION from .env")
    parser.add_argument("--max-jobs", type=int, help="Override MAX_JOBS from .env")
    parser.add_argument("--headed", action="store_true",
                        help="Force browser to run in headed (visible) mode")
    parser.add_argument("--remote", action="store_true",
                        help="Filter to remote jobs only")
    parser.add_argument("--cover-letter", action="store_true",
                        help="Generate a tailored cover letter for each job")
    parser.add_argument("--status", action="store_true",
                        help="Show application status summary and exit")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if args.status:
        show_status()
        raise SystemExit(0)

    cfg = Settings()

    if args.query:
        cfg.search_query = args.query
    if args.location:
        cfg.search_location = args.location
    if args.max_jobs:
        cfg.max_jobs = args.max_jobs
    if args.headed:
        cfg.headless = False
    if args.remote:
        cfg.remote_filter = "remote"
    if args.cover_letter:
        cfg.generate_cover_letter = True

    asyncio.run(run(cfg, dry_run=args.dry_run))
