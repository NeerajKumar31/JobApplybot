import pytest

from src.llm.resume_rewriter import ResumeRewriter


def test_parse_output_extracts_keywords():
    rewriter = ResumeRewriter(client=None)  # type: ignore[arg-type]
    raw = (
        "<!-- ATS_KEYWORDS: Python, Docker, REST APIs, microservices -->\n"
        "# Jane Doe\n\n## Summary\nExperienced engineer...\n"
    )
    resume, keywords = rewriter._parse_output(raw)

    assert "Python" in keywords
    assert "Docker" in keywords
    assert "REST APIs" in keywords
    assert "microservices" in keywords
    assert len(keywords) == 4


def test_parse_output_strips_comment_from_resume():
    rewriter = ResumeRewriter(client=None)  # type: ignore[arg-type]
    raw = "<!-- ATS_KEYWORDS: Python -->\n# Jane Doe\nSummary here"
    resume, _ = rewriter._parse_output(raw)
    assert "<!-- ATS_KEYWORDS" not in resume
    assert "# Jane Doe" in resume


def test_parse_output_no_keyword_comment():
    rewriter = ResumeRewriter(client=None)  # type: ignore[arg-type]
    raw = "# Jane Doe\nNo keyword comment present."
    resume, keywords = rewriter._parse_output(raw)
    assert keywords == []
    assert resume == raw


def test_parse_output_case_insensitive_comment():
    rewriter = ResumeRewriter(client=None)  # type: ignore[arg-type]
    raw = "<!-- ats_keywords: Go, Kubernetes -->\n# Resume"
    _, keywords = rewriter._parse_output(raw)
    assert "Go" in keywords
    assert "Kubernetes" in keywords


def test_parse_output_trims_whitespace_from_keywords():
    rewriter = ResumeRewriter(client=None)  # type: ignore[arg-type]
    raw = "<!-- ATS_KEYWORDS:  Python ,  FastAPI ,  SQL  -->\n# Resume"
    _, keywords = rewriter._parse_output(raw)
    assert keywords == ["Python", "FastAPI", "SQL"]
