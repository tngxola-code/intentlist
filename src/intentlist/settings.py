"""Runtime configuration. Everything is environment-driven (12-factor); see .env.example."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INTENTLIST_", env_file=".env", extra="ignore")

    # --- storage -----------------------------------------------------------
    database_url: str = "sqlite:///./intentlist.db"
    capture_dir: Path = Path("./data/captures")
    export_dir: Path = Path("./data/exports")

    # --- config files ------------------------------------------------------
    sources_file: Path = Path("./config/sources.yaml")
    taxonomy_file: Path = Path("./config/taxonomy.yaml")

    # --- providers: "fixture" runs offline with no keys ---------------------
    search_provider: str = "brave"  # brave | fixture
    extractor: str = "claude"  # claude | rules
    planner: str = "claude"  # claude | template
    email_verifier: str = "zerobounce"  # zerobounce | millionverifier | local | fixture
    fetch_mode: str = "http"  # http | fixture

    brave_api_key: str | None = None
    anthropic_api_key: str | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    anthropic_model: str = "claude-sonnet-5"
    zerobounce_api_key: str | None = None
    millionverifier_api_key: str | None = None

    fixture_dir: Path = Path("./tests/fixtures")

    # --- crawling etiquette ------------------------------------------------
    user_agent: str = "IntentListResearchBot/1.0 (+https://example.com/bot; contact: ops@example.com)"
    default_rate_limit_rps: float = 0.5
    request_timeout_s: float = 20.0
    max_page_bytes: int = 3_000_000

    # --- pipeline policy ---------------------------------------------------
    batch_size: int = 100
    max_post_age_days: int = 730  # older wanted posts are unlikely to still be live
    verification_cache_days: int = 30
    check_email_dns: bool = True
    queries_per_run: int = 40
    results_per_query: int = 20
    min_confidence: float = 0.6

    # --- cost model (USD) used for per-record economics reporting -----------
    cost_search_call: float = 0.005
    cost_llm_input_mtok: float = 3.0
    cost_llm_output_mtok: float = 15.0
    cost_email_verification: float = 0.008

    # --- review UI auth: "alice:secret,bob:secret2" -------------------------
    reviewers: str = "admin:change-me"

    def reviewer_map(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for pair in filter(None, (p.strip() for p in self.reviewers.split(","))):
            user, _, pw = pair.partition(":")
            if user and pw:
                out[user] = pw
        return out


@lru_cache
def get_settings() -> Settings:
    return Settings()
