import tempfile
from pathlib import Path

import pytest

from src.utils.pdf_generator import generate_resume_pdf, _strip_markdown_inline


# ── PDF creation ───────────────────────────────────────────────────────────────

def test_pdf_file_is_created():
    md = "# John Doe\n## Summary\nGreat engineer.\n## Skills\n- Python\n- Docker"
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "resume.pdf"
        result = generate_resume_pdf(md, out)
        assert result.exists()
        assert result.stat().st_size > 0


def test_pdf_created_in_nested_directory():
    md = "# Test\n## Section\nContent here."
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "sub" / "nested" / "resume.pdf"
        generate_resume_pdf(md, out)
        assert out.exists()


def test_pdf_returns_output_path():
    md = "# Jane\n- Bullet point"
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "out.pdf"
        returned = generate_resume_pdf(md, out)
        assert returned == out


# ── Markdown inline stripping ──────────────────────────────────────────────────

def test_strip_bold():
    assert _strip_markdown_inline("**bold text**") == "bold text"


def test_strip_italic():
    assert _strip_markdown_inline("*italic text*") == "italic text"


def test_strip_code():
    assert _strip_markdown_inline("`code snippet`") == "code snippet"


def test_strip_link():
    assert _strip_markdown_inline("[label](https://example.com)") == "label"


def test_strip_mixed():
    result = _strip_markdown_inline("**Python** and [FastAPI](https://fastapi.tiangolo.com)")
    assert result == "Python and FastAPI"
