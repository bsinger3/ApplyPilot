"""ApplyPilot database layer: schema, migrations, stats, and connection helpers.

Single source of truth for the jobs table schema. All columns from every
pipeline stage are created up front so any stage can run independently
without migration ordering issues.
"""

import sqlite3
import threading
import hashlib
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from applypilot.config import (
    APP_DIR,
    DB_PATH,
    PROFILE_PATH,
    RESUME_PATH,
    RESUME_PDF_PATH,
    SEARCH_CONFIG_PATH,
)

# Thread-local connection storage — each thread gets its own connection
# (required for SQLite thread safety with parallel workers)
_local = threading.local()


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Get a thread-local cached SQLite connection with WAL mode enabled.

    Each thread gets its own connection (required for SQLite thread safety).
    Connections are cached and reused within the same thread.

    Args:
        db_path: Override the default DB_PATH. Useful for testing.

    Returns:
        sqlite3.Connection configured with WAL mode and row factory.
    """
    path = str(db_path or DB_PATH)

    if not hasattr(_local, 'connections'):
        _local.connections = {}

    conn = _local.connections.get(path)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.ProgrammingError:
            pass

    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    _local.connections[path] = conn
    return conn


def close_connection(db_path: Path | str | None = None) -> None:
    """Close the cached connection for the current thread."""
    path = str(db_path or DB_PATH)
    if hasattr(_local, 'connections'):
        conn = _local.connections.pop(path, None)
        if conn is not None:
            conn.close()


def init_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Create the full jobs table with all columns from every pipeline stage.

    This is idempotent -- safe to call on every startup. Uses CREATE TABLE IF NOT EXISTS
    so it won't destroy existing data.

    Schema columns by stage:
      - Discovery:  url, title, salary, description, location, site, strategy, discovered_at
      - Enrichment: full_description, application_url, detail_scraped_at, detail_error
      - Scoring:    fit_score, score_reasoning, scored_at
      - Tailoring:  tailored_resume_path, tailored_at, tailor_attempts
      - Cover:      cover_letter_path, cover_letter_at, cover_attempts
      - Apply:      applied_at, apply_status, apply_error, apply_attempts,
                   agent_id, last_attempted_at, apply_duration_ms, apply_task_id,
                   verification_confidence

    Args:
        db_path: Override the default DB_PATH.

    Returns:
        sqlite3.Connection with the schema initialized.
    """
    path = db_path or DB_PATH

    # Ensure parent directory exists
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    conn = get_connection(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            -- Discovery stage (smart_extract / job_search)
            id                    TEXT PRIMARY KEY,
            company_name          TEXT,
            company_normalized    TEXT,
            title_normalized      TEXT,
            description_hash      TEXT,
            work_arrangement      TEXT,
            office_location       TEXT,
            office_location_normalized TEXT,
            remote_region         TEXT,
            location_text         TEXT,
            application_url_canonical TEXT,

            -- Legacy URL-keyed shape, kept during compatibility migration
            url                   TEXT UNIQUE,
            title                 TEXT,
            salary                TEXT,
            description           TEXT,
            location              TEXT,
            site                  TEXT,
            strategy              TEXT,
            discovered_at         TEXT,

            -- Enrichment stage (detail_scraper)
            full_description      TEXT,
            application_url       TEXT,
            detail_scraped_at     TEXT,
            detail_error          TEXT,

            -- Scoring stage (job_scorer)
            fit_score             INTEGER,
            score_reasoning       TEXT,
            scored_at             TEXT,

            -- Tailoring stage (resume tailor)
            tailored_resume_path  TEXT,
            tailored_at           TEXT,
            tailor_attempts       INTEGER DEFAULT 0,

            -- Cover letter stage
            cover_letter_path     TEXT,
            cover_letter_at       TEXT,
            cover_attempts        INTEGER DEFAULT 0,

            -- Application stage
            applied_at            TEXT,
            apply_status          TEXT,
            apply_error           TEXT,
            apply_attempts        INTEGER DEFAULT 0,
            agent_id              TEXT,
            last_attempted_at     TEXT,
            apply_duration_ms     INTEGER,
            apply_task_id         TEXT,
            verification_confidence TEXT
        )
    """)
    conn.commit()

    # Run migrations for any columns added after initial schema
    ensure_columns(conn)
    ensure_jobs_uuid_primary_key(conn)
    ensure_persona_schema(conn)
    migrate_legacy_jobs(conn)

    return conn


# Complete column registry: column_name -> SQL type with optional default.
# This is the single source of truth. Adding a column here is all that's needed
# for it to appear in both new databases and migrated ones.
_ALL_COLUMNS: dict[str, str] = {
    # Discovery
    "id": "TEXT",
    "company_name": "TEXT",
    "company_normalized": "TEXT",
    "title_normalized": "TEXT",
    "description_hash": "TEXT",
    "work_arrangement": "TEXT",
    "office_location": "TEXT",
    "office_location_normalized": "TEXT",
    "remote_region": "TEXT",
    "location_text": "TEXT",
    "application_url_canonical": "TEXT",
    "url": "TEXT UNIQUE",
    "title": "TEXT",
    "salary": "TEXT",
    "description": "TEXT",
    "location": "TEXT",
    "site": "TEXT",
    "strategy": "TEXT",
    "discovered_at": "TEXT",
    # Enrichment
    "full_description": "TEXT",
    "application_url": "TEXT",
    "detail_scraped_at": "TEXT",
    "detail_error": "TEXT",
    # Scoring
    "fit_score": "INTEGER",
    "score_reasoning": "TEXT",
    "scored_at": "TEXT",
    # Tailoring
    "tailored_resume_path": "TEXT",
    "tailored_at": "TEXT",
    "tailor_attempts": "INTEGER DEFAULT 0",
    # Cover letter
    "cover_letter_path": "TEXT",
    "cover_letter_at": "TEXT",
    "cover_attempts": "INTEGER DEFAULT 0",
    # Application
    "applied_at": "TEXT",
    "apply_status": "TEXT",
    "apply_error": "TEXT",
    "apply_attempts": "INTEGER DEFAULT 0",
    "agent_id": "TEXT",
    "last_attempted_at": "TEXT",
    "apply_duration_ms": "INTEGER",
    "apply_task_id": "TEXT",
    "verification_confidence": "TEXT",
}


def ensure_columns(conn: sqlite3.Connection | None = None) -> list[str]:
    """Add any missing columns to the jobs table (forward migration).

    Reads the current table schema via PRAGMA table_info and compares against
    the full column registry. Any missing columns are added with ALTER TABLE.

    This makes it safe to upgrade the database from any previous version --
    columns are only added, never removed or renamed.

    Args:
        conn: Database connection. Uses get_connection() if None.

    Returns:
        List of column names that were added (empty if schema was already current).
    """
    if conn is None:
        conn = get_connection()

    existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    added = []

    for col, dtype in _ALL_COLUMNS.items():
        if col not in existing:
            # PRIMARY KEY columns can't be added via ALTER TABLE, but url
            # is always created with the table itself so this is safe
            if "PRIMARY KEY" in dtype:
                continue
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {dtype}")
            added.append(col)

    if added:
        conn.commit()

    return added


def _jobs_id_is_primary_key(conn: sqlite3.Connection) -> bool:
    """Return True when jobs.id is the SQLite primary key."""
    rows = conn.execute("PRAGMA table_info(jobs)").fetchall()
    return any(row[1] == "id" and row[5] == 1 for row in rows)


def ensure_jobs_uuid_primary_key(conn: sqlite3.Connection | None = None) -> None:
    """Promote jobs.id to the physical primary key while preserving legacy data.

    Older ApplyPilot databases used jobs.url as the primary key. SQLite cannot
    alter a primary key in place, so this rebuilds the table once and copies the
    existing columns across with generated UUIDs for rows that lack one.
    """
    if conn is None:
        conn = get_connection()
    if _jobs_id_is_primary_key(conn):
        return

    rows = conn.execute("SELECT * FROM jobs").fetchall()
    row_dicts = [dict(row) for row in rows]
    conn.commit()

    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("""
        CREATE TABLE jobs_rebuild (
            -- Discovery stage (smart_extract / job_search)
            id                    TEXT PRIMARY KEY,
            company_name          TEXT,
            company_normalized    TEXT,
            title_normalized      TEXT,
            description_hash      TEXT,
            work_arrangement      TEXT,
            office_location       TEXT,
            office_location_normalized TEXT,
            remote_region         TEXT,
            location_text         TEXT,
            application_url_canonical TEXT,

            -- Legacy URL-keyed shape, kept during compatibility migration
            url                   TEXT UNIQUE,
            title                 TEXT,
            salary                TEXT,
            description           TEXT,
            location              TEXT,
            site                  TEXT,
            strategy              TEXT,
            discovered_at         TEXT,

            -- Enrichment stage (detail_scraper)
            full_description      TEXT,
            application_url       TEXT,
            detail_scraped_at     TEXT,
            detail_error          TEXT,

            -- Scoring stage (job_scorer)
            fit_score             INTEGER,
            score_reasoning       TEXT,
            scored_at             TEXT,

            -- Tailoring stage (resume tailor)
            tailored_resume_path  TEXT,
            tailored_at           TEXT,
            tailor_attempts       INTEGER DEFAULT 0,

            -- Cover letter stage
            cover_letter_path     TEXT,
            cover_letter_at       TEXT,
            cover_attempts        INTEGER DEFAULT 0,

            -- Application stage
            applied_at            TEXT,
            apply_status          TEXT,
            apply_error           TEXT,
            apply_attempts        INTEGER DEFAULT 0,
            agent_id              TEXT,
            last_attempted_at     TEXT,
            apply_duration_ms     INTEGER,
            apply_task_id         TEXT,
            verification_confidence TEXT
        )
    """)

    columns = list(_ALL_COLUMNS.keys())
    placeholders = ", ".join("?" for _ in columns)
    column_sql = ", ".join(columns)
    for job in row_dicts:
        job["id"] = job.get("id") or _new_id()
        conn.execute(
            f"INSERT OR IGNORE INTO jobs_rebuild ({column_sql}) VALUES ({placeholders})",
            [job.get(col) for col in columns],
        )

    conn.execute("DROP TABLE jobs")
    conn.execute("ALTER TABLE jobs_rebuild RENAME TO jobs")
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")


def _new_id() -> str:
    """Return a stable-looking text UUID for SQLite primary keys."""
    return str(uuid.uuid4())


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_text(value: str | None) -> str:
    """Normalize text for conservative matching and grouping."""
    if not value:
        return ""
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_company(value: str | None) -> str:
    """Normalize company names while keeping the matching conservative."""
    normalized = normalize_text(value)
    suffixes = (
        " incorporated",
        " corporation",
        " company",
        " limited",
        " inc",
        " llc",
        " ltd",
        " corp",
        " co",
    )
    for suffix in suffixes:
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)].strip()
    return normalized


def normalize_title(value: str | None) -> str:
    return normalize_text(value)


def normalize_location(value: str | None) -> str:
    return normalize_text(value)


def infer_work_arrangement(location_text: str | None, description: str | None = None) -> str:
    """Best-effort work arrangement inference from source text."""
    text = normalize_text(" ".join(v for v in (location_text, description) if v))
    if not text:
        return "unknown"
    if "hybrid" in text:
        return "hybrid"
    if "remote" in text or "work from home" in text or "wfh" in text:
        return "remote"
    if "onsite" in text or "on site" in text or "in office" in text:
        return "onsite"
    return "unknown"


def compute_description_hash(description: str | None) -> str | None:
    """Hash cleaned descriptions only when enough text exists for useful dedupe."""
    normalized = normalize_text(description)
    if len(normalized) < 200:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def canonicalize_url(url: str | None) -> str | None:
    """Basic canonical URL cleanup for storage; intentionally conservative."""
    if not url:
        return None
    return url.strip()


def ensure_persona_schema(conn: sqlite3.Connection | None = None) -> None:
    """Create persona-aware tables and indexes without removing legacy columns."""
    if conn is None:
        conn = get_connection()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS personas (
            id                 TEXT PRIMARY KEY,
            slug               TEXT NOT NULL UNIQUE,
            name               TEXT NOT NULL,
            profile_path       TEXT,
            resume_path        TEXT,
            resume_pdf_path    TEXT,
            search_config_path TEXT,
            created_at         TEXT NOT NULL,
            updated_at         TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS job_sources (
            id                      TEXT PRIMARY KEY,
            job_id                  TEXT NOT NULL,
            url                     TEXT NOT NULL UNIQUE,
            application_url         TEXT,
            source_site             TEXT,
            source_strategy         TEXT,
            source_company          TEXT,
            source_title            TEXT,
            source_location         TEXT,
            source_work_arrangement TEXT,
            raw_description         TEXT,
            discovered_at           TEXT,
            detail_scraped_at       TEXT,
            detail_error            TEXT,
            FOREIGN KEY(job_id) REFERENCES jobs(id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS job_persona (
            job_id                  TEXT NOT NULL,
            persona_id              TEXT NOT NULL,
            fit_score               INTEGER,
            score_reasoning         TEXT,
            scored_at               TEXT,
            tailored_resume_path    TEXT,
            tailored_at             TEXT,
            tailor_attempts         INTEGER DEFAULT 0,
            cover_letter_path       TEXT,
            cover_letter_at         TEXT,
            cover_attempts          INTEGER DEFAULT 0,
            applied_at              TEXT,
            apply_status            TEXT,
            apply_error             TEXT,
            apply_attempts          INTEGER DEFAULT 0,
            agent_id                TEXT,
            last_attempted_at       TEXT,
            apply_duration_ms       INTEGER,
            apply_task_id           TEXT,
            verification_confidence TEXT,
            selected_source_id      TEXT,
            applied_source_url      TEXT,
            PRIMARY KEY(job_id, persona_id),
            FOREIGN KEY(job_id) REFERENCES jobs(id),
            FOREIGN KEY(persona_id) REFERENCES personas(id),
            FOREIGN KEY(selected_source_id) REFERENCES job_sources(id)
        )
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_company_title ON jobs(company_normalized, title_normalized)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_description_hash ON jobs(description_hash)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_id_unique ON jobs(id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_job_sources_job_id ON job_sources(job_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_job_persona_persona ON job_persona(persona_id)")
    conn.commit()


def ensure_default_persona(conn: sqlite3.Connection | None = None) -> dict:
    """Create or return the compatibility `default` persona."""
    if conn is None:
        conn = get_connection()
    ensure_persona_schema(conn)

    existing = conn.execute("SELECT * FROM personas WHERE slug = ?", ("default",)).fetchone()
    if existing:
        return dict(existing)

    now = _utc_now()
    persona_id = _new_id()
    conn.execute(
        """
        INSERT INTO personas (
            id, slug, name, profile_path, resume_path, resume_pdf_path,
            search_config_path, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            persona_id,
            "default",
            "Default",
            str(PROFILE_PATH),
            str(RESUME_PATH),
            str(RESUME_PDF_PATH),
            str(SEARCH_CONFIG_PATH),
            now,
            now,
        ),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM personas WHERE id = ?", (persona_id,)).fetchone())


