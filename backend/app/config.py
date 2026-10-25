"""Configuration for the local, single-user Skill core."""
from dataclasses import dataclass, field
import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import make_url

ROOT = Path(os.getenv("SCA_PROJECT_ROOT", Path(__file__).resolve().parents[2])).resolve()
load_dotenv(ROOT / ".env")


def database_url():
    explicit = os.getenv("SCA_DATABASE_URL")
    if explicit:
        return explicit
    legacy = os.getenv("DATABASE_URL", "")
    if legacy:
        return make_url(legacy).set(database="sca_skill_v2").render_as_string(hide_password=False)
    return ""


@dataclass
class Settings:
    database_url: str = field(default_factory=database_url)
    artifact_root: Path = field(default_factory=lambda: Path(os.getenv("SCA_ARTIFACT_ROOT", ROOT / "data" / "skill-v2")))
    fixture_mode: bool = field(default_factory=lambda: os.getenv("SCA_FIXTURE_MODE", "false").lower() == "true")
    lease_seconds: int = 90
    heartbeat_seconds: int = 15

    def validate(self):
        if not self.database_url:
            raise ValueError("Configure SCA_DATABASE_URL with a PostgreSQL connection")
        url = make_url(self.database_url)
        if url.drivername not in {"postgresql", "postgresql+psycopg"}:
            raise ValueError("SCA_DATABASE_URL must use PostgreSQL with psycopg")
        self.database_url = url.set(drivername="postgresql+psycopg").render_as_string(hide_password=False)
        if self.lease_seconds <= 60 or not 0 < self.heartbeat_seconds < self.lease_seconds:
            raise ValueError("Invalid lease/heartbeat configuration")


settings = Settings()
