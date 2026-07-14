"""JobApplybot — LinkedIn + Indeed + Naukri simultaneous Easy Apply with LLM resume tailoring.

Usage:
    python main.py                          # run all enabled portals simultaneously
    python main.py --dry-run                # search + rewrite, no Submit
    python main.py --max-jobs 5             # override max jobs per portal
    python main.py --query "Backend Engineer"
    python main.py --remote                 # filter to remote jobs only
    python main.py --headed                 # show browser windows
    python main.py --status                 # show application summary and exit
    python main.py --linkedin-only          # run LinkedIn only
    python main.py --indeed-only            # run Indeed only
    python main.py --naukri-only            # run Naukri only
"""

import argparse
import asyncio
import random
import sys
from pathlib import Path

from loguru import logger
from playwright.async_api import async_playwright

from config import Settings
from src.indeed import IndeedAuth, IndeedEasyApplyHandler, IndeedJobSearcher
from src.indeed.easy_apply import AlreadyAppliedError as IndeedAlreadyAppliedError
from src.linkedin import EasyApplyHandler, JobSearcher, LinkedInAuth
from src.linkedin.easy_apply import AlreadyAppliedError as LinkedInAlreadyAppliedError
from src.naukri import NaukriAuth, NaukriEasyApplyHandler, NaukriJobSearcher
from src.naukri import NaukriAlreadyAppliedError
from src.llm import CoverLetterGenerator, OllamaClient, ResumeRewriter
from src.models.applicant import Applicant
from src.models.job import Job, JobStatus
from src.utils import AppliedJobsTracker, generate_resume_pdf, setup_logger


# ── Shared per-job orchestration ───────────────────────────────────────────────


async def process_job(
    job: Job,
    source: str,
    base_resume: str,
    rewriter: ResumeRewriter | None,
    cover_gen: CoverLetterGenerator | None,
    easy_apply,
    applicant: Applicant,
    tracker: AppliedJobsTracker,
    data_dir: Path,
    dry_run: bool,
    already_applied_error_type: type,
) -> None:
    """Rewrite resume → generate PDF → (cover letter) → Easy Apply → record.

    `source` is 'linkedin', 'indeed', or 'naukri' and is passed to the tracker
    so IDs from different portals never collide in applied_jobs.json.

    When `rewriter` is None (Ollama offline), the base resume is used as-is.
    """
    logger.info(f"[{source}] Processing: {job.title} @ {job.company}")

    if rewriter is not None:
        try:
            tailored_md, keywords = await rewriter.rewrite(job, base_resume)
            job.keywords_extracted = keywords
        except Exception as e:
            logger.warning(f"[{source}] Resume rewrite failed [{job.job_id}]: {e} — using base resume")
            tailored_md = base_resume
    else:
        logger.debug(f"[{source}] LLM offline — using base resume for {job.job_id}")
        tailored_md = base_resume

    pdf_path = data_dir / "resumes" / f"{source}_{job.job_id}.pdf"
    try:
        generate_resume_pdf(tailored_md, pdf_path)
    except Exception as e:
        logger.error(f"[{source}] PDF generation failed [{job.job_id}]: {e}")
        job.mark_failed(f"pdf_gen: {e}")
        tracker.record(job, source=source)
        return

    cover_letter = ""
    if cover_gen is not None:
        try:
            cover_letter = await cover_gen.generate(job, applicant)
        except Exception as e:
            logger.warning(f"[{source}] Cover letter failed [{job.job_id}]: {e} — continuing")

    if dry_run:
        logger.info(f"[{source}] [DRY RUN] Would apply to {job.title} using {pdf_path.name}")
        job.mark_skipped("dry_run")
        tracker.record(job, source=source)
        return

    try:
        await easy_apply.apply(job, pdf_path, cover_letter=cover_letter)
        job.mark_applied(str(pdf_path))
        logger.success(f"[{source}] Applied to {job.title} @ {job.company}")
    except already_applied_error_type:
        logger.info(f"[{source}] Already applied to {job.job_id} — skipping")
        job.mark_skipped("already_applied")
    except Exception as e:
        err_str = str(e)
        # Page/browser closed mid-apply (e.g. smartapply tab closed by Indeed)
        if "closed" in err_str.lower() and (
            "page" in err_str.lower() or "browser" in err_str.lower()
            or "target" in err_str.lower()
        ):
            logger.warning(
                f"[{source}] Apply tab closed unexpectedly for {job.job_id} — "
                "marking failed so it will be retried next run"
            )
        else:
            logger.error(f"[{source}] Easy Apply failed [{job.job_id}]: {e}")
        job.mark_failed(f"easy_apply: {e}")

    tracker.record(job, source=source)


