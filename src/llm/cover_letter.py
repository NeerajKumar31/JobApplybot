from loguru import logger

from src.llm.ollama_client import OllamaClient
from src.models.applicant import Applicant
from src.models.job import Job

COVER_LETTER_SYSTEM = """\
You are an expert career coach who writes concise, tailored cover letters.
Always write in first person. Keep it professional, specific, and under 250 words.\
"""

COVER_LETTER_PROMPT = """\
Write a cover letter body for the following job application.

Job Title: {title}
Company: {company}
Location: {location}

Job Description (excerpt):
{description}

Applicant Name: {name}

Guidelines:
- 3 short paragraphs:
    1. Why you're excited about THIS role at THIS company (reference something specific from the JD).
    2. Your top 2–3 relevant strengths / achievements that directly match the job requirements.
    3. A confident closing with a call to action.
- Do NOT include "Dear Hiring Manager", a date, or any header/footer.
- Do NOT fabricate specific companies, numbers, or technologies not implied by the applicant's background.
- Return ONLY the plain text body. No markdown, no labels.\
"""


class CoverLetterGenerator:
    """Generates a tailored cover letter body using the local Ollama LLM."""

    def __init__(self, client: OllamaClient) -> None:
        self._client = client

    async def generate(self, job: Job, applicant: Applicant) -> str:
        """Return a plain-text cover letter body for the given job and applicant.

        The output is suitable for pasting directly into a cover letter textarea.
        """
        logger.info(f"Generating cover letter for: {job.title} @ {job.company}")

        prompt = COVER_LETTER_PROMPT.format(
            title=job.title,
            company=job.company,
            location=job.location,
            description=job.description[:2000],
            name=applicant.name,
        )

        cover_letter = await self._client.generate(prompt=prompt, system=COVER_LETTER_SYSTEM)
        cover_letter = cover_letter.strip()

        logger.debug(f"Cover letter generated ({len(cover_letter)} chars)")
        return cover_letter
