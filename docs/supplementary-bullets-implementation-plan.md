# Supplementary Bullets Implementation Plan

## Goal

Create a durable `supplementary_bullets.json` file that stores:

- All bullets currently in the base resume.
- Additional transcript-backed bullets discovered during job-specific tailoring.
- Enough metadata to trace each bullet back to its source and decide when to use it.

Going forward, custom resume generation should use this file as the bullet library. For each job description, ApplyPilot should:

1. Search the FWM Supabase transcripts table for relevant work evidence.
2. Generate new grounded bullets from relevant transcript evidence.
3. Add those bullets to `supplementary_bullets.json`.
4. Select the best bullets from `supplementary_bullets.json` for the target JD.
5. Generate the tailored resume from selected bullets rather than relying only on the base resume text.

## Proposed File Location

`portable_applypilot/software-pm/personas/software-pm/supplementary_bullets.json`

This keeps the bullet library persona-specific, next to:

- `profile.json`
- `resume.txt`
- `resume.pdf`
- `searches.yaml`

## Proposed JSON Shape

```json
{
  "version": 1,
  "persona": "software-pm",
  "updated_at": "2026-05-20T00:00:00Z",
  "canonical_roles": {
    "FriendsWithMeasurements.com": {
      "role": "Technical Program Manager, Data & AI Pipelines",
      "company_type": "employer"
    }
  },
  "bullets": [
    {
      "id": "fwm-data-ai-pipelines-001",
      "company": "FriendsWithMeasurements.com",
      "role": "Technical Program Manager, Data & AI Pipelines",
      "category": "experience",
      "bullet": "Led a multi-threaded data pipeline program, coordinating scrape claims, implementation rules, validation metrics, Git/S3/Supabase syncs, and transcript documentation across concurrent engineering workstreams.",
      "source_type": "supabase_transcript",
      "source_refs": [
        {
          "table": "codex_chat_transcripts",
          "chat_key": "codex-fwm-hollister-full-site-scrape-and-remot-650254d980c65001",
          "title": "FWM Hollister full-site scrape and remote sync"
        }
      ],
      "evidence_notes": "Grounded in FWM transcript records describing scrape claims, validation metrics, S3 sync, Git commits, and transcript table updates.",
      "tags": [
        "technical program management",
        "data pipelines",
        "supabase",
        "s3",
        "governance",
        "cross-functional delivery"
      ],
      "metrics": [],
      "created_at": "2026-05-20T00:00:00Z",
      "last_used_at": null
    }
  ]
}
```

## Initial Contents

The first version should include two groups.

### 1. Base Resume Bullets

Every current bullet from `portable_applypilot/software-pm/personas/software-pm/resume.txt` should be added with:

- `source_type`: `base_resume`
- `source_refs`: path to the resume text file
- `company`, `role`, and `category` populated from the resume section
- `tags` inferred conservatively from the bullet text

### 2. FWM Transcript-Backed Bullets

Add the strong FWM bullets already reviewed:

- Led a multi-threaded data pipeline program, coordinating scrape claims, implementation rules, validation metrics, Git/S3/Supabase syncs, and transcript documentation across concurrent engineering workstreams.
- Built and governed Python ingestion pipelines that discover full public product catalogs, extract review text, customer images, ordered sizes, body measurements, product metadata, and source URLs, then validate outputs against a standardized Step 1 intake schema.
- Scaled catalog and review data coverage across high-volume sources, including Hollister with 1,966 products scanned, 213K+ image rows, 160K+ distinct reviews, 194K+ rows with measurement data, and 187K+ Supabase-qualified rows.
- Designed reusable provider-specific scraping patterns for Bazaarvoice, Yotpo, Stamped, Judge.me, Okendo, Shopify, Nuxt, and custom review endpoints, improving delivery speed while preserving product-level coverage and stop conditions for 429/captcha/WAF/auth blocks.
- Established quality gates for data readiness, including dedupe checks, numeric-field validation, missing image/product/comment counts, product URL sampling, broken media checks, coverage summaries, and qualification rules for downstream Supabase insertion.
- Managed cloud-backed data operations by syncing generated FWM_Data outputs and claims to S3, maintaining traceability through Supabase transcript rows, and keeping repo commits scoped to pipeline scripts, documentation, and handoff artifacts.

## Resume Generation Workflow

### Step 1. Parse Job Description

Extract:

- Role title
- Company
- Must-have skills
- Nice-to-have skills
- Domain terms
- Seniority signals
- Delivery expectations
- Required tools/platforms

### Step 1a. Resolve JD-Aware FWM Title

FriendsWithMeasurements.com should always read like a real employer/company, but the role title should be adapted to the job description.

The title resolver should:

- Stay close to the JD title and target function.
- Keep the title plausible for FriendsWithMeasurements.com.
- Avoid blindly copying titles that do not fit the company or the actual work.
- Use a constrained set of FWM-safe title patterns.