# ── LinkedIn pipeline ──────────────────────────────────────────────────────────


async def run_linkedin(
    settings: Settings,
    ollama: OllamaClient,
    tracker: AppliedJobsTracker,
    applicant: Applicant,
    base_resume: str,
    dry_run: bool,
) -> int:
    """Full LinkedIn pipeline. Returns number of applications submitted."""
    logger.info("LinkedIn: pipeline starting")
    applied_count = 0

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

        auth = LinkedInAuth(page, context)
        await auth.login_with_session(
            settings.linkedin_email,
            settings.linkedin_password,
            settings.data_dir / "linkedin_cookies.json",
        )

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
        logger.info(f"LinkedIn: found {len(jobs)} Easy Apply jobs")

        if not jobs:
            logger.warning("LinkedIn: no jobs found")
            await browser.close()
            return 0

        rewriter   = ResumeRewriter(ollama) if ollama is not None else None
        cover_gen  = CoverLetterGenerator(ollama) if (ollama is not None and settings.generate_cover_letter) else None
        ea_handler = EasyApplyHandler(page, applicant, llm=ollama)

        for job in jobs:
            if tracker.already_processed(job.job_id, source="linkedin"):
                logger.info(f"LinkedIn: skipping already-processed job {job.job_id}")
                continue
            if not job.is_easy_apply:
                job.mark_skipped("not_easy_apply")
                tracker.record(job, source="linkedin")
                continue

            await process_job(
                job=job,
                source="linkedin",
                base_resume=base_resume,
                rewriter=rewriter,
                cover_gen=cover_gen,
                easy_apply=ea_handler,
                applicant=applicant,
                tracker=tracker,
                data_dir=settings.data_dir,
                dry_run=dry_run,
                already_applied_error_type=LinkedInAlreadyAppliedError,
            )
            if job.status == JobStatus.APPLIED:
                applied_count += 1

            await asyncio.sleep(random.uniform(8, 18))

        await browser.close()

    logger.info(f"LinkedIn: pipeline done — applied={applied_count}")
    return applied_count


# ── Indeed pipeline ────────────────────────────────────────────────────────────


