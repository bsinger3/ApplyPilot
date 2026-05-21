"""Generate validated resumes for score-threshold rows in a manual apply CSV."""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from applypilot.config import load_env, load_profile, resolve_persona_paths
from applypilot.database import (
    canonicalize_url,
    compute_description_hash,
    get_connection,
    get_persona_by_slug,
    upsert_logical_job,
)
from applypilot.enrichment.detail import scrape_detail_page
from applypilot.scoring.pdf import convert_to_pdf
from applypilot.scoring.supplementary_bullets import (
    format_selected_bullets_for_prompt,
    load_supplementary_bullets,
    select_bullets_for_job,
)
from applypilot.scoring.tailor import _resume_filename_prefix, tailor_resume
from applypilot.scoring.validator import validate_resume_pdf


DEFAULT_INPUT = Path(r"C:\Users\bsing\Downloads\manual_apply_index - manual_apply_index_software-pm_refreshed.csv")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "portable_applypilot" / "software-pm" / "resume_generation_unique_company_2026-05-21"
PORTABLE_BULLETS = (
    PROJECT_ROOT
    / "portable_applypilot"
    / "software-pm"
    / "personas"
    / "software-pm"
    / "supplementary_bullets.json"
)


def _score(row: dict[str, str]) -> float:
    try:
        return float(row.get("score") or 0)
    except ValueError:
        return 0.0


def _company_key(row: dict[str, str]) -> str:
    return re.sub(r"\s+", " ", (row.get("company") or "").strip().lower())


def _title_priority(row: dict[str, str]) -> int:
    """Tie-breaker for same-company rows with equal scores."""
    title = str(row.get("title") or "").lower()
    score = 0
    if "technical program manager" in title:
        score += 40
    if "technical project manager" in title:
        score += 35
    if "program manager" in title:
        score += 25
    if "project manager" in title:
        score += 22
    if "product manager" in title:
        score += 18
    if any(term in title for term in ("senior", "sr.", "sr ")):
        score += 4
    if any(term in title for term in ("director", "intern", "principal")):
        score -= 12
    return score


