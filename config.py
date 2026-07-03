from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LinkedIn
    linkedin_email: str
    linkedin_password: str

    # Ollama
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3"

    # Job search
    search_query: str = "Software Engineer"
    search_location: str = "United States"
    max_jobs: int = 20

    # Job filters (all optional — leave blank to skip)
    # remote_filter: remote | hybrid | on-site | "" (any)
    remote_filter: Literal["remote", "hybrid", "on-site", ""] = ""
    # date_posted: day | week | month | "" (any time)
    date_posted: Literal["day", "week", "month", ""] = ""
    # experience_level: internship | entry | associate | mid-senior | director | executive | ""
    experience_level: Literal["internship", "entry", "associate", "mid-senior", "director", "executive", ""] = ""
    # job_type: full-time | part-time | contract | temporary | internship | ""
    job_type: Literal["full-time", "part-time", "contract", "temporary", "internship", ""] = ""

    # Browser
    headless: bool = False

    # Features
    generate_cover_letter: bool = False

    # Applicant info for form-filling
    applicant_name: str
    applicant_email: str
    applicant_phone: str
    applicant_linkedin: str = ""

    # Paths
    resume_base_path: Path = Path("resume_base.md")
    data_dir: Path = Path("data")

    @field_validator("data_dir", mode="after")
    @classmethod
    def ensure_data_dirs(cls, v: Path) -> Path:
        (v / "resumes").mkdir(parents=True, exist_ok=True)
        (v / "screenshots").mkdir(parents=True, exist_ok=True)
        return v

    @field_validator("resume_base_path", mode="after")
    @classmethod
    def resume_must_exist(cls, v: Path) -> Path:
        if not v.exists():
            raise ValueError(f"Resume file not found: {v}. Create it before running.")
        return v

    @property
    def cookie_path(self) -> Path:
        return self.data_dir / "cookies.json"
