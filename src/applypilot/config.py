"""ApplyPilot configuration: paths, platform detection, user data."""

import os
import platform
import shutil
from dataclasses import dataclass
from pathlib import Path

# User data directory — all user-specific files live here
APP_DIR = Path(os.environ.get("APPLYPILOT_DIR", Path.home() / ".applypilot"))

# Core paths
DB_PATH = APP_DIR / "applypilot.db"
PERSONAS_DIR = APP_DIR / "personas"
PROFILE_PATH = APP_DIR / "profile.json"
RESUME_PATH = APP_DIR / "resume.txt"
RESUME_PDF_PATH = APP_DIR / "resume.pdf"
SEARCH_CONFIG_PATH = APP_DIR / "searches.yaml"
ENV_PATH = APP_DIR / ".env"

# Generated output
TAILORED_DIR = APP_DIR / "tailored_resumes"
COVER_LETTER_DIR = APP_DIR / "cover_letters"
LOG_DIR = APP_DIR / "logs"

# Chrome worker isolation
CHROME_WORKER_DIR = APP_DIR / "chrome-workers"
APPLY_WORKER_DIR = APP_DIR / "apply-workers"

# Package-shipped config (YAML registries)
PACKAGE_DIR = Path(__file__).parent
CONFIG_DIR = PACKAGE_DIR / "config"


@dataclass(frozen=True)
class PersonaPaths:
    """Resolved persona-specific file paths."""

    slug: str
    name: str
    profile_path: Path
    resume_path: Path
    resume_pdf_path: Path
    search_config_path: Path
    tailored_dir: Path
    cover_letter_dir: Path


def get_chrome_path() -> str:
    """Auto-detect Chrome/Chromium executable path, cross-platform.

    Override with CHROME_PATH environment variable.
    """
    env_path = os.environ.get("CHROME_PATH")
    if env_path and Path(env_path).exists():
        return env_path

    system = platform.system()

    if system == "Windows":
        candidates = [
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ]
    elif system == "Darwin":
        candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ]
    else:  # Linux
        candidates = []
        for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))

    for c in candidates:
        if c and c.exists():
            return str(c)

    # Fall back to PATH search
    for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return found

    raise FileNotFoundError(
        "Chrome/Chromium not found. Install Chrome or set CHROME_PATH environment variable."
    )


def get_chrome_user_data() -> Path:
    """Default Chrome user data directory, cross-platform."""
    system = platform.system()
    if system == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
    elif system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    else:
        return Path.home() / ".config" / "google-chrome"


def ensure_dirs():
    """Create all required directories."""
    for d in [APP_DIR, PERSONAS_DIR, TAILORED_DIR, COVER_LETTER_DIR, LOG_DIR, CHROME_WORKER_DIR, APPLY_WORKER_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def resolve_persona_paths(persona: str | dict | PersonaPaths | None = None) -> PersonaPaths:
    """Resolve persona file paths without touching the database.

    The `default` persona uses existing root-level files for compatibility.
    Non-default personas use APP_DIR/personas/<slug>/.
    """
    if isinstance(persona, PersonaPaths):
        return persona

    if isinstance(persona, dict):
        slug = persona.get("slug") or "default"
        name = persona.get("name") or slug
        if slug == "default":
            return PersonaPaths(
                slug=slug,
                name=name,
                profile_path=Path(persona.get("profile_path") or PROFILE_PATH),
                resume_path=Path(persona.get("resume_path") or RESUME_PATH),
                resume_pdf_path=Path(persona.get("resume_pdf_path") or RESUME_PDF_PATH),
                search_config_path=Path(persona.get("search_config_path") or SEARCH_CONFIG_PATH),
                tailored_dir=TAILORED_DIR / slug,
                cover_letter_dir=COVER_LETTER_DIR / slug,
            )
        persona_dir = PERSONAS_DIR / slug
        return PersonaPaths(
            slug=slug,
            name=name,
            profile_path=Path(persona.get("profile_path") or persona_dir / "profile.json"),
            resume_path=Path(persona.get("resume_path") or persona_dir / "resume.txt"),
            resume_pdf_path=Path(persona.get("resume_pdf_path") or persona_dir / "resume.pdf"),
            search_config_path=Path(persona.get("search_config_path") or persona_dir / "searches.yaml"),
            tailored_dir=TAILORED_DIR / slug,
            cover_letter_dir=COVER_LETTER_DIR / slug,
        )

    slug = persona or "default"
    if slug == "default":
        return PersonaPaths(
            slug="default",
            name="Default",
            profile_path=PROFILE_PATH,
            resume_path=RESUME_PATH,
            resume_pdf_path=RESUME_PDF_PATH,
            search_config_path=SEARCH_CONFIG_PATH,
            tailored_dir=TAILORED_DIR / "default",
            cover_letter_dir=COVER_LETTER_DIR / "default",
        )

    persona_dir = PERSONAS_DIR / slug
    return PersonaPaths(
        slug=slug,
        name=slug.replace("-", " ").title(),
        profile_path=persona_dir / "profile.json",
        resume_path=persona_dir / "resume.txt",
        resume_pdf_path=persona_dir / "resume.pdf",
        search_config_path=persona_dir / "searches.yaml",
        tailored_dir=TAILORED_DIR / slug,
        cover_letter_dir=COVER_LETTER_DIR / slug,
    )


def ensure_persona_dirs(persona: str | dict | PersonaPaths | None = None) -> PersonaPaths:
    """Create output and persona directories for a resolved persona."""
    paths = resolve_persona_paths(persona)
    paths.profile_path.parent.mkdir(parents=True, exist_ok=True)
    paths.tailored_dir.mkdir(parents=True, exist_ok=True)
    paths.cover_letter_dir.mkdir(parents=True, exist_ok=True)
    return paths


def load_profile(persona: str | dict | PersonaPaths | None = None) -> dict:
    """Load user profile for a persona."""
    import json
    paths = resolve_persona_paths(persona)
    if not paths.profile_path.exists():
        raise FileNotFoundError(
            f"Profile not found at {paths.profile_path}. Run `applypilot init` or create the persona first."
        )
    return json.loads(paths.profile_path.read_text(encoding="utf-8-sig"))


def load_search_config(persona: str | dict | PersonaPaths | None = None) -> dict:
    """Load search configuration for a persona."""
    import yaml
    paths = resolve_persona_paths(persona)
    if not paths.search_config_path.exists():
        # Fall back to package-shipped example
        example = CONFIG_DIR / "searches.example.yaml"
        if example.exists():
            return yaml.safe_load(example.read_text(encoding="utf-8"))
        return {}
    return yaml.safe_load(paths.search_config_path.read_text(encoding="utf-8"))


def load_sites_config() -> dict:
    """Load sites.yaml configuration (sites list, manual_ats, blocked, etc.)."""
    import yaml
    path = CONFIG_DIR / "sites.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def is_manual_ats(url: str | None) -> bool:
    """Check if a URL routes through an ATS that requires manual application."""
    if not url:
        return False
    sites_cfg = load_sites_config()
    domains = sites_cfg.get("manual_ats", [])
    url_lower = url.lower()
    return any(domain in url_lower for domain in domains)


def load_blocked_sites() -> tuple[set[str], list[str]]:
    """Load blocked sites and URL patterns from sites.yaml.

    Returns:
        (blocked_site_names, blocked_url_patterns)
    """
    cfg = load_sites_config()
    blocked = cfg.get("blocked", {})
    sites = set(blocked.get("sites", []))
    patterns = blocked.get("url_patterns", [])
    return sites, patterns


def load_blocked_sso() -> list[str]:
    """Load blocked SSO domains from sites.yaml."""
    cfg = load_sites_config()
    return cfg.get("blocked_sso", [])


def load_base_urls() -> dict[str, str | None]:
    """Load site base URLs for URL resolution from sites.yaml."""
    cfg = load_sites_config()
    return cfg.get("base_urls", {})


# ---------------------------------------------------------------------------
# Default values — referenced across modules instead of magic numbers
# ---------------------------------------------------------------------------

DEFAULTS = {
    "min_score": 7,
    "max_apply_attempts": 3,
    "max_tailor_attempts": 5,
    "poll_interval": 60,
    "apply_timeout": 300,
    "viewport": "1280x900",
}


def load_env():
    """Load environment variables from ~/.applypilot/.env if it exists."""
    from dotenv import load_dotenv
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)
    # Also try CWD .env as fallback
    load_dotenv()


