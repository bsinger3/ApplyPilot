# ApplyPilot Refactor Plan

This plan captures the fixes needed before running any more resume or cover
letter generation. The current generated materials should be treated as
provisional because several resumes include formatting defects and some
high-scoring jobs are location-ineligible.

## Current Problems

### Resume Subtitle Bug

Generated resumes contain lines like:

```text
Tech | Aug 2025 - Present
```

The dates are correct and should be preserved. The problem is the fake `Tech`
subtitle, which appears to come from the prompt example rather than real resume
data.

Desired output should intentionally preserve the real source-resume location
and dates:

```text
Newark, New Jersey | Aug 2025 - Present
```

### Empty Projects Section

Some rendered resumes show an empty `PROJECTS` section. Projects should be
optional. If there are no real project entries, no `PROJECTS` header should be
emitted or rendered.

### Location-Ineligible High Scores

At least one job in the manual CSV was hybrid in Mexico City, despite the user
living in Newark, New Jersey and seeking local NY/NJ or remote roles. These jobs
should not score as strong fits and should not appear in the manual application
CSV.

For location eligibility, a non-remote job must score no higher than `1` unless
it is either:

- in New York City, or
- accessible within 90 minutes by train or bus from:

  ```text
  54 Polk Street Apt G2
  Newark, NJ 07105
  ```

Driving commute should not be used as the basis for eligibility unless the user
explicitly changes this preference later.

### Filename Collisions

The original generated-material filenames used only `site_title`, causing
different jobs with the same title to overwrite each other. New generation
should use stable per-job filenames that include a URL-derived token.

Generated filenames should also be human-readable in upload dialogs:

- every resume filename must include the word `Resume`
- every cover letter filename must include the words `Cover_Letter`
- every resume and cover letter filename must end with `Brianna_Singer`

Preferred pattern:

```text
linkedin_Product_Manager_b7b770c7a9_Resume_Brianna_Singer.pdf
linkedin_Product_Manager_b7b770c7a9_Cover_Letter_Brianna_Singer.pdf
```

Use the same pattern for the companion `.txt` files.

## Desired Workflow

Do not generate more resumes or cover letters until the discovery, scoring,
location filtering, and resume-formatting fixes are in place.

Recommended order:

1. Fix location eligibility and scoring.
2. Fix resume formatting and optional projects.
3. Clean up bad generated artifacts.
4. Run fresh discovery.
5. Run fresh scoring.
6. Review eligible score-7+ jobs.
7. Generate resumes and cover letters only for eligible jobs.
8. Export a refreshed manual application CSV.

## Refactor Tasks

### 1. Shared Location Eligibility

Create one shared location eligibility module used by discovery, scoring,
generation, CSV export, and auto-apply.

Suggested classification values:

- `eligible_remote`
- `eligible_local`
- `ineligible_location`
- `unknown_location`

Rules:

- Remote or work-from-anywhere roles are eligible.
- Hybrid or onsite roles are eligible only if they are in New York City or
  reachable within 90 minutes by train or bus from `54 Polk Street Apt G2,
  Newark, NJ 07105`.
- Non-local hybrid or onsite roles without a remote option are ineligible and
  should receive location eligibility score `1`.
- Unknown location should be handled conservatively during generation and CSV
  export.

### 2. Search Config Compatibility

Current user config stores location rules under:

```yaml
location:
  accept_patterns:
  reject_patterns:
```

Some code paths appear to read older top-level keys:

```yaml
location_accept:
location_reject_non_remote:
```

Normalize this so all code reads the same config shape, with backward
compatibility for existing files.

### 3. Location-Aware Scoring

Scoring should consider location before asking the LLM, or at least before
accepting the LLM score.

Policy:

- Known ineligible location: set score no higher than `1` or mark ineligible.
- Local or remote: continue normal scoring.
- Unknown: optionally score, but keep a flag so generation/CSV can decide.

This would prevent a hybrid Mexico City role from becoming score 7+.

### 4. Resume Subtitle Formatting

Keep dates. Remove only the fake `Tech` label.

Preferred design:

- Parse experience metadata from the source resume.
- Preserve role/company/location/date metadata in code.
- Let the LLM rewrite only summary, skills, and bullets.
- Render location and dates from known source data rather than free-form LLM
  output.

Minimum defensive cleanup:

- Update the prompt example from `"Tech | Dates"` to a date-only or
  location/date example.
- Strip leading placeholder subtitles like `Tech |` if they appear.
- Validate that generated subtitles do not contain prompt placeholders.

### 5. Optional Projects

Make projects optional end-to-end:

- Prompt: do not require projects.
- JSON validator: do not require `projects`.
- Text assembler: emit `PROJECTS` only when there is at least one non-empty
  project entry.
- PDF renderer: render `Projects` only when parsed entries contain real content.
- Resume validator: do not require a `PROJECTS` section.

### 6. Unique Generated Filenames

Keep the stable URL-token filename approach:

```text
linkedin_Product_Manager_b7b770c7a9_Resume_Brianna_Singer.pdf
linkedin_Product_Manager_b7b770c7a9_Cover_Letter_Brianna_Singer.pdf
```

This prevents duplicate job titles from overwriting each other.

CSV export should prefer unique paths and fall back to legacy paths only for
old generated materials.

### 7. Manual CSV Eligibility Filter

The manual application CSV should include eligible jobs even when generated
materials do not exist yet. Missing resume or cover letter paths should be left
blank rather than excluding the job.

The manual application CSV should include only jobs with:

- a correct eligible or acceptable location status
- a live or recently confirmed posting, when available

The CSV should include these additional columns:

- `location`
- `location_status`
- `checked_at`
- `posting_status`

## Cleanup Plan

Before fresh generation, remove contaminated generated materials.

Controlled cleanup:

1. Find tailored resume `.txt` files containing:

   ```text
   ^Tech |
   ```

2. For each matching tailored resume, delete only the contaminated resume
   files:

   - `.txt`
   - `.pdf`

   Keep the companion traceability files:

   - `_REPORT.json`
   - `_JOB.txt`

3. For DB rows pointing to deleted files:

   - set `tailored_resume_path = NULL`
   - set `tailored_at = NULL`
   - set `cover_letter_path = NULL` if the cover letter belongs to the same
     contaminated generation set
   - set `cover_letter_at = NULL` for those rows

4. Refresh `manual_apply_index.csv`.

Do not regenerate these materials immediately. Regenerate only after fresh
discovery and scoring are complete.

## Validation Checklist

Before resuming generation:

- Any non-remote job outside New York City and outside a 90-minute train/bus
  commute from `54 Polk Street Apt G2, Newark, NJ 07105` is classified as
  location-ineligible.
- A NY/NJ hybrid job is classified as local eligible.
- A remote US job is classified as eligible remote.
- Scoring excludes or strongly penalizes ineligible locations.
- Resume output never contains `Tech |` before dates.
- Resume output keeps real dates.
- Empty `PROJECTS` sections are not rendered.
- Duplicate job titles produce distinct files.
- Manual CSV excludes known location-ineligible jobs.

## Notes

There are existing local edits around resume projects validation and rendering.
Review those carefully before implementing the next pass so unrelated user work
is not overwritten.