async def run_indeed(
    settings: Settings,
    ollama: OllamaClient,
    tracker: AppliedJobsTracker,
    applicant: Applicant,
    base_resume: str,
    dry_run: bool,
) -> int:
    """Full Indeed pipeline. Returns number of applications submitted."""
    logger.info("Indeed: pipeline starting")
    applied_count = 0

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

        try:
            auth = IndeedAuth(page, context)
            await auth.login_with_session(
                settings.indeed_email,
                settings.indeed_password,
                settings.data_dir / "indeed_cookies.json",
            )
        except Exception as e:
            logger.error(f"Indeed: login failed — {e}")
            await browser.close()
            return 0

        searcher = IndeedJobSearcher(page)
        jobs = await searcher.search(
            query=settings.search_query,
            location=settings.search_location,
            max_jobs=settings.max_jobs,
            remote_filter=settings.remote_filter,
            date_posted=settings.date_posted,
            job_type=settings.job_type,
        )
        logger.info(f"Indeed: found {len(jobs)} Easy Apply jobs")

        if not jobs:
            logger.warning("Indeed: no jobs found")
            await browser.close()
            return 0

        rewriter   = ResumeRewriter(ollama) if ollama is not None else None
        cover_gen  = CoverLetterGenerator(ollama) if (ollama is not None and settings.generate_cover_letter) else None
        ea_handler = IndeedEasyApplyHandler(page, applicant, llm=ollama, context=context)

        for job in jobs:
            if tracker.already_processed(job.job_id, source="indeed"):
                logger.info(f"Indeed: skipping already-processed job {job.job_id}")
                continue

            await process_job(
                job=job,
                source="indeed",
                base_resume=base_resume,
                rewriter=rewriter,
                cover_gen=cover_gen,
                easy_apply=ea_handler,
                applicant=applicant,
                tracker=tracker,
                data_dir=settings.data_dir,
                dry_run=dry_run,
                already_applied_error_type=IndeedAlreadyAppliedError,
            )
            if job.status == JobStatus.APPLIED:
                applied_count += 1

            await asyncio.sleep(random.uniform(8, 18))

        await browser.close()

    logger.info(f"Indeed: pipeline done — applied={applied_count}")
    return applied_count


# ── Naukri pipeline ────────────────────────────────────────────────────────────