def get_persona_by_slug(slug: str, conn: sqlite3.Connection | None = None) -> dict:
    """Return a persona row by slug, or raise FileNotFoundError."""
    if conn is None:
        conn = get_connection()
    ensure_persona_schema(conn)
    if slug == "default":
        ensure_default_persona(conn)
    row = conn.execute("SELECT * FROM personas WHERE slug = ?", (slug,)).fetchone()
    if not row:
        raise FileNotFoundError(f"Persona not found: {slug}")
    return dict(row)


def create_persona(slug: str, name: str | None = None, conn: sqlite3.Connection | None = None) -> dict:
    """Create a persona with conventional file paths under APP_DIR/personas."""
    if conn is None:
        conn = get_connection()
    ensure_persona_schema(conn)

    slug = normalize_text(slug).replace(" ", "-")
    if not slug:
        raise ValueError("Persona slug cannot be empty.")

    existing = conn.execute("SELECT * FROM personas WHERE slug = ?", (slug,)).fetchone()
    if existing:
        return dict(existing)

    persona_dir = APP_DIR / "personas" / slug
    now = _utc_now()
    persona_id = _new_id()
    conn.execute(
        """
        INSERT INTO personas (
            id, slug, name, profile_path, resume_path, resume_pdf_path,
            search_config_path, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            persona_id,
            slug,
            name or slug.replace("-", " ").title(),
            str(persona_dir / "profile.json"),
            str(persona_dir / "resume.txt"),
            str(persona_dir / "resume.pdf"),
            str(persona_dir / "searches.yaml"),
            now,
            now,
        ),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM personas WHERE id = ?", (persona_id,)).fetchone())


def _find_existing_logical_job(conn: sqlite3.Connection, job: dict) -> str | None:
    """Find an existing logical job using conservative dedupe signals."""
    url = canonicalize_url(job.get("url"))
    if url:
        source = conn.execute("SELECT job_id FROM job_sources WHERE url = ?", (url,)).fetchone()
        if source:
            return source["job_id"]

    company_normalized = normalize_company(job.get("company") or job.get("company_name") or job.get("site"))
    title_normalized = normalize_title(job.get("title"))
    description_hash = compute_description_hash(job.get("full_description") or job.get("description"))
    if company_normalized and title_normalized and description_hash:
        row = conn.execute(
            """
            SELECT id FROM jobs
            WHERE company_normalized = ?
              AND title_normalized = ?
              AND description_hash = ?
            LIMIT 1
            """,
            (company_normalized, title_normalized, description_hash),
        ).fetchone()
        if row:
            return row["id"]

    application_url = canonicalize_url(job.get("application_url"))
    if company_normalized and title_normalized and application_url:
        row = conn.execute(
            """
            SELECT id FROM jobs
            WHERE company_normalized = ?
              AND title_normalized = ?
              AND application_url_canonical = ?
            LIMIT 1
            """,
            (company_normalized, title_normalized, application_url),
        ).fetchone()
        if row:
            return row["id"]

    return None


def upsert_logical_job(
    conn: sqlite3.Connection,
    job: dict,
    source_site: str | None = None,
    source_strategy: str | None = None,
) -> tuple[str, str, bool]:
    """Insert or update a logical job plus its source URL.

    Returns:
        (job_id, source_id, created_job)
    """
    now = _utc_now()
    url = canonicalize_url(job.get("url"))
    if not url:
        raise ValueError("Job source URL is required.")

    company_name = job.get("company") or job.get("company_name") or job.get("site") or source_site
    title = job.get("title")
    location_text = job.get("location") or job.get("location_text")
    full_description = job.get("full_description")
    description = job.get("description")
    description_for_hash = full_description or description
    work_arrangement = job.get("work_arrangement") or infer_work_arrangement(location_text, description_for_hash)
    application_url = canonicalize_url(job.get("application_url"))

    job_id = _find_existing_logical_job(conn, job)
    created_job = False

    if not job_id:
        job_id = _new_id()
        conn.execute(
            """
            INSERT INTO jobs (
                id, company_name, company_normalized, title, title_normalized,
                salary, description, full_description, description_hash,
                work_arrangement, office_location, office_location_normalized,
                remote_region, url, location, location_text, application_url,
                application_url_canonical, site, strategy, discovered_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                company_name,
                normalize_company(company_name),
                title,
                normalize_title(title),
                job.get("salary"),
                description,
                full_description,
                compute_description_hash(description_for_hash),
                work_arrangement,
                job.get("office_location"),
                normalize_location(job.get("office_location")),
                job.get("remote_region"),
                url,
                location_text,
                location_text,
                application_url,
                application_url,
                source_site or job.get("site"),
                source_strategy or job.get("strategy"),
                job.get("discovered_at") or now,
            ),
        )
        created_job = True
    else:
        conn.execute(
            """
            UPDATE jobs
            SET company_name = COALESCE(company_name, ?),
                company_normalized = COALESCE(company_normalized, ?),
                title = COALESCE(title, ?),
                title_normalized = COALESCE(title_normalized, ?),
                salary = COALESCE(salary, ?),
                description = COALESCE(description, ?),
                full_description = COALESCE(full_description, ?),
                description_hash = COALESCE(description_hash, ?),
                work_arrangement = CASE
                    WHEN work_arrangement IS NULL OR work_arrangement = 'unknown' THEN ?
                    ELSE work_arrangement
                END,
                office_location = COALESCE(office_location, ?),
                office_location_normalized = COALESCE(office_location_normalized, ?),
                remote_region = COALESCE(remote_region, ?),
                location = COALESCE(location, ?),
                location_text = COALESCE(location_text, ?),
                application_url = COALESCE(application_url, ?),
                application_url_canonical = COALESCE(application_url_canonical, ?)
            WHERE id = ?
            """,
            (
                company_name,
                normalize_company(company_name),
                title,
                normalize_title(title),
                job.get("salary"),
                description,
                full_description,
                compute_description_hash(description_for_hash),
                work_arrangement,
                job.get("office_location"),
                normalize_location(job.get("office_location")),
                job.get("remote_region"),
                location_text,
                location_text,
                application_url,
                application_url,
                job_id,
            ),
        )

    source_row = conn.execute("SELECT id FROM job_sources WHERE url = ?", (url,)).fetchone()
    if source_row:
        source_id = source_row["id"]
        conn.execute(
            """
            UPDATE job_sources
            SET job_id = ?,
                application_url = COALESCE(application_url, ?),
                source_site = COALESCE(source_site, ?),
                source_strategy = COALESCE(source_strategy, ?),
                source_company = COALESCE(source_company, ?),
                source_title = COALESCE(source_title, ?),
                source_location = COALESCE(source_location, ?),
                source_work_arrangement = COALESCE(source_work_arrangement, ?),
                raw_description = COALESCE(raw_description, ?)
            WHERE id = ?
            """,
            (
                job_id,
                application_url,
                source_site or job.get("site"),
                source_strategy or job.get("strategy"),
                company_name,
                title,
                location_text,
                work_arrangement,
                description,
                source_id,
            ),
        )
    else:
        source_id = _new_id()
        conn.execute(
            """
            INSERT INTO job_sources (
                id, job_id, url, application_url, source_site, source_strategy,
                source_company, source_title, source_location, source_work_arrangement,
                raw_description, discovered_at, detail_scraped_at, detail_error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                job_id,
                url,
                application_url,
                source_site or job.get("site"),
                source_strategy or job.get("strategy"),
                company_name,
                title,
                location_text,
                work_arrangement,
                description,
                job.get("discovered_at") or now,
                job.get("detail_scraped_at"),
                job.get("detail_error"),
            ),
        )

    return job_id, source_id, created_job


def migrate_legacy_jobs(conn: sqlite3.Connection | None = None) -> None:
    """Backfill persona-aware tables from the legacy URL-keyed jobs table."""
    if conn is None:
        conn = get_connection()
    ensure_persona_schema(conn)
    default_persona = ensure_default_persona(conn)

    rows = conn.execute("SELECT * FROM jobs").fetchall()
    for row in rows:
        job = dict(row)
        if not job.get("url"):
            continue
        job_id = job.get("id") or _new_id()
        source_site = job.get("site")
        source_strategy = job.get("strategy")
        location_text = job.get("location_text") or job.get("location")
        description_for_hash = job.get("full_description") or job.get("description")
        work_arrangement = job.get("work_arrangement") or infer_work_arrangement(location_text, description_for_hash)

        conn.execute(
            """
            UPDATE jobs
            SET id = ?,
                company_name = COALESCE(company_name, ?),
                company_normalized = COALESCE(company_normalized, ?),
                title_normalized = COALESCE(title_normalized, ?),
                description_hash = COALESCE(description_hash, ?),
                work_arrangement = COALESCE(work_arrangement, ?),
                office_location = COALESCE(office_location, ?),
                office_location_normalized = COALESCE(office_location_normalized, ?),
                location_text = COALESCE(location_text, ?),
                application_url_canonical = COALESCE(application_url_canonical, ?)
            WHERE url = ?
            """,
            (
                job_id,
                source_site,
                normalize_company(source_site),
                normalize_title(job.get("title")),
                compute_description_hash(description_for_hash),
                work_arrangement,
                None if work_arrangement == "remote" else location_text,
                None if work_arrangement == "remote" else normalize_location(location_text),
                location_text,
                canonicalize_url(job.get("application_url")),
                job["url"],
            ),
        )

        source_id = None
        existing_source = conn.execute("SELECT id FROM job_sources WHERE url = ?", (job["url"],)).fetchone()
        if existing_source:
            source_id = existing_source["id"]
        else:
            source_id = _new_id()
            conn.execute(
                """
                INSERT INTO job_sources (
                    id, job_id, url, application_url, source_site, source_strategy,
                    source_company, source_title, source_location, source_work_arrangement,
                    raw_description, discovered_at, detail_scraped_at, detail_error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    job_id,
                    job["url"],
                    job.get("application_url"),
                    source_site,
                    source_strategy,
                    source_site,
                    job.get("title"),
                    location_text,
                    work_arrangement,
                    job.get("description"),
                    job.get("discovered_at"),
                    job.get("detail_scraped_at"),
                    job.get("detail_error"),
                ),
            )

        conn.execute(
            """
            INSERT OR IGNORE INTO job_persona (
                job_id, persona_id, fit_score, score_reasoning, scored_at,
                tailored_resume_path, tailored_at, tailor_attempts,
                cover_letter_path, cover_letter_at, cover_attempts,
                applied_at, apply_status, apply_error, apply_attempts,
                agent_id, last_attempted_at, apply_duration_ms, apply_task_id,
                verification_confidence, selected_source_id, applied_source_url
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                default_persona["id"],
                job.get("fit_score"),
                job.get("score_reasoning"),
                job.get("scored_at"),
                job.get("tailored_resume_path"),
                job.get("tailored_at"),
                job.get("tailor_attempts") or 0,
                job.get("cover_letter_path"),
                job.get("cover_letter_at"),
                job.get("cover_attempts") or 0,
                job.get("applied_at"),
                job.get("apply_status"),
                job.get("apply_error"),
                job.get("apply_attempts") or 0,
                job.get("agent_id"),
                job.get("last_attempted_at"),
                job.get("apply_duration_ms"),
                job.get("apply_task_id"),
                job.get("verification_confidence"),
                source_id,
                job.get("application_url") if job.get("applied_at") else None,
            ),
        )

    conn.commit()


def get_stats(conn: sqlite3.Connection | None = None, persona_id: str | None = None) -> dict:
    """Return job counts by pipeline stage.

    Provides a snapshot of how many jobs are at each stage, useful for
    dashboard display and pipeline progress tracking.

    Args:
        conn: Database connection. Uses get_connection() if None.

    Returns:
        Dictionary with keys:
            total, by_site, pending_detail, with_description,
            scored, unscored, tailored, untailored_eligible,
            with_cover_letter, applied, score_distribution
    """
    if conn is None:
        conn = get_connection()

    if persona_id is not None:
        return get_persona_stats(conn, persona_id)

    stats: dict = {}

    # Total jobs
    stats["total"] = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    # By site breakdown
    rows = conn.execute(
        "SELECT site, COUNT(*) as cnt FROM jobs GROUP BY site ORDER BY cnt DESC"
    ).fetchall()
    stats["by_site"] = [(row[0], row[1]) for row in rows]

    # Enrichment stage
    stats["pending_detail"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE detail_scraped_at IS NULL"
    ).fetchone()[0]

    stats["with_description"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL"
    ).fetchone()[0]

    stats["detail_errors"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE detail_error IS NOT NULL"
    ).fetchone()[0]

    # Scoring stage
    stats["scored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL"
    ).fetchone()[0]

    stats["unscored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE full_description IS NOT NULL AND fit_score IS NULL"
    ).fetchone()[0]

    # Score distribution
    dist_rows = conn.execute(
        "SELECT fit_score, COUNT(*) as cnt FROM jobs "
        "WHERE fit_score IS NOT NULL "
        "GROUP BY fit_score ORDER BY fit_score DESC"
    ).fetchall()
    stats["score_distribution"] = [(row[0], row[1]) for row in dist_rows]

    # Tailoring stage
    stats["tailored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL"
    ).fetchone()[0]

    stats["untailored_eligible"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE fit_score >= 7 AND full_description IS NOT NULL "
        "AND tailored_resume_path IS NULL"
    ).fetchone()[0]

    stats["tailor_exhausted"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(tailor_attempts, 0) >= 5 "
        "AND tailored_resume_path IS NULL"
    ).fetchone()[0]

    # Cover letter stage
    stats["with_cover_letter"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE cover_letter_path IS NOT NULL"
    ).fetchone()[0]

    stats["cover_exhausted"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(cover_attempts, 0) >= 5 "
        "AND (cover_letter_path IS NULL OR cover_letter_path = '')"
    ).fetchone()[0]

    # Application stage
    stats["applied"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL"
    ).fetchone()[0]

    stats["apply_errors"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE apply_error IS NOT NULL"
    ).fetchone()[0]

    stats["ready_to_apply"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE tailored_resume_path IS NOT NULL "
        "AND applied_at IS NULL "
        "AND application_url IS NOT NULL"
    ).fetchone()[0]

    return stats


def get_persona_stats(conn: sqlite3.Connection, persona_id: str) -> dict:
    """Return job counts by pipeline stage for one persona."""
    stats: dict = {}

    stats["total"] = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    rows = conn.execute(
        "SELECT COALESCE(company_name, site) as company, COUNT(*) as cnt "
        "FROM jobs GROUP BY COALESCE(company_name, site) ORDER BY cnt DESC"
    ).fetchall()
    stats["by_site"] = [(row[0], row[1]) for row in rows]

    stats["pending_detail"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE detail_scraped_at IS NULL"
    ).fetchone()[0]
    stats["with_description"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL"
    ).fetchone()[0]
    stats["detail_errors"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE detail_error IS NOT NULL"
    ).fetchone()[0]

    stats["scored"] = conn.execute(
        "SELECT COUNT(*) FROM job_persona WHERE persona_id = ? AND fit_score IS NOT NULL",
        (persona_id,),
    ).fetchone()[0]
    stats["unscored"] = conn.execute(
        """
        SELECT COUNT(*) FROM jobs j
        LEFT JOIN job_persona jp
          ON jp.job_id = j.id AND jp.persona_id = ?
        WHERE j.full_description IS NOT NULL AND jp.fit_score IS NULL
        """,
        (persona_id,),
    ).fetchone()[0]

    dist_rows = conn.execute(
        """
        SELECT fit_score, COUNT(*) as cnt FROM job_persona
        WHERE persona_id = ? AND fit_score IS NOT NULL
        GROUP BY fit_score ORDER BY fit_score DESC
        """,
        (persona_id,),
    ).fetchall()
    stats["score_distribution"] = [(row[0], row[1]) for row in dist_rows]

    stats["tailored"] = conn.execute(
        "SELECT COUNT(*) FROM job_persona WHERE persona_id = ? AND tailored_resume_path IS NOT NULL",
        (persona_id,),
    ).fetchone()[0]
    stats["untailored_eligible"] = conn.execute(
        """
        SELECT COUNT(*) FROM jobs j
        JOIN job_persona jp ON jp.job_id = j.id
        WHERE jp.persona_id = ? AND jp.fit_score >= 7
          AND j.full_description IS NOT NULL
          AND jp.tailored_resume_path IS NULL
        """,
        (persona_id,),
    ).fetchone()[0]
    stats["tailor_exhausted"] = conn.execute(
        """
        SELECT COUNT(*) FROM job_persona
        WHERE persona_id = ? AND COALESCE(tailor_attempts, 0) >= 5
          AND tailored_resume_path IS NULL
        """,
        (persona_id,),
    ).fetchone()[0]

    stats["with_cover_letter"] = conn.execute(
        "SELECT COUNT(*) FROM job_persona WHERE persona_id = ? AND cover_letter_path IS NOT NULL",
        (persona_id,),
    ).fetchone()[0]
    stats["cover_exhausted"] = conn.execute(
        """
        SELECT COUNT(*) FROM job_persona
        WHERE persona_id = ? AND COALESCE(cover_attempts, 0) >= 5
          AND (cover_letter_path IS NULL OR cover_letter_path = '')
        """,
        (persona_id,),
    ).fetchone()[0]

    stats["applied"] = conn.execute(
        "SELECT COUNT(*) FROM job_persona WHERE persona_id = ? AND applied_at IS NOT NULL",
        (persona_id,),
    ).fetchone()[0]
    stats["apply_errors"] = conn.execute(
        "SELECT COUNT(*) FROM job_persona WHERE persona_id = ? AND apply_error IS NOT NULL",
        (persona_id,),
    ).fetchone()[0]
    stats["ready_to_apply"] = conn.execute(
        """
        SELECT COUNT(*) FROM jobs j
        JOIN job_persona jp ON jp.job_id = j.id
        WHERE jp.persona_id = ?
          AND jp.tailored_resume_path IS NOT NULL
          AND jp.applied_at IS NULL
          AND COALESCE(j.application_url_canonical, j.application_url) IS NOT NULL
        """,
        (persona_id,),
    ).fetchone()[0]

    return stats


def store_jobs(conn: sqlite3.Connection, jobs: list[dict],
               site: str, strategy: str) -> tuple[int, int]:
    """Store discovered jobs, attaching source URLs to logical jobs.

    Args:
        conn: Database connection.
        jobs: List of job dicts with keys: url, title, salary, description, location.
        site: Source site name (e.g. "RemoteOK", "Dice").
        strategy: Extraction strategy used (e.g. "json_ld", "api_response", "css_selectors").

    Returns:
        Tuple of (new_count, duplicate_count).
    """
    now = datetime.now(timezone.utc).isoformat()
    new = 0
    existing = 0

    for job in jobs:
        url = job.get("url")
        if not url:
            continue
        already_seen = conn.execute("SELECT 1 FROM job_sources WHERE url = ?", (url,)).fetchone() is not None
        try:
            job.setdefault("discovered_at", now)
            upsert_logical_job(conn, job, source_site=site, source_strategy=strategy)
            if already_seen:
                existing += 1
            else:
                new += 1
        except sqlite3.IntegrityError:
            # Compatibility fallback for any legacy row collision not represented
            # in job_sources yet.
            existing += 1
        except ValueError:
            continue

    conn.commit()
    return new, existing


def get_jobs_by_stage(conn: sqlite3.Connection | None = None,
                      stage: str = "discovered",
                      min_score: int | None = None,
                      limit: int = 100,
                      persona_id: str | None = None) -> list[dict]:
    """Fetch jobs filtered by pipeline stage.

    Args:
        conn: Database connection. Uses get_connection() if None.
        stage: One of "discovered", "enriched", "scored", "tailored", "applied".
        min_score: Minimum fit_score filter (only relevant for scored+ stages).
        limit: Maximum number of rows to return.

    Returns:
        List of job dicts.
    """
    if conn is None:
        conn = get_connection()

    if persona_id is not None:
        return get_persona_jobs_by_stage(conn, stage=stage, persona_id=persona_id,
                                         min_score=min_score, limit=limit)

    conditions = {
        "discovered": "1=1",
        "pending_detail": "detail_scraped_at IS NULL",
        "enriched": "full_description IS NOT NULL",
        "pending_score": "full_description IS NOT NULL AND fit_score IS NULL",
        "scored": "fit_score IS NOT NULL",
        "pending_tailor": (
            "fit_score >= ? AND full_description IS NOT NULL "
            "AND tailored_resume_path IS NULL AND COALESCE(tailor_attempts, 0) < 5"
        ),
        "tailored": "tailored_resume_path IS NOT NULL",
        "pending_apply": (
            "tailored_resume_path IS NOT NULL AND applied_at IS NULL "
            "AND application_url IS NOT NULL"
        ),
        "applied": "applied_at IS NOT NULL",
    }

    where = conditions.get(stage, "1=1")
    params: list = []

    if "?" in where and min_score is not None:
        params.append(min_score)
    elif "?" in where:
        params.append(7)  # default min_score

    if min_score is not None and "fit_score" not in where and stage in ("scored", "tailored", "applied"):
        where += " AND fit_score >= ?"
        params.append(min_score)

    query = f"SELECT * FROM jobs WHERE {where} ORDER BY fit_score DESC NULLS LAST, discovered_at DESC"
    if limit > 0:
        query += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()

    # Convert sqlite3.Row objects to dicts
    if rows:
        columns = rows[0].keys()
        return [dict(zip(columns, row)) for row in rows]
    return []


def get_persona_jobs_by_stage(conn: sqlite3.Connection,
                              stage: str,
                              persona_id: str,
                              min_score: int | None = None,
                              limit: int = 100) -> list[dict]:
    """Fetch jobs joined with persona-specific pipeline state."""
    ensure_persona_schema(conn)

    select_clause = """
        SELECT
            j.id AS job_id,
            j.url,
            j.title,
            COALESCE(j.company_name, j.site) AS site,
            j.company_name,
            j.salary,
            j.description,
            j.location,
            j.location_text,
            j.work_arrangement,
            j.office_location,
            j.remote_region,
            j.full_description,
            COALESCE(j.application_url_canonical, j.application_url) AS application_url,
            jp.persona_id,
            jp.fit_score,
            jp.score_reasoning,
            jp.scored_at,
            jp.tailored_resume_path,
            jp.tailored_at,
            jp.tailor_attempts,
            jp.cover_letter_path,
            jp.cover_letter_at,
            jp.cover_attempts,
            jp.applied_at,
            jp.apply_status,
            jp.apply_error,
            jp.apply_attempts,
            jp.agent_id,
            jp.last_attempted_at,
            jp.apply_duration_ms,
            jp.apply_task_id,
            jp.verification_confidence,
            jp.selected_source_id,
            jp.applied_source_url
        FROM jobs j
        LEFT JOIN job_persona jp
          ON jp.job_id = j.id
         AND jp.persona_id = ?
    """
    params: list = [persona_id]

    conditions = {
        "discovered": "1=1",
        "pending_detail": "j.detail_scraped_at IS NULL",
        "enriched": "j.full_description IS NOT NULL",
        "pending_score": "j.full_description IS NOT NULL AND jp.fit_score IS NULL",
        "scored": "jp.fit_score IS NOT NULL",
        "pending_tailor": (
            "jp.fit_score >= ? AND j.full_description IS NOT NULL "
            "AND jp.tailored_resume_path IS NULL AND COALESCE(jp.tailor_attempts, 0) < 5"
        ),
        "tailored": "jp.tailored_resume_path IS NOT NULL",
        "pending_cover": (
            "jp.fit_score >= ? AND jp.tailored_resume_path IS NOT NULL "
            "AND j.full_description IS NOT NULL "
            "AND (jp.cover_letter_path IS NULL OR jp.cover_letter_path = '') "
            "AND COALESCE(jp.cover_attempts, 0) < 5"
        ),
        "pending_apply": (
            "jp.tailored_resume_path IS NOT NULL AND jp.applied_at IS NULL "
            "AND COALESCE(j.application_url_canonical, j.application_url) IS NOT NULL"
        ),
        "applied": "jp.applied_at IS NOT NULL",
    }

    where = conditions.get(stage, "1=1")
    if "?" in where:
        params.append(min_score if min_score is not None else 7)
    elif min_score is not None and stage in ("scored", "tailored", "applied"):
        where += " AND jp.fit_score >= ?"
        params.append(min_score)

    query = f"""
        {select_clause}
        WHERE {where}
        ORDER BY jp.fit_score DESC NULLS LAST, j.discovered_at DESC
    """
    if limit > 0:
        query += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()
    if rows:
        columns = rows[0].keys()
        return [dict(zip(columns, row)) for row in rows]
    return []


def ensure_job_persona(conn: sqlite3.Connection, job_id: str, persona_id: str) -> None:
    """Ensure a job/persona state row exists."""
    conn.execute(
        """
        INSERT OR IGNORE INTO job_persona (job_id, persona_id)
        VALUES (?, ?)
        """,
        (job_id, persona_id),
    )


def update_job_score(conn: sqlite3.Connection, job: dict, persona_id: str,
                     score: int, reasoning: str, scored_at: str) -> None:
    """Persist persona-specific score and mirror to legacy columns."""
    job_id = job.get("job_id") or job.get("id")
    ensure_job_persona(conn, job_id, persona_id)
    conn.execute(
        """
        UPDATE job_persona
        SET fit_score = ?, score_reasoning = ?, scored_at = ?
        WHERE job_id = ? AND persona_id = ?
        """,
        (score, reasoning, scored_at, job_id, persona_id),
    )
    if job.get("url"):
        conn.execute(
            "UPDATE jobs SET fit_score = ?, score_reasoning = ?, scored_at = ? WHERE url = ?",
            (score, reasoning, scored_at, job["url"]),
        )


def update_tailor_result(conn: sqlite3.Connection, job: dict, persona_id: str,
                         path: str | None, tailored_at: str | None,
                         success: bool) -> None:
    """Persist persona-specific tailoring result and mirror to legacy columns."""
    job_id = job.get("job_id") or job.get("id")
    ensure_job_persona(conn, job_id, persona_id)
    if success:
        conn.execute(
            """
            UPDATE job_persona
            SET tailored_resume_path = ?, tailored_at = ?,
                tailor_attempts = COALESCE(tailor_attempts, 0) + 1
            WHERE job_id = ? AND persona_id = ?
            """,
            (path, tailored_at, job_id, persona_id),
        )
        if job.get("url"):
            conn.execute(
                "UPDATE jobs SET tailored_resume_path=?, tailored_at=?, "
                "tailor_attempts=COALESCE(tailor_attempts,0)+1 WHERE url=?",
                (path, tailored_at, job["url"]),
            )
    else:
        conn.execute(
            """
            UPDATE job_persona
            SET tailor_attempts = COALESCE(tailor_attempts, 0) + 1
            WHERE job_id = ? AND persona_id = ?
            """,
            (job_id, persona_id),
        )
        if job.get("url"):
            conn.execute(
                "UPDATE jobs SET tailor_attempts=COALESCE(tailor_attempts,0)+1 WHERE url=?",
                (job["url"],),
            )


def update_cover_result(conn: sqlite3.Connection, job: dict, persona_id: str,
                        path: str | None, cover_letter_at: str | None,
                        success: bool) -> None:
    """Persist persona-specific cover-letter result and mirror to legacy columns."""
    job_id = job.get("job_id") or job.get("id")
    ensure_job_persona(conn, job_id, persona_id)
    if success:
        conn.execute(
            """
            UPDATE job_persona
            SET cover_letter_path = ?, cover_letter_at = ?,
                cover_attempts = COALESCE(cover_attempts, 0) + 1
            WHERE job_id = ? AND persona_id = ?
            """,
            (path, cover_letter_at, job_id, persona_id),
        )
        if job.get("url"):
            conn.execute(
                "UPDATE jobs SET cover_letter_path=?, cover_letter_at=?, "
                "cover_attempts=COALESCE(cover_attempts,0)+1 WHERE url=?",
                (path, cover_letter_at, job["url"]),
            )
    else:
        conn.execute(
            """
            UPDATE job_persona
            SET cover_attempts = COALESCE(cover_attempts, 0) + 1
            WHERE job_id = ? AND persona_id = ?
            """,
            (job_id, persona_id),
        )
        if job.get("url"):
            conn.execute(
                "UPDATE jobs SET cover_attempts=COALESCE(cover_attempts,0)+1 WHERE url=?",
                (job["url"],),
            )