def _dedupe_rows_by_company(rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Keep one best-fit row per company and separate same-company alternatives."""
    indexed = list(enumerate(rows))
    by_company: dict[str, list[tuple[int, dict[str, str]]]] = {}
    keep_indices: set[int] = set()
    duplicates: list[dict[str, str]] = []

    for index, row in indexed:
        key = _company_key(row)
        if not key:
            keep_indices.add(index)
            continue
        by_company.setdefault(key, []).append((index, row))

    for key, items in by_company.items():
        if len(items) == 1:
            keep_indices.add(items[0][0])
            continue
        best_index, best_row = max(
            items,
            key=lambda item: (
                _score(item[1]),
                _title_priority(item[1]),
                -item[0],
            ),
        )
        keep_indices.add(best_index)
        for index, row in items:
            if index == best_index:
                continue
            duplicate = dict(row)
            duplicate["duplicate_resolution"] = (
                f"Moved because {best_row.get('company', row.get('company', 'this company'))} "
                f"already has a stronger kept row: "
                f"{best_row.get('title', '')} (score {best_row.get('score', '')})."
            )
            duplicate["kept_company_job_title"] = best_row.get("title", "")
            duplicate["kept_company_job_score"] = best_row.get("score", "")
            duplicates.append(duplicate)

    kept = [row for index, row in indexed if index in keep_indices]
    duplicates.sort(key=lambda row: (_company_key(row), -_score(row), str(row.get("title", ""))))
    return kept, duplicates


def _safe_filename_part(value: object, max_len: int = 80) -> str:
    text = str(value or "").strip()
    text = re.sub(r'[<>:"/\\|?*]', "", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:max_len].strip(" .") or "resume"


def _load_bullet_library(persona_paths: Any) -> dict[str, Any]:
    library = load_supplementary_bullets(persona_paths)
    if library.get("bullets"):
        return library
    if PORTABLE_BULLETS.exists():
        return json.loads(PORTABLE_BULLETS.read_text(encoding="utf-8-sig"))
    return library


def _row_urls(row: dict[str, str]) -> list[str]:
    urls = []
    for key in ("source_url", "application_url"):
        value = (row.get(key) or "").strip()
        if value and value not in urls:
            urls.append(value)
    return urls


def _find_job(conn: Any, row: dict[str, str]) -> dict[str, Any] | None:
    for url in _row_urls(row):
        match = conn.execute(
            "SELECT * FROM jobs WHERE url = ? OR application_url = ?",
            (url, url),
        ).fetchone()
        if match:
            return dict(match)

    title = (row.get("title") or "").strip()
    company = (row.get("company") or "").strip()
    if title and company:
        match = conn.execute(
            """
            SELECT * FROM jobs
            WHERE lower(title) = lower(?) AND lower(site) = lower(?)
            ORDER BY full_description IS NOT NULL DESC, discovered_at DESC
            LIMIT 1
            """,
            (title, company),
        ).fetchone()
        if match:
            return dict(match)
    return None


def _fetch_and_save_job(
    conn: Any,
    page: Any,
    row: dict[str, str],
) -> dict[str, Any] | None:
    """Fetch a missing JD from the row URLs and save it into the DB."""
    for url in _row_urls(row):
        print(f"  fetching JD: {url}", flush=True)
        result = scrape_detail_page(page, url)
        description = str(result.get("full_description") or "").strip()
        if not description:
            continue

        application_url = canonicalize_url(result.get("application_url")) or (row.get("application_url") or "").strip()
        job_payload = {
            "url": url,
            "application_url": application_url,
            "company": row.get("company"),
            "company_name": row.get("company"),
            "site": row.get("company"),
            "title": row.get("title"),
            "location": row.get("location") or row.get("office_location") or row.get("remote_region"),
            "description": description,
            "full_description": description,
            "work_arrangement": row.get("work_arrangement"),
            "discovered_at": row.get("discovered_at"),
            "detail_scraped_at": datetime.now(timezone.utc).isoformat(),
        }
        job_id, _, _ = upsert_logical_job(conn, job_payload, source_site=row.get("company"), source_strategy="manual_apply_url")
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            UPDATE jobs
            SET full_description = ?,
                description = COALESCE(description, ?),
                application_url = COALESCE(application_url, ?),
                application_url_canonical = COALESCE(application_url_canonical, ?),
                description_hash = ?,
                detail_scraped_at = ?,
                detail_error = NULL
            WHERE id = ?
            """,
            (
                description,
                description,
                application_url,
                application_url,
                compute_description_hash(description),
                now,
                job_id,
            ),
        )
        conn.commit()
        saved = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(saved) if saved else None
    return None


def _ensure_columns(fieldnames: list[str]) -> list[str]:
    required = [
        "Resume Path",
        "tailored_resume_path",
        "custom_resume_path",
        "custom_resume_status",
        "custom_resume_validation_errors",
        "custom_resume_validation_warnings",
    ]
    output = list(fieldnames)
    for name in required:
        if name not in output:
            output.append(name)
    return output


def _duplicate_fieldnames(fieldnames: list[str]) -> list[str]:
    output = list(fieldnames)
    for name in ("duplicate_resolution", "kept_company_job_title", "kept_company_job_score"):
        if name not in output:
            output.append(name)
    return output


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_workbook(path: Path, apply_rows: list[dict[str, str]], duplicate_rows: list[dict[str, str]]) -> None:
    import pandas as pd

    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame(apply_rows).to_excel(writer, sheet_name="manual_apply", index=False)
        pd.DataFrame(duplicate_rows).to_excel(writer, sheet_name="same_company_removed", index=False)


def _row_key(row: dict[str, str]) -> tuple[str, str, str]:
    return (
        (row.get("source_url") or "").strip().lower(),
        (row.get("company") or "").strip().lower(),
        (row.get("title") or "").strip().lower(),
    )


def _merge_existing_approved(output_csv: Path, rows: list[dict[str, str]]) -> int:
    """Carry forward approved resume paths from a previous interrupted run."""
    if not output_csv.exists():
        return 0
    with output_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        previous = list(csv.DictReader(handle))
    previous_by_key = {_row_key(row): row for row in previous}
    merged = 0
    for row in rows:
        prior = previous_by_key.get(_row_key(row))
        if not prior or prior.get("custom_resume_status") != "approved":
            continue
        path = prior.get("custom_resume_path") or prior.get("tailored_resume_path") or prior.get("Resume Path")
        if not path or not Path(path).exists():
            continue
        for field in (
            "Resume Path",
            "tailored_resume_path",
            "custom_resume_path",
            "custom_resume_status",
            "custom_resume_validation_errors",
            "custom_resume_validation_warnings",
        ):
            row[field] = prior.get(field, row.get(field, ""))
        merged += 1
    return merged


