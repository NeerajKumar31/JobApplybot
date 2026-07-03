import re

from loguru import logger

from src.llm.ollama_client import OllamaClient
from src.models.job import Job

SYSTEM_PROMPT = """\
You are an expert ATS resume writer. Your task is to tailor a candidate's resume
to a specific job description, improving keyword alignment without fabricating
experience or skills. Always return valid Markdown. Be concise and professional.\
"""

REWRITE_PROMPT_TEMPLATE = """\
## Job Description
{job_description}

## Current Resume
{resume_content}

## Instructions
1. Extract the 15 most important ATS keywords/skills from the job description above.
2. Rewrite the resume's Summary and Work Experience bullet points to naturally
   incorporate those keywords, matching the language of the job posting.
3. Do NOT fabricate skills, companies, dates, or achievements that are not already
   present in the resume — only rephrase and reframe existing content.
4. Keep all section headings (Summary, Experience, Education, Skills, etc.) intact.
5. Output format:
   - First: a comment block listing the extracted keywords, e.g.:
     <!-- ATS_KEYWORDS: Python, REST APIs, microservices, ... -->
   - Then: the full rewritten resume in Markdown.

Return ONLY the Markdown. No preamble, no explanations.\
"""

KEYWORD_COMMENT_RE = re.compile(r"<!--\s*ATS_KEYWORDS:\s*(.+?)\s*-->", re.IGNORECASE)


class ResumeRewriter:
    """Uses an Ollama LLM to tailor a base resume to a specific job posting."""

    def __init__(self, client: OllamaClient) -> None:
        self._client = client

    async def rewrite(self, job: Job, base_resume: str) -> tuple[str, list[str]]:
        """Return (tailored_resume_markdown, extracted_keywords).

        The tailored resume is the base resume rewritten to match the job's
        language and required keywords, suitable for ATS screening.
        """
        logger.info(f"Rewriting resume for: {job.title} @ {job.company}")

        prompt = REWRITE_PROMPT_TEMPLATE.format(
            job_description=job.description[:4000],  # keep within context window
            resume_content=base_resume,
        )

        raw_output = await self._client.generate(prompt=prompt, system=SYSTEM_PROMPT)
        tailored_resume, keywords = self._parse_output(raw_output)

        logger.info(f"Keywords extracted ({len(keywords)}): {', '.join(keywords[:8])}…")
        return tailored_resume, keywords

    def _parse_output(self, raw: str) -> tuple[str, list[str]]:
        """Extract keyword list from the comment block and return clean resume text."""
        keywords: list[str] = []

        match = KEYWORD_COMMENT_RE.search(raw)
        if match:
            keywords = [kw.strip() for kw in match.group(1).split(",") if kw.strip()]

        # Remove the comment line so the PDF contains only resume content
        resume_md = KEYWORD_COMMENT_RE.sub("", raw).strip()
        return resume_md, keywords