Examples:

- Technical Program Manager JD: `Technical Program Manager, Data & AI Pipelines`
- AI/ML Platform JD: `Technical Program Manager, AI & Data Platforms`
- Product Manager JD: `Technical Product Manager, Data & AI Platform`
- Data/Analytics JD: `Technical Program Manager, Data Pipelines & Analytics`
- Implementation/Delivery JD: `Technical Program Manager, Data Platform Delivery`
- Scrum/Agile JD: `Technical Program Manager, Agile Data Delivery`

### Step 2. Search Transcript Table

Search `codex_chat_transcripts` using JD-relevant keywords.

For example, a GenAI TPM JD should search terms such as:

- AI
- ML
- NLP
- LLM
- pipeline
- governance
- validation
- metrics
- stakeholder
- roadmap
- cloud
- Supabase
- S3
- data quality

### Step 3. Generate Candidate Bullets

For each relevant transcript result:

- Use only evidence present in transcript snippets.
- Preserve real metrics exactly.
- Do not infer tools or ownership not present in the transcript.
- Attach source refs to each generated bullet.

### Step 4. Update Supplementary Bullets

Before writing new bullets:

- Normalize bullet text.
- Deduplicate exact or near-identical bullets.
- Preserve existing bullets.
- Add new bullets with `source_type: "supabase_transcript"`.
- Update `updated_at`.

### Step 5. Select Best Bullets

When tailoring a resume, rank bullets using:

- Keyword overlap with the JD.
- Role relevance.
- Recency.
- Metric strength.
- Evidence strength.
- Diversity across responsibilities.
- Exact or close matches to the JD's required skills and responsibilities.
- Numeric accomplishment strength, prioritizing bullets with concrete metrics such as percentages, dollar values, row counts, scale, cycle-time reductions, or volume handled.

The generator should choose the strongest bullets for each role while staying within one-page resume constraints.

Bullet ordering within each company should be JD-aware:

1. Most relevant bullet to the target JD.
2. Strongest metric-bearing bullet, if not already first.
3. Remaining bullets ordered by keyword and responsibility overlap.
4. Lower-relevance bullets omitted first when space is tight.

Each company should have no more than five bullets. In most cases, target three to four bullets per company for a one-page resume.

### Step 6. Generate Resume

The LLM prompt should receive:

- The base resume.
- The job description.
- The selected bullet set from `supplementary_bullets.json`.
- Canonical facts from `profile_overrides.py`.

The prompt should instruct the model to select and lightly reframe bullets, but not invent new work.

### Step 7. Enforce One-Page Resume Constraints

The resume generation process should try to fit the resume on one page.

First, control content:

- Limit each company to at most five bullets.
- Prefer three to four bullets per company.
- Drop lower-relevance bullets before shrinking the design.
- Keep only the most JD-relevant projects, or omit projects when experience carries the match.
- Keep the summary to two concise sentences.
- Keep skills tightly grouped around JD-relevant terms.

Then, control layout:

- Render the PDF.
- If it exceeds one page, reduce vertical spacing.
- If still over one page, reduce font size within a professional range.
- If still over one page, remove the lowest-scoring bullet and rerender.
- Repeat until the resume fits one page or reaches a defined minimum font/spacing threshold.

The PDF renderer should never make the resume unreadably small. Content reduction should happen before aggressive font-size reduction.

## Implementation Tasks

1. Create `supplementary_bullets.json` with base resume bullets and the approved FWM transcript-backed bullets.
2. Add a loader utility for supplementary bullets.
3. Add a writer/upsert utility that appends transcript-backed bullets and deduplicates.
4. Add transcript-search support for job-specific bullet discovery.
5. Update resume tailoring to pass selected supplementary bullets into the LLM prompt.
6. Update validation so generated resumes can only use bullets grounded in the base resume, profile facts, or supplementary bullets.
7. Replace the fixed FWM role override with a JD-aware FWM title resolver constrained to plausible FWM title variants.
8. Add bullet ranking and ordering based on JD relevance, keyword overlap, and numerical accomplishment strength.
9. Enforce no more than five bullets per company.
10. Add one-page PDF fit handling through content trimming, then spacing/font adjustments.
11. Add a small test or smoke check for JD-aware FWM title selection, bullet ordering, max bullet count, and one-page rendering behavior.

## Approval Questions

Before implementation, please confirm:

- File path: should `supplementary_bullets.json` live under the `software-pm` persona folder?
- Should transcript-backed bullets be automatically added during each resume generation, or should they be proposed for review first?
- Should the generator keep every historical generated bullet, or only bullets you explicitly approve?
- Should the one-page fitter be allowed to omit volunteer/projects sections first, or should it only trim experience bullets after skills/spacing adjustments?
