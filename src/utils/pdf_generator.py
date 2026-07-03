import re
import unicodedata
from pathlib import Path

from fpdf import FPDF
from loguru import logger

# Characters outside Latin-1 that commonly appear in resumes, mapped to safe ASCII
_UNICODE_REPLACEMENTS = str.maketrans({
    "\u2013": "-",    # en-dash
    "\u2014": "--",   # em-dash
    "\u2018": "'",    # left single quote
    "\u2019": "'",    # right single quote
    "\u201c": '"',    # left double quote
    "\u201d": '"',    # right double quote
    "\u2022": "-",    # bullet
    "\u2026": "...",  # ellipsis
    "\u00a0": " ",    # non-breaking space
})


def _safe(text: str) -> str:
    """Replace non-Latin-1 characters so fpdf's built-in Helvetica doesn't crash."""
    text = text.translate(_UNICODE_REPLACEMENTS)
    text = unicodedata.normalize("NFKD", text)
    return text.encode("latin-1", errors="ignore").decode("latin-1")


class ResumePDF(FPDF):
    """FPDF subclass with pre-configured fonts and helper methods for resume layout."""

    MARGIN = 15
    BODY_FONT_SIZE = 10
    HEADING_FONT_SIZE = 13

    def __init__(self) -> None:
        super().__init__()
        self.set_margins(self.MARGIN, self.MARGIN, self.MARGIN)
        self.set_auto_page_break(auto=True, margin=self.MARGIN)
        self.add_page()
        self.set_font("Helvetica", size=self.BODY_FONT_SIZE)

    def _reset_x(self) -> None:
        """Always reset the cursor to the left margin before writing.

        fpdf2 leaves the cursor at the end of the last rendered character after
        some operations (e.g. cell). Without resetting, multi_cell sees 0 available
        width and raises 'Not enough horizontal space'.
        """
        self.set_x(self.l_margin)

    def add_heading(self, text: str) -> None:
        self.ln(3)
        self._reset_x()
        self.set_font("Helvetica", "B", self.HEADING_FONT_SIZE)
        self.cell(0, 7, _safe(text).upper(), new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(100, 100, 100)
        self.line(self.MARGIN, self.get_y(), self.w - self.MARGIN, self.get_y())
        self.ln(2)
        self.set_font("Helvetica", size=self.BODY_FONT_SIZE)

    def add_subheading(self, text: str) -> None:
        self._reset_x()
        self.set_font("Helvetica", "B", self.BODY_FONT_SIZE)
        self.multi_cell(0, 6, _safe(text))
        self.set_font("Helvetica", size=self.BODY_FONT_SIZE)

    def add_body(self, text: str) -> None:
        self._reset_x()
        self.set_font("Helvetica", size=self.BODY_FONT_SIZE)
        self.multi_cell(0, 5, _safe(text))

    def add_bullet(self, text: str) -> None:
        self.set_x(self.MARGIN + 4)
        self.set_font("Helvetica", size=self.BODY_FONT_SIZE)
        self.multi_cell(0, 5, f"-  {_safe(text)}")


def generate_resume_pdf(markdown_content: str, output_path: Path) -> Path:
    """Convert a Markdown resume string to a PDF file at `output_path`.

    Supported Markdown elements:
    - # H1  → name / title block (centred, large)
    - ## H2 → section heading with underline
    - ### H3 → role/company subheading (bold body text)
    - - / * → bullet point
    - blank lines → vertical spacing
    - **bold**, *italic*, `code`, [links](url) → stripped to plain text
    """
    pdf = ResumePDF()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    for line in markdown_content.splitlines():
        stripped = line.strip()

        if not stripped:
            pdf.ln(2)
            continue

        if stripped.startswith("# "):
            pdf._reset_x()
            pdf.set_font("Helvetica", "B", 16)
            pdf.cell(0, 10, _safe(stripped[2:].strip()), new_x="LMARGIN", new_y="NEXT", align="C")
            pdf.set_font("Helvetica", size=ResumePDF.BODY_FONT_SIZE)

        elif stripped.startswith("## "):
            pdf.add_heading(stripped[3:].strip())

        elif stripped.startswith("### "):
            pdf.add_subheading(stripped[4:].strip())

        elif stripped.startswith(("- ", "* ")):
            pdf.add_bullet(_strip_markdown_inline(stripped[2:]))

        else:
            pdf.add_body(_strip_markdown_inline(stripped))

    pdf.output(str(output_path))
    logger.debug(f"Resume PDF written to {output_path}")
    return output_path


def _strip_markdown_inline(text: str) -> str:
    """Remove inline Markdown syntax (bold, italic, code, links) from a string."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)   # **bold**
    text = re.sub(r"\*(.+?)\*", r"\1", text)         # *italic*
    text = re.sub(r"`(.+?)`", r"\1", text)            # `code`
    text = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", text)  # [link](url)
    return text
