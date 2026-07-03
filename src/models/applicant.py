from pydantic import BaseModel


class Applicant(BaseModel):
    """Applicant profile used to fill Easy Apply forms."""

    name: str
    email: str
    phone: str
    linkedin_url: str = ""

    @property
    def first_name(self) -> str:
        return self.name.split()[0]

    @property
    def last_name(self) -> str:
        parts = self.name.split()
        return parts[-1] if len(parts) > 1 else ""