# ---------------------------------------------------------------------------
# Tier system — feature gating by installed dependencies
# ---------------------------------------------------------------------------

TIER_LABELS = {
    1: "Discovery",
    2: "AI Scoring & Tailoring",
    3: "Full Auto-Apply",
}

TIER_COMMANDS: dict[int, list[str]] = {
    1: ["init", "run discover", "run enrich", "status", "dashboard"],
    2: ["run score", "run tailor", "run cover", "run pdf", "run"],
    3: ["apply"],
}


def get_tier() -> int:
    """Detect the current tier based on available dependencies.

    Tier 1 (Discovery):            Python + pip
    Tier 2 (AI Scoring & Tailoring): + LLM API key
    Tier 3 (Full Auto-Apply):       + Claude Code CLI + Chrome
    """
    load_env()

    has_llm = any(os.environ.get(k) for k in ("GEMINI_API_KEY", "OPENAI_API_KEY", "LLM_URL"))
    if not has_llm:
        return 1

    has_claude = shutil.which("claude") is not None
    try:
        get_chrome_path()
        has_chrome = True
    except FileNotFoundError:
        has_chrome = False

    if has_claude and has_chrome:
        return 3

    return 2


def check_tier(required: int, feature: str) -> None:
    """Raise SystemExit with a clear message if the current tier is too low.

    Args:
        required: Minimum tier needed (1, 2, or 3).
        feature: Human-readable description of the feature being gated.
    """
    current = get_tier()
    if current >= required:
        return

    from rich.console import Console
    _console = Console(stderr=True)

    missing: list[str] = []
    if required >= 2 and not any(os.environ.get(k) for k in ("GEMINI_API_KEY", "OPENAI_API_KEY", "LLM_URL")):
        missing.append("LLM API key — run [bold]applypilot init[/bold] or set GEMINI_API_KEY")
    if required >= 3:
        if not shutil.which("claude"):
            missing.append("Claude Code CLI — install from [bold]https://claude.ai/code[/bold]")
        try:
            get_chrome_path()
        except FileNotFoundError:
            missing.append("Chrome/Chromium — install or set CHROME_PATH")

    _console.print(
        f"\n[red]'{feature}' requires {TIER_LABELS.get(required, f'Tier {required}')} (Tier {required}).[/red]\n"
        f"Current tier: {TIER_LABELS.get(current, f'Tier {current}')} (Tier {current})."
    )
    if missing:
        _console.print("\n[yellow]Missing:[/yellow]")
        for m in missing:
            _console.print(f"  - {m}")
    _console.print()
    raise SystemExit(1)