def _job_description(job: dict[str, Any]) -> str:
    return str(job.get("full_description") or job.get("description") or "").strip()


def _status_errors(report: dict[str, Any]) -> tuple[str, str]:
    errors: list[str] = []
    warnings: list[str] = []
    for key in ("validator", "quality_validator", "pdf_validator"):
        result = report.get(key) or {}
        errors.extend(str(item) for item in result.get("errors", []) or [])
        warnings.extend(str(item) for item in result.get("warnings", []) or [])
    judge = report.get("judge") or {}
    if judge and not judge.get("passed", True):
        errors.append(f"Judge rejected: {judge.get('issues', 'unknown')}")
    return "; ".join(errors), "; ".join(warnings)


def generate_for_sheet(
    input_csv: Path,
    output_dir: Path,
    min_score: float,
    limit: int | None,
    validation_mode: str,
    persona: str,
) -> dict[str, Any]:
    load_env()
    conn = get_connection()
    persona_row = get_persona_by_slug(persona, conn=conn)
    persona_paths = resolve_persona_paths(persona_row)
    profile = load_profile(persona_row)
    resume_text = persona_paths.resume_path.read_text(encoding="utf-8")
    bullet_library = _load_bullet_library(persona_paths)

    output_dir.mkdir(parents=True, exist_ok=True)
    resume_dir = output_dir / "resumes"
    resume_dir.mkdir(parents=True, exist_ok=True)

    with input_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        original_rows = list(reader)
        rows, duplicate_rows = _dedupe_rows_by_company(original_rows)
        fieldnames = _ensure_columns(reader.fieldnames or [])
        duplicate_fieldnames = _duplicate_fieldnames(reader.fieldnames or [])

    output_csv = output_dir / input_csv.name.replace(".csv", "_with_resume_paths.csv")
    resumed_approved = _merge_existing_approved(output_csv, rows)

    targets = [
        (index, row)
        for index, row in enumerate(rows, start=2)
        if _score(row) >= min_score
        and row.get("custom_resume_status") != "approved"
    ]
    if limit is not None:
        targets = targets[:limit]

    summary: dict[str, Any] = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "input_csv": str(input_csv),
        "output_dir": str(output_dir),
        "min_score": min_score,
        "input_rows": len(original_rows),
        "kept_unique_company_rows": len(rows),
        "same_company_removed": len(duplicate_rows),
        "resumed_approved": resumed_approved,
        "target_rows": len(targets),
        "generated": 0,
        "fetched_job_description": 0,
        "missing_job_description": 0,
        "failed_validation": 0,
        "errors": 0,
        "rows": [],
    }

    missing_for_fetch = [
        (line_number, row)
        for line_number, row in targets
        if not (job := _find_job(conn, row)) or not _job_description(job)
    ]
    if missing_for_fetch:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )
            page = context.new_page()
            for line_number, row in missing_for_fetch:
                job = _fetch_and_save_job(conn, page, row)
                if job and _job_description(job):
                    summary["fetched_job_description"] += 1
            browser.close()

    for completed, (line_number, row) in enumerate(targets, start=1):
            title = row.get("title", "")
            company = row.get("company", "")
            print(f"[{completed}/{len(targets)}] {company} - {title}", flush=True)

            job = _find_job(conn, row)
            if not job or not _job_description(job):
                row["custom_resume_status"] = "missing_job_description"
                row["custom_resume_validation_errors"] = "No full job description found in ApplyPilot DB or fetched from URL."
                summary["missing_job_description"] += 1
                summary["rows"].append({"line": line_number, "title": title, "company": company, "status": "missing_job_description"})
                continue

            selected_bullets = select_bullets_for_job(bullet_library, job)
            selected_bullets_text = format_selected_bullets_for_prompt(selected_bullets)
            report: dict[str, Any] = {"status": "error", "attempts": 0}
            txt_path: Path | None = None
            pdf_path: Path | None = None

            try:
                tailored, report = tailor_resume(
                    resume_text,
                    job,
                    profile,
                    validation_mode=validation_mode,
                    selected_bullets_text=selected_bullets_text,
                    selected_bullets=selected_bullets,
                    bullet_library=bullet_library,
                )
                prefix = _resume_filename_prefix(profile, job)
                prefix = f"{line_number:04d}_{_safe_filename_part(company, 45)}_{_safe_filename_part(prefix, 120)}"
                txt_path = resume_dir / f"{prefix}.txt"
                job_path = resume_dir / f"{prefix}_JOB.txt"
                report_path = resume_dir / f"{prefix}_REPORT.json"

                txt_path.write_text(tailored, encoding="utf-8")
                job_path.write_text(
                    "\n".join(
                        [
                            f"Title: {job.get('title', title)}",
                            f"Company: {job.get('site', company)}",
                            f"Location: {job.get('location', '')}",
                            f"Score: {row.get('score', '')}",
                            f"URL: {job.get('url', row.get('source_url', ''))}",
                            "",
                            _job_description(job),
                        ]
                    ),
                    encoding="utf-8",
                )

                if report.get("status") in {"approved", "approved_with_judge_warning"}:
                    generated_pdf = convert_to_pdf(txt_path)
                    pdf_check = validate_resume_pdf(generated_pdf)
                    report["pdf_validator"] = pdf_check
                    if pdf_check["passed"]:
                        pdf_path = generated_pdf
                        final_path = str(pdf_path)
                        row["Resume Path"] = final_path
                        row["tailored_resume_path"] = final_path
                        row["custom_resume_path"] = final_path
                        row["custom_resume_status"] = "approved"
                        row["custom_resume_validation_errors"] = ""
                        row["custom_resume_validation_warnings"] = _status_errors(report)[1]
                        summary["generated"] += 1
                    else:
                        report["status"] = "failed_pdf_validation"

                if row.get("custom_resume_status") != "approved":
                    errors, warnings = _status_errors(report)
                    row["custom_resume_status"] = str(report.get("status") or "failed_validation")
                    row["custom_resume_validation_errors"] = errors or row["custom_resume_status"]
                    row["custom_resume_validation_warnings"] = warnings
                    summary["failed_validation"] += 1

                report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
                summary["rows"].append(
                    {
                        "line": line_number,
                        "title": title,
                        "company": company,
                        "status": row.get("custom_resume_status"),
                        "path": row.get("custom_resume_path", ""),
                        "attempts": report.get("attempts", 0),
                    }
                )
            except Exception as exc:
                row["custom_resume_status"] = "error"
                row["custom_resume_validation_errors"] = str(exc)
                summary["errors"] += 1
                summary["rows"].append({"line": line_number, "title": title, "company": company, "status": "error", "error": str(exc)})

            if completed % 10 == 0:
                output_csv = output_dir / input_csv.name.replace(".csv", "_with_resume_paths.csv")
                duplicate_csv = output_dir / input_csv.name.replace(".csv", "_same_company_removed.csv")
                _write_csv(output_csv, fieldnames, rows)
                _write_csv(duplicate_csv, duplicate_fieldnames, duplicate_rows)
                _write_workbook(output_dir / input_csv.name.replace(".csv", "_with_resume_paths.xlsx"), rows, duplicate_rows)
                (output_dir / "generation_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    output_csv = output_dir / input_csv.name.replace(".csv", "_with_resume_paths.csv")
    duplicate_csv = output_dir / input_csv.name.replace(".csv", "_same_company_removed.csv")
    output_xlsx = output_dir / input_csv.name.replace(".csv", "_with_resume_paths.xlsx")
    _write_csv(output_csv, fieldnames, rows)
    _write_csv(duplicate_csv, duplicate_fieldnames, duplicate_rows)
    _write_workbook(output_xlsx, rows, duplicate_rows)
    summary["finished_at"] = datetime.now().isoformat(timespec="seconds")
    summary["output_csv"] = str(output_csv)
    summary["duplicate_csv"] = str(duplicate_csv)
    summary["output_xlsx"] = str(output_xlsx)
    (output_dir / "generation_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-csv", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--min-score", type=float, default=8)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--validation", choices=["strict", "normal", "lenient"], default="normal")
    parser.add_argument("--persona", default="software-pm")
    args = parser.parse_args()

    start = time.time()
    summary = generate_for_sheet(
        input_csv=args.input_csv,
        output_dir=args.output_dir,
        min_score=args.min_score,
        limit=args.limit,
        validation_mode=args.validation,
        persona=args.persona,
    )
    summary["elapsed_seconds"] = round(time.time() - start, 1)
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
