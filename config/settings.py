"""
config/settings.py
──────────────────
Loads all configuration from the environment (via a .env file) into a single,
validated, immutable `Settings` object. Import `settings` anywhere:

    from config.settings import settings
    print(settings.smtp.host)

Why it's built this way
-----------------------
* ONE place reads os.environ. No other module touches raw env vars, so there is
  a single, testable boundary for configuration and secrets.
* Secrets never appear in code or logs. `__repr__` masks them.
* Fail fast: if a required secret is missing, we raise a clear error at startup
  instead of deep inside an SMTP call at 2 a.m.
* Grouped, typed sub-configs (SMTP, IMAP, AI, Sheets…) keep call sites readable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


# ── Locate project root and load .env ───────────────────────────────
# settings.py lives in <root>/config/, so the project root is one level up.
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
LOGS_DIR: Path = PROJECT_ROOT / "logs"

# Load .env from the project root. override=False means real environment
# variables (e.g. set by a CI system or the OS) win over the file — standard
# 12-factor behaviour.
load_dotenv(PROJECT_ROOT / ".env", override=False)


# ════════════════════════════════════════════════════════════════════
#  Small helpers for reading + coercing env vars
# ════════════════════════════════════════════════════════════════════
class ConfigError(RuntimeError):
    """Raised when configuration is missing or invalid. Fails startup loudly."""


def _get(name: str, default: str | None = None, *, required: bool = False) -> str:
    val = os.getenv(name, default)
    if required and (val is None or val.strip() == ""):
        raise ConfigError(
            f"Missing required environment variable: {name}. "
            f"Copy .env.example to .env and fill it in."
        )
    return "" if val is None else val.strip()


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _mask(secret: str) -> str:
    """Mask a secret for safe display: keep last 4 chars, hide the rest."""
    if not secret:
        return "<empty>"
    if len(secret) <= 4:
        return "****"
    return "****" + secret[-4:]


# ════════════════════════════════════════════════════════════════════
#  Typed configuration groups
# ════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class SheetsConfig:
    service_account_file: str
    sheet_id: str

    def __repr__(self) -> str:  # keep the file path but not the sheet contents
        return f"SheetsConfig(service_account_file={self.service_account_file!r}, sheet_id={_mask(self.sheet_id)})"


@dataclass(frozen=True)
class AIConfig:
    provider: str            # "anthropic" | "openai"
    anthropic_api_key: str
    anthropic_model: str
    openai_api_key: str
    openai_model: str
    # Optional OpenAI-compatible endpoint. Set this to use a free provider such
    # as Groq ("https://api.groq.com/openai/v1") or Google Gemini
    # ("https://generativelanguage.googleapis.com/v1beta/openai/"). Leave blank
    # for real OpenAI.
    openai_base_url: str = ""

    @property
    def active_key(self) -> str:
        return self.anthropic_api_key if self.provider == "anthropic" else self.openai_api_key

    @property
    def active_model(self) -> str:
        return self.anthropic_model if self.provider == "anthropic" else self.openai_model

    def __repr__(self) -> str:
        return (
            f"AIConfig(provider={self.provider!r}, model={self.active_model!r}, "
            f"key={_mask(self.active_key)})"
        )


@dataclass(frozen=True)
class SMTPConfig:
    host: str
    port: int
    use_ssl: bool
    user: str
    password: str
    sender_name: str
    sender_email: str

    def __repr__(self) -> str:
        return (
            f"SMTPConfig(host={self.host!r}, port={self.port}, use_ssl={self.use_ssl}, "
            f"user={self.user!r}, password={_mask(self.password)}, "
            f"sender={self.sender_name!r} <{self.sender_email}>)"
        )


@dataclass(frozen=True)
class IMAPConfig:
    host: str
    port: int
    user: str
    password: str
    folder: str

    def __repr__(self) -> str:
        return (
            f"IMAPConfig(host={self.host!r}, port={self.port}, user={self.user!r}, "
            f"password={_mask(self.password)}, folder={self.folder!r})"
        )


@dataclass(frozen=True)
class WebSearchConfig:
    provider: str            # "serpapi" | "none"
    serpapi_key: str

    @property
    def enabled(self) -> bool:
        return self.provider.lower() != "none" and bool(self.serpapi_key)

    def __repr__(self) -> str:
        return f"WebSearchConfig(provider={self.provider!r}, key={_mask(self.serpapi_key)}, enabled={self.enabled})"


@dataclass(frozen=True)
class OutreachConfig:
    portfolio_url: str
    booking_url: str


@dataclass(frozen=True)
class BehaviourConfig:
    delay_min_minutes: int
    delay_max_minutes: int
    followup_1_days: int
    followup_2_days: int
    max_email_words: int
    research_confidence_threshold: float
    max_sends_per_run: int
    dry_run: bool
    auto_send: bool  # True = send to every lead with an email, no "Needs Review" holds


# ════════════════════════════════════════════════════════════════════
#  Top-level Settings
# ════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Settings:
    sheets: SheetsConfig
    ai: AIConfig
    smtp: SMTPConfig
    imap: IMAPConfig
    web_search: WebSearchConfig
    outreach: OutreachConfig
    behaviour: BehaviourConfig
    project_root: Path = field(default=PROJECT_ROOT)
    logs_dir: Path = field(default=LOGS_DIR)

    # ── Validation that must pass before ANY run ────────────────────
    def validate_for_run(self) -> None:
        """
        Assert that everything required for a live run is present. Called by
        main.py at startup so misconfiguration is caught immediately, not after
        research has already burned API calls.
        """
        errors: list[str] = []

        if not self.sheets.sheet_id:
            errors.append("GOOGLE_SHEET_ID is not set.")
        if not Path(self.sheets.service_account_file).is_absolute():
            # allow relative-to-root, resolve for the existence check
            sa_path = PROJECT_ROOT / self.sheets.service_account_file
        else:
            sa_path = Path(self.sheets.service_account_file)
        if not sa_path.exists():
            errors.append(f"Service account file not found: {sa_path}")

        if self.ai.provider not in {"anthropic", "openai"}:
            errors.append(f"AI_PROVIDER must be 'anthropic' or 'openai', got {self.ai.provider!r}.")
        if not self.ai.active_key:
            errors.append(f"No API key set for AI provider {self.ai.provider!r}.")

        for label, cfg in (("SMTP", self.smtp), ("IMAP", self.imap)):
            if not cfg.user or not cfg.password:
                errors.append(f"{label} user/password not set.")
        if not self.smtp.sender_email:
            errors.append("SENDER_EMAIL is not set.")

        if self.behaviour.delay_min_minutes > self.behaviour.delay_max_minutes:
            errors.append("DELAY_MIN_MINUTES cannot exceed DELAY_MAX_MINUTES.")

        if errors:
            raise ConfigError(
                "Configuration is invalid:\n  - " + "\n  - ".join(errors)
            )

    def service_account_path(self) -> Path:
        """Absolute path to the service-account JSON, resolving relative paths."""
        p = Path(self.sheets.service_account_file)
        return p if p.is_absolute() else (PROJECT_ROOT / p)


# ════════════════════════════════════════════════════════════════════
#  Build the singleton
# ════════════════════════════════════════════════════════════════════
def _build_settings() -> Settings:
    return Settings(
        sheets=SheetsConfig(
            service_account_file=_get("GOOGLE_SERVICE_ACCOUNT_FILE", "credentials/service_account.json"),
            sheet_id=_get("GOOGLE_SHEET_ID"),
        ),
        ai=AIConfig(
            provider=_get("AI_PROVIDER", "anthropic").lower(),
            anthropic_api_key=_get("ANTHROPIC_API_KEY"),
            anthropic_model=_get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514"),
            openai_api_key=_get("OPENAI_API_KEY"),
            openai_model=_get("OPENAI_MODEL", "gpt-4o"),
            openai_base_url=_get("OPENAI_BASE_URL", ""),
        ),
        smtp=SMTPConfig(
            host=_get("SMTP_HOST", "smtp.hostinger.com"),
            port=_get_int("SMTP_PORT", 465),
            use_ssl=_get_bool("SMTP_USE_SSL", True),
            user=_get("SMTP_USER"),
            password=_get("SMTP_PASSWORD"),
            sender_name=_get("SENDER_NAME", "Prem Thakur"),
            sender_email=_get("SENDER_EMAIL"),
        ),
        imap=IMAPConfig(
            host=_get("IMAP_HOST", "imap.hostinger.com"),
            port=_get_int("IMAP_PORT", 993),
            user=_get("IMAP_USER"),
            password=_get("IMAP_PASSWORD"),
            folder=_get("IMAP_FOLDER", "INBOX"),
        ),
        web_search=WebSearchConfig(
            provider=_get("WEB_SEARCH_PROVIDER", "none").lower(),
            serpapi_key=_get("SERPAPI_KEY"),
        ),
        outreach=OutreachConfig(
            portfolio_url=_get("PORTFOLIO_URL", "https://premthakurr.framer.media/"),
            booking_url=_get("BOOKING_URL", "https://cal.com/encesmarketing/15min"),
        ),
        behaviour=BehaviourConfig(
            delay_min_minutes=_get_int("DELAY_MIN_MINUTES", 4),
            delay_max_minutes=_get_int("DELAY_MAX_MINUTES", 10),
            followup_1_days=_get_int("FOLLOWUP_1_DAYS", 4),
            followup_2_days=_get_int("FOLLOWUP_2_DAYS", 10),
            max_email_words=_get_int("MAX_EMAIL_WORDS", 130),
            research_confidence_threshold=_get_float("RESEARCH_CONFIDENCE_THRESHOLD", 0.55),
            max_sends_per_run=_get_int("MAX_SENDS_PER_RUN", 25),
            dry_run=_get_bool("DRY_RUN", False),
            auto_send=_get_bool("AUTO_SEND", True),
        ),
    )


# The one instance the whole app imports.
settings: Settings = _build_settings()


if __name__ == "__main__":
    # Quick manual check: `python -m config.settings` prints the loaded config
    # with all secrets masked. Handy for verifying your .env without exposing it.
    s = settings
    print("Loaded configuration (secrets masked):")
    print(" ", s.sheets)
    print(" ", s.ai)
    print(" ", s.smtp)
    print(" ", s.imap)
    print(" ", s.web_search)
    print(" ", s.outreach)
    print(" ", s.behaviour)
    print("\nProject root:", s.project_root)
