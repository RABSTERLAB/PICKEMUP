from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# The X connector (and any env-driven library code) reads os.environ directly;
# pydantic-settings consumes .env only for THIS object, so export the file
# first — real environment variables keep priority (override=False).
# Anchor for everything location-dependent: sqlite paths AND .env discovery.
# A CWD-relative default silently forks the corpus whenever the API is started
# from another folder (observed in the wild); every launch mode - dev script,
# portable drive, service - must resolve to the same files unless the
# environment says otherwise explicitly.
_REPO_ROOT = Path(__file__).resolve().parents[3]

load_dotenv(_REPO_ROOT / ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_REPO_ROOT / ".env"),
                                       env_prefix="", extra="ignore")

    database_url: str = "sqlite:///./data/pickemup.db"
    pickemup_env: str = "dev"
    # dev/test surfaces (synthetic campaign, mock connectors, file importers,
    # demo endpoint) are OFF unless explicitly enabled: PICKEMUP_DEV_TOOLS=1
    pickemup_dev_tools: bool = False
    pickemup_secret_key: str = "dev-secret-change-me"
    pickemup_api_key: str = ""            # when set, all /api/v1 requires X-API-Key
    pickemup_worker_poll_seconds: int = 2
    pickemup_worker_threads: int = 2
    x_max_cost_per_job_usd: float = 2.0
    x_max_cost_per_day_usd: float = 10.0
    x_cost_per_post_read_usd: float = 0.005
    x_cost_per_user_read_usd: float = 0.010

    @property
    def dev_tools(self) -> bool:
        return self.pickemup_dev_tools or self.pickemup_env == "test"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def model_post_init(self, __context) -> None:
        # fail closed: a production deployment must never run on the shipped
        # default secret or without an API key (env=PICKEMUP_ENV=prod).
        if self.pickemup_env == "prod":
            if self.pickemup_secret_key == "dev-secret-change-me":
                raise RuntimeError(
                    "PICKEMUP_SECRET_KEY is still the public default; set a private value before env=prod")
            if not self.pickemup_api_key:
                raise RuntimeError(
                    "PICKEMUP_API_KEY must be set before env=prod (API would be unauthenticated)")
        # sqlite:///./relative is resolved against CWD by SQLAlchemy; pin it
        # to the project root so the corpus cannot fork by launch directory.
        url = self.database_url
        if url.startswith("sqlite:///") and ":memory:" not in url:
            tail = url[len("sqlite:///"):]
            if not (tail.startswith("/") or tail[1:3] in (":\\", ":/") or len(tail) < 2):
                target = _REPO_ROOT / tail.replace("\\", "/")
                # fresh clones/CI have no data/ dir (gitignored) — sqlite cannot
                # create a file inside a missing directory, so make the parent.
                target.parent.mkdir(parents=True, exist_ok=True)
                self.database_url = "sqlite:///" + target.as_posix()

settings = Settings()
