"""Settings, read from an env file (default: .env next to the package)."""

import os
from dataclasses import dataclass, field
from pathlib import Path

# Where the env file, the database and received attachments live. By default
# the project folder; AGENT_HOME points it elsewhere (e.g. a mounted volume).
ROOT = Path(os.environ.get("AGENT_HOME") or Path(__file__).resolve().parent.parent).resolve()


@dataclass
class Config:
    mode: str = "test"                 # test | live
    db: Path = ROOT / "data" / "agent.db"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_security: str = "starttls"    # none | starttls | ssl
    imap_host: str = ""
    imap_port: int = 993
    imap_security: str = "ssl"         # none | ssl
    user: str = ""
    password: str = ""
    mail_from: str = ""
    allowed: list = field(default_factory=list)   # addresses or @domains
    followup_days: int = 7
    max_followups: int = 3
    # Test only: a second mailbox that plays the partner and answers requests.
    partner_user: str = ""
    partner_password: str = ""
    # Reading free-text replies with Claude. Without a key that step is skipped.
    anthropic_key: str = ""
    ai_model: str = "claude-opus-5-5"
    # Alternative reader, used only when anthropic_key is empty.
    gemini_key: str = ""
    gemini_model: str = "gemini-3.6-flash"


def _read_env_file(path: Path) -> dict:
    values = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def load(env_file: Path | None = None) -> Config:
    env = _read_env_file(Path(env_file) if env_file else ROOT / ".env")
    get = lambda k, d="": os.environ.get(k, env.get(k, d))
    db = Path(get("AGENT_DB", "data/agent.db"))
    cfg = Config(
        mode=get("MAIL_MODE", "test").lower(),
        db=db if db.is_absolute() else ROOT / db,
        smtp_host=get("SMTP_HOST"),
        smtp_port=int(get("SMTP_PORT", "587")),
        smtp_security=get("SMTP_SECURITY", "starttls").lower(),
        imap_host=get("IMAP_HOST"),
        imap_port=int(get("IMAP_PORT", "993")),
        imap_security=get("IMAP_SECURITY", "ssl").lower(),
        user=get("MAIL_USER"),
        password=get("MAIL_PASSWORD"),
        mail_from=get("MAIL_FROM") or get("MAIL_USER"),
        allowed=[a.strip().lower() for a in get("ALLOWED_RECIPIENTS").split(",") if a.strip()],
        followup_days=int(get("FOLLOWUP_DAYS", "7")),
        max_followups=int(get("MAX_FOLLOWUPS", "3")),
        partner_user=get("TEST_PARTNER_USER"),
        partner_password=get("TEST_PARTNER_PASSWORD"),
        anthropic_key=get("ANTHROPIC_API_KEY"),
        ai_model=get("AI_MODEL", "claude-opus-5-5"),
        gemini_key=get("GEMINI_API_KEY"),
        gemini_model=get("GEMINI_MODEL") or "gemini-3.6-flash",
    )
    if cfg.mode not in ("test", "live"):
        raise ValueError(f"MAIL_MODE πρέπει να είναι test ή live (δόθηκε {cfg.mode!r})")
    return cfg
