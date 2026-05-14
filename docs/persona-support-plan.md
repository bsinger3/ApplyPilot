# Persona Support Plan

## Goal

ApplyPilot should support multiple job-search personas in one ApplyPilot workspace. A persona represents a resume, profile, and search configuration for a specific application strategy, such as `default`, `product-manager`, or `software-pm`.

The user should be able to discover, score, tailor, generate cover letters, and apply for jobs using an explicit persona without mixing state across resumes.

## Decisions

- Use a persona-specific join table instead of duplicating job rows.
- Migrate the existing single-profile setup into a persona named `default`.
- Require `--persona` for persona-sensitive commands instead of silently using a default.
- Replace URL-as-primary-key with a stable job ID.
- Split logical job postings from discovered source/application URLs.
- Capture company name explicitly, separate from the source site where the job was found.
- Model work arrangement separately from office/home-base location.
- Keep job discovery and enrichment facts global where possible, while allowing many source URLs to point at one logical job.
- Keep scoring, tailoring, cover letters, and application state persona-specific.

## Proposed Data Model

### `jobs`

Stores one logical job opportunity. These facts are independent of any resume, profile, or source URL.

Proposed columns:

- `id`
- `company_name`
- `company_normalized`
- `title`
- `title_normalized`
- `salary`
- `description`
- `full_description`
- `description_hash`
- `work_arrangement`
- `office_location`
- `office_location_normalized`
- `remote_region`
- `location_text`
- `application_url_canonical`
- `created_at`
- `updated_at`

Primary key:

- `id`

`id` can be a UUID string. The important part is that it is stable internal identity, not a discovered URL.

### `job_sources`

Stores every discovered URL or application URL that points at a logical job.

This avoids scoring and tailoring the same job multiple times when the same posting appears on LinkedIn, Indeed, Workday, and the company site.

Proposed columns:

- `id`
- `job_id`
- `url`
- `application_url`
- `source_site`
- `source_strategy`
- `source_company`
- `source_title`
- `source_location`
- `source_work_arrangement`
- `raw_description`
- `discovered_at`
- `detail_scraped_at`
- `detail_error`

Primary key:

- `id`

Unique constraints:

- `url`

Foreign keys:

- `job_id` references `jobs(id)`

Naming note:

- `source_site` means where ApplyPilot found the job, for example `LinkedIn`, `Indeed`, `Workday`, or a direct career site.
- `company_name` means the employer, for example `Stripe`, `Atlassian`, or `Acme Corp`.
- The current code often uses `site` ambiguously. The migration should make this distinction explicit.

### Location And Remote Model

Location should not be a single overloaded string. Many postings are remote but still have a home office, regional eligibility, or occasional in-person requirements.

Recommended fields on `jobs`:

- `work_arrangement`: enum-like text: `remote`, `hybrid`, `onsite`, or `unknown`.
- `office_location`: the office or home-base location associated with the job, for example `New York, NY`.
- `office_location_normalized`: normalized office location for filtering and grouping.
- `remote_region`: geographic eligibility or work-from region, for example `United States`, `US Eastern Time`, `California`, or `Global`.
- `location_text`: original or best human-readable location text from the posting.

Examples:

| Posting text | `work_arrangement` | `office_location` | `remote_region` | `location_text` |
| --- | --- | --- | --- | --- |
| `Remote - United States` | `remote` | `NULL` | `United States` | `Remote - United States` |
| `Remote, based near New York office` | `remote` | `New York, NY` | `United States` | `Remote, based near New York office` |
| `Hybrid - New York, NY` | `hybrid` | `New York, NY` | `NULL` | `Hybrid - New York, NY` |
| `New York, NY` | `onsite` or `unknown` | `New York, NY` | `NULL` | `New York, NY` |

Source rows should preserve source-specific values:

- `job_sources.source_location`
- `job_sources.source_work_arrangement`

Canonical job rows should store the best current interpretation after enrichment.

### Dedupe Signals

UUID primary keys fix the URL-as-identity problem, but they do not automatically dedupe jobs. Dedupe should combine stable signals.

Recommended matching logic:

1. Normalize company name.
2. Normalize job title.
3. Normalize work arrangement, office location, and remote region.
4. Hash the cleaned full description, or a stable description excerpt if the full description is missing.
5. Treat postings as the same logical job when company, title, and description hash match.
6. Treat postings as likely the same logical job when company, title, and canonical application URL strongly match.
7. Do not dedupe on title alone.

The dedupe should be conservative. A company can post the same title for different teams, and those may deserve separate scores and resumes.

### Current `jobs` Columns To Move

Fields that should move out of `jobs` over time:

- `fit_score`
- `score_reasoning`
- `scored_at`
- `tailored_resume_path`
- `tailored_at`
- `tailor_attempts`
- `cover_letter_path`
- `cover_letter_at`
- `cover_attempts`
- `applied_at`
- `apply_status`
- `apply_error`
- `apply_attempts`
- `agent_id`
- `last_attempted_at`
- `apply_duration_ms`
- `apply_task_id`
- `verification_confidence`

### `personas`

Stores each resume/profile/search bundle.

Proposed columns:

- `id`
- `slug`
- `name`
- `profile_path`
- `resume_path`
- `resume_pdf_path`
- `search_config_path`
- `created_at`
- `updated_at`

`slug` should be unique and used in CLI commands, for example `--persona software-pm`.

### `job_persona`

Stores the relationship between a logical job and a specific persona.

Proposed columns:

- `job_id`
- `persona_id`
- `fit_score`
- `score_reasoning`
- `scored_at`
- `tailored_resume_path`
- `tailored_at`
- `tailor_attempts`
- `cover_letter_path`
- `cover_letter_at`
- `cover_attempts`
- `applied_at`
- `apply_status`
- `apply_error`
- `apply_attempts`
- `agent_id`
- `last_attempted_at`
- `apply_duration_ms`
- `apply_task_id`
- `verification_confidence`
- `selected_source_id`
- `applied_source_url`

Primary key:

- `(job_id, persona_id)`

Foreign keys:

- `job_id` references `jobs(id)`
- `persona_id` references `personas(id)`
- `selected_source_id` references `job_sources(id)`

`selected_source_id` and `applied_source_url` record which discovered source or application URL was actually used. For example, ApplyPilot may discover a job on LinkedIn but choose the direct Workday URL for the application.

## Proposed Workspace Layout

Current single-profile files:

```text
~/.applypilot/
  profile.json
  resume.txt
  resume.pdf
  searches.yaml
  applypilot.db
```

Proposed persona-aware layout:

```text
~/.applypilot/
  personas/
    default/
      profile.json
      resume.txt
      resume.pdf
      searches.yaml
    software-pm/
      profile.json
      resume.txt
      resume.pdf
      searches.yaml
  applypilot.db
  tailored_resumes/
    default/
    software-pm/
  cover_letters/
    default/
    software-pm/
```

Existing root-level files can remain as migration input and backward-compatibility references, but new writes should target persona folders.

## CLI Shape

Persona-sensitive commands should require `--persona`:

```powershell
applypilot run --persona default
applypilot run score tailor --persona software-pm
applypilot status --persona software-pm
applypilot apply --persona software-pm
```

Persona management commands:

```powershell
applypilot persona list
applypilot persona create software-pm
applypilot persona show software-pm
```

Potential later command:

```powershell
applypilot persona import software-pm --profile profile.json --resume resume.txt --searches searches.yaml
```

## Pipeline Behavior

### Discovery

`discover --persona <slug>` should load that persona's `searches.yaml`, then write discovered source URLs to `job_sources` and attach them to logical jobs in `jobs`.

Discovery output remains shared because job posting facts and source URLs do not belong to one persona.

### Enrichment

Enrichment remains global. It fetches descriptions and application URLs from `job_sources`, then updates the linked logical job in `jobs` when the enriched description improves the canonical job record.

### Scoring

Scoring is persona-specific:

- Load the selected persona's resume and profile.
- Read enriched jobs from `jobs`.
- Write score data to `job_persona`.
- Allow the same logical job to have different scores for different personas.

### Tailoring

Tailoring is persona-specific:

- Read score eligibility from `job_persona`.
- Load selected persona's resume/profile.
- Write tailored files under `tailored_resumes/<persona>/`.
- Persist `tailored_resume_path` in `job_persona`.

### Cover Letters

Cover letters are persona-specific:

- Read selected persona profile and tailored resume state.
- Write output under `cover_letters/<persona>/`.
- Persist `cover_letter_path` in `job_persona`.

### Auto-Apply

Application selection is persona-specific:

- Select jobs through `job_persona`.
- Use the selected persona profile in the apply prompt.
- Upload the selected persona's tailored resume and cover letter.
- Persist apply state in `job_persona`.
- Store the specific source/application URL used in `selected_source_id` and `applied_source_url`.

## Migration Plan

1. Add new logical-job tables and persona tables:
   - `jobs` with stable `id` identity.
   - `job_sources` with one row per discovered URL.
   - `personas`.
   - `job_persona`.
2. Create the `default` persona if no personas exist.
3. Point `default` at existing root files if present:
   - `profile.json`
   - `resume.txt`
   - `resume.pdf`
   - `searches.yaml`
4. For existing URL-keyed `jobs` rows, create one logical job and one source row per existing URL.
5. Populate company fields as best-effort from existing data, while preserving original ambiguous `site` values in `job_sources.source_site` until each scraper is updated to emit real company names.
6. Copy existing persona-specific columns from the legacy jobs shape into `job_persona` for the `default` persona.
7. Leave existing legacy columns during the first migration for compatibility.
8. Once all code reads/writes `jobs`, `job_sources`, and `job_persona`, consider a later cleanup migration.

## Implementation Notes

- Add a persona resolver in `config.py`, likely `resolve_persona(slug: str)`.
- Avoid module-level persona paths for selected personas; use a runtime object so `--persona` is explicit.
- Add job identity helpers for source URL insertion, company/title normalization, description hashing, and conservative dedupe.
- Update database query helpers to join `jobs`, `job_sources`, and `job_persona` for scoring, tailoring, cover letters, status, dashboard, and apply.
- Update discovery helpers to write source rows and attach each source to an existing or new logical job.
- Keep discovery search configuration persona-specific.
- Include the persona slug in generated filenames or directories to prevent collisions.

## Test Plan

- Migration creates `default` from an existing single-profile workspace.
- Migration converts existing URL-keyed jobs into logical jobs plus source rows.
- Multiple source URLs can point to one logical job.
- Conservative dedupe avoids merging same-title jobs with different descriptions.
- `run --persona default score` writes rows to `job_persona`.
- Two personas can score the same logical job differently.
- Tailoring for persona A does not mark persona B as tailored.
- Cover letter generation for persona A does not mark persona B as complete.
- `apply --persona A` only selects jobs ready for persona A.
- `apply --persona A` records the source/application URL it actually used.
- `status --persona A` reports persona-specific counts.
- Commands that require persona fail clearly when `--persona` is omitted.