async def run_naukri(
    settings: Settings,
    ollama: OllamaClient,
    tracker: AppliedJobsTracker,
    applicant: Applicant,
    base_resume: str,
    dry_run: bool,
) -> int:
    """Full Naukri pipeline. Returns number of applications submitted."""
    logger.info("Naukri: pipeline starting")
    applied_count = 0

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

        try:
            auth = NaukriAuth(page, context)
            await auth.login_with_session(
                settings.naukri_email,
                settings.naukri_password,
                settings.data_dir / "naukri_cookies.json",
            )
        except Exception as e:
            logger.error(f"Naukri: login failed — {e}")
            await browser.close()
            return 0

        searcher   = NaukriJobSearcher(page)
        queries    = settings.naukri_query_list
        seen_ids: set[str] = set()
        all_jobs:  list[Job] = []

        jobs_per_query = max(1, settings.max_jobs // len(queries))

        for query in queries:
            logger.info(f"Naukri: searching '{query}' in {settings.search_location}")
            found = await searcher.search(
                query=query,
                location=settings.search_location,
                max_jobs=jobs_per_query,
                date_posted=settings.date_posted,
            )
            new = [j for j in found if j.job_id not in seen_ids]
            seen_ids.update(j.job_id for j in new)
            all_jobs.extend(new)
            logger.info(f"Naukri: '{query}' → {len(new)} new jobs (total {len(all_jobs)})")

        # Relevance filter: keep only jobs whose title matches at least one keyword
        _RELEVANT_KEYWORDS = [kw.lower() for kw in queries] + [
            "mobile", "android", "ios", "react native", "ionic",
            "angular", "frontend", "front-end", "front end",
            "flutter", "cordova", "hybrid",
        ]

        def _is_relevant(job: Job) -> bool:
            title = job.title.lower()
            return any(kw in title for kw in _RELEVANT_KEYWORDS)

        jobs = [j for j in all_jobs if _is_relevant(j)]
        skipped = len(all_jobs) - len(jobs)
        if skipped:
            logger.info(f"Naukri: filtered out {skipped} irrelevant jobs — {len(jobs)} remain")

        logger.info(f"Naukri: found {len(jobs)} relevant jobs")

        if not jobs:
            logger.warning("Naukri: no relevant jobs found")
            await browser.close()
            return 0

        rewriter   = ResumeRewriter(ollama) if ollama is not None else None
        cover_gen  = CoverLetterGenerator(ollama) if (ollama is not None and settings.generate_cover_letter) else None
        ea_handler = NaukriEasyApplyHandler(page, applicant, llm=ollama)

        for job in jobs:
            if tracker.already_processed(job.job_id, source="naukri"):
                logger.info(f"Naukri: skipping already-processed job {job.job_id}")
                continue

            await process_job(
                job=job,
                source="naukri",
                base_resume=base_resume,
                rewriter=rewriter,
                cover_gen=cover_gen,
                easy_apply=ea_handler,
                applicant=applicant,
                tracker=tracker,
                data_dir=settings.data_dir,
                dry_run=dry_run,
                already_applied_error_type=NaukriAlreadyAppliedError,
            )
            if job.status == JobStatus.APPLIED:
                applied_count += 1

            await asyncio.sleep(random.uniform(8, 18))

        await browser.close()

    logger.info(f"Naukri: pipeline done — applied={applied_count}")
    return applied_count


# ── Orchestrator: run portals simultaneously ───────────────────────────────────


async def run(
    settings: Settings,
    dry_run: bool,
    run_li: bool = True,
    run_in: bool = True,
    run_nk: bool = True,
) -> None:
    """Launch LinkedIn, Indeed, and/or Naukri pipelines simultaneously."""
    setup_logger(settings.data_dir)
    logger.info(
        f"JobApplybot starting | query='{settings.search_query}' "
        f"location='{settings.search_location}' dry_run={dry_run} "
        f"linkedin={run_li and settings.linkedin_enabled} "
        f"indeed={run_in and settings.indeed_enabled} "
        f"naukri={run_nk and settings.naukri_enabled}"
    )

    base_resume = settings.resume_base_path.read_text(encoding="utf-8")
    tracker     = AppliedJobsTracker(settings.data_dir)
    applicant   = Applicant(
        name=settings.applicant_name,
        email=settings.applicant_email,
        phone=settings.applicant_phone,
        linkedin_url=settings.applicant_linkedin,
    )

    async with OllamaClient(settings.ollama_base_url, settings.ollama_model) as ollama:
        ollama_ok = await ollama.check_health()
        if not ollama_ok:
            logger.warning(
                f"Ollama is not reachable ({settings.ollama_base_url}). "
                "Continuing WITHOUT LLM — base resume will be used as-is, "
                "no resume tailoring or cover letters. "
                "To enable LLM: run `ollama serve` and pull your model."
            )
            ollama = None  # pipelines check for None and skip rewriting

        portal_tasks: list[tuple[str, any]] = []
        if run_li and settings.linkedin_enabled:
            portal_tasks.append(("LinkedIn", run_linkedin(settings, ollama, tracker, applicant, base_resume, dry_run)))
        if run_in and settings.indeed_enabled:
            portal_tasks.append(("Indeed",   run_indeed(settings, ollama, tracker, applicant, base_resume, dry_run)))
        if run_nk and settings.naukri_enabled:
            portal_tasks.append(("Naukri",   run_naukri(settings, ollama, tracker, applicant, base_resume, dry_run)))

        if not portal_tasks:
            logger.warning(
                "No portals enabled — set LINKEDIN_ENABLED, INDEED_ENABLED, "
                "or NAUKRI_ENABLED=true in .env"
            )
            return

        names, coros = zip(*portal_tasks)
        results = await asyncio.gather(*coros, return_exceptions=True)

        total_applied = 0
        for name, result in zip(names, results):
            if isinstance(result, Exception):
                logger.error(f"{name} pipeline crashed: {result}")
            else:
                total_applied += result

        summary = tracker.summary()
        summary_str = " | ".join(
            f"{k}={v}" for k, v in summary.items() if k != "applied"
        )
        logger.info(f"Run complete | applied={total_applied} | {summary_str}")


# ── Status report ──────────────────────────────────────────────────────────────


def show_status() -> None:
    """Print a formatted summary of all tracked job applications."""
    from collections import Counter
    import json

    data_file = Path("data/applied_jobs.json")
    if not data_file.exists():
        print("No applications recorded yet — run the bot first.")
        return

    records: dict = json.loads(data_file.read_text(encoding="utf-8"))
    if not records:
        print("Tracker file exists but contains no records.")
        return

    counts  = Counter(r["status"] for r in records.values())
    by_src  = Counter(r.get("source", "linkedin") for r in records.values())
    total   = len(records)

    print()
    print("=" * 52)
    print("  JobApplybot — Application Status")
    print("=" * 52)
    print(f"  Total tracked : {total}")
    print(f"  LinkedIn      : {by_src.get('linkedin', 0)}")
    print(f"  Indeed        : {by_src.get('indeed', 0)}")
    print(f"  Naukri        : {by_src.get('naukri', 0)}")
    print()
    icons = {"applied": "✅", "failed": "❌", "skipped": "⏭ ", "found": "🔍"}
    for status in ["applied", "failed", "skipped", "found"]:
        if counts.get(status):
            print(f"  {icons.get(status, '  ')}  {status:<10}  {counts[status]}")
    print()

    applied = [r for r in records.values() if r["status"] == "applied"]
    if applied:
        print(f"  Applied jobs ({len(applied)}):")
        print("  " + "-" * 48)
        for r in sorted(applied, key=lambda x: x.get("applied_at") or ""):
            date  = (r.get("applied_at") or "")[:10]
            src   = r.get("source", "li")[:2].upper()
            title = r.get("title", "Unknown")[:32]
            co    = r.get("company", "Unknown")[:22]
            print(f"  {date}  [{src}]  {title:<32}  {co}")
    else:
        print("  No successful applications yet.")

    failed = [r for r in records.values() if r["status"] == "failed"]
    if failed:
        print()
        print(f"  Failed jobs ({len(failed)}):")
        print("  " + "-" * 48)
        for r in failed:
            src   = r.get("source", "li")[:2].upper()
            title = r.get("title", "Unknown")[:32]
            err   = (r.get("error") or "")[:35]
            print(f"  [{src}]  {title:<32}  {err}")

    print("=" * 52)
    print()


# ── CLI ────────────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="JobApplybot — LinkedIn + Indeed Easy Apply with LLM resume tailoring"
    )
    parser.add_argument("--dry-run",       action="store_true", help="No Submit — test mode")
    parser.add_argument("--query",         type=str,            help="Override SEARCH_QUERY")
    parser.add_argument("--location",      type=str,            help="Override SEARCH_LOCATION")
    parser.add_argument("--max-jobs",      type=int,            help="Override MAX_JOBS per portal")
    parser.add_argument("--headed",        action="store_true", help="Show browser windows")
    parser.add_argument("--remote",        action="store_true", help="Remote jobs only")
    parser.add_argument("--cover-letter",  action="store_true", help="Generate cover letters")
    parser.add_argument("--status",        action="store_true", help="Show status and exit")
    parser.add_argument("--linkedin-only", action="store_true", help="Run LinkedIn only")
    parser.add_argument("--indeed-only",   action="store_true", help="Run Indeed only")
    parser.add_argument("--naukri-only",   action="store_true", help="Run Naukri only")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if args.status:
        show_status()
        raise SystemExit(0)

    cfg = Settings()

    if args.query:        cfg.search_query    = args.query
    if args.location:     cfg.search_location = args.location
    if args.max_jobs:     cfg.max_jobs        = args.max_jobs
    if args.headed:       cfg.headless        = False
    if args.remote:       cfg.remote_filter   = "remote"
    if args.cover_letter: cfg.generate_cover_letter = True

    only_one = args.linkedin_only or args.indeed_only or args.naukri_only
    run_li = args.linkedin_only or not only_one
    run_in = args.indeed_only   or not only_one
    run_nk = args.naukri_only   or not only_one

    asyncio.run(run(cfg, dry_run=args.dry_run, run_li=run_li, run_in=run_in, run_nk=run_nk))
