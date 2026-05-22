# Auto-Apply Backend Experiments

This document defines experiments for finding the most reliable backend for automatic job application filling and submission in ApplyPilot.

The goal is to compare tools by evidence, not vibes. A backend should only become the default if it reliably fills applications, uploads documents, detects blockers, and produces auditable results.

## Goals

- Identify which browser/agent backend works best for real job application flows.
- Separate model issues from browser-control issues.
- Measure reliability by ATS type, not only overall success rate.
- Prefer safe automation: fill accurately, stop when uncertain, and submit only after explicit human approval.
- Produce logs/screenshots/result objects that explain failures clearly.

## Candidate Backends

### Current Backend: Claude/OpenAI + Playwright MCP

Current ApplyPilot apply flow:

```text
ApplyPilot job
-> launch browser
-> send large prompt to model/tool agent
-> agent controls Playwright MCP
-> parse RESULT line
-> update database
```

Variants to test:

- Claude model through Claude Code.
- OpenAI model through an equivalent tool loop, if available.
- Same prompt vs model-specific prompt.
- Dry-run mode vs human-approved submit mode.

Primary question:

Does the current architecture fail because of the model, the prompt, Playwright MCP, or arbitrary ATS complexity?

### Browserbase + Stagehand

Stagehand offers higher-level browser actions such as observe/act/extract while still allowing lower-level Playwright access for uploads and special cases.

Hypothesis:

Stagehand may perform better than the current giant-prompt loop because it provides more structured browser primitives and managed browser sessions.

Primary question:

Can Stagehand reliably reach a ready-to-submit state across common ATS systems?

### Skyvern

Skyvern is a purpose-built browser agent/workflow system for multi-step web tasks.

Hypothesis:

Skyvern may handle messy unknown forms better than a custom prompt-based Playwright agent, especially when workflows can be saved or replayed.

Primary question:

Does Skyvern reduce per-site brittleness enough to justify handing off more control to an external agent system?

### Browser-Use

Browser-use is an open-source browser agent framework.

Hypothesis:

Browser-use may be useful as a local/self-hosted alternative, but may face similar issues unless wrapped with strong validation and result schemas.

Primary question:

Can browser-use outperform the current ApplyPilot approach without adding too much operational complexity?

### Raw Playwright With ATS Adapters

Instead of using a general browser agent, build deterministic adapters for common ATS platforms.

Examples:

- Greenhouse
- Lever
- Ashby
- Workday
- iCIMS
- SmartRecruiters
- Generic HTML form

Hypothesis:

Raw Playwright with site-specific adapters may be the most reliable for high-volume common ATS systems, even if it has lower coverage for unknown forms.

Primary question:

Is deterministic automation for top ATS platforms more valuable than broad agentic coverage?

## Test Modes

### Mode 1: Page Understanding

The backend opens the application URL and returns structured page state.

Required output:

```json
{
  "applicationReachable": true,
  "ats": "greenhouse",
  "jobOpen": true,
  "loginRequired": false,
  "captchaDetected": false,
  "applyFormPresent": true,
  "blockers": []
}
```

Success criteria:

- Correctly identifies whether the page is a real application.
- Correctly detects expired jobs.
- Correctly detects login/captcha/SSO blockers.
- Does not click submit or alter the application.

## Common Question Answer Bank

ApplyPilot should maintain a reusable bank of common application questions and approved answers. Every backend should consult this bank before generating a free-form answer.

The answer bank should be stored as structured data, not only embedded in prompts. Suggested file:

```text
~/.applypilot/common_answers.yaml
```

or persona-specific:

```text
~/.applypilot/personas/<persona>/common_answers.yaml
```

### Answer Precedence

When a question appears, answer sources should be applied in this order:

1. Explicit user-approved common answer bank.
2. Persona `profile.json` hard facts.
3. Job-specific tailored resume/cover letter context.
4. LLM-generated answer.
5. `needs_review` if the answer is uncertain.

Hard facts must never be invented. If the answer bank and `profile.json` conflict, the backend should stop with `needs_review` instead of guessing.

### Initial Common Answers

These are user-approved defaults for the current experiments:

| Question Pattern | Answer |
|---|---|
| Have you ever worked for this company before? | No |
| Are you a former employee of this company? | No |
| Are you subject to a non-compete agreement? | No |
| Are you subject to any restrictive covenant? | No |
| Do you have any security clearances? | I do not have any security clearances |
| What security clearance do you hold? | None |
| Are you over 18? | Yes |
| Are you at least 18 years old? | Yes |
| Do you agree to the privacy policy? | Yes |
| Do you agree to the applicant privacy notice? | Yes |
| Will you now or in the future require company sponsorship? | No |
| Will you require visa sponsorship? | No |
| Do you need employer sponsorship to work in this country? | No |

### Example YAML Shape

```yaml
answers:
  - id: previous_company_employment
    match:
      any:
        - "ever worked for this company"
        - "former employee"
        - "previously employed by"
    answer: "No"
    category: employment_history
    confidence: high

  - id: non_compete
    match:
      any:
        - "non-compete"
        - "non compete"
        - "restrictive covenant"
    answer: "No"
    category: legal
    confidence: high

  - id: security_clearance
    match:
      any:
        - "security clearance"
        - "clearance level"
    answer: "I do not have any security clearances"
    category: hard_fact
    confidence: high

  - id: over_18
    match:
      any:
        - "over 18"
        - "at least 18"
        - "18 years"
    answer: "Yes"
    category: eligibility
    confidence: high

  - id: privacy_policy
    match:
      any:
        - "privacy policy"
        - "privacy notice"
        - "privacy statement"
    answer: "Yes"
    category: consent
    confidence: high

  - id: sponsorship_required
    match:
      any:
        - "require sponsorship"
        - "visa sponsorship"
        - "employer sponsorship"
        - "now or in the future"
    answer: "No"
    category: work_authorization
    confidence: high
```

### Backend Requirements

Every backend should report which answer source it used:

```json
{
  "question": "Will you now or in the future require sponsorship?",
  "answer": "No",
  "source": "common_answers",
  "answerId": "sponsorship_required",
  "confidence": "high"
}
```

Questions should be flagged as `needs_review` when:

- The question is legal/eligibility-related and no approved answer exists.
- The answer bank conflicts with `profile.json`.
- The question asks for a certification, license, clearance, citizenship, education credential, relocation, or compensation fact that is not in the profile.
- The answer would require interpretation beyond the approved examples.

### Mode 2: Dry-Run Fill

The backend fills the application but stops before final submission.

Required output:

```json
{
  "status": "dry_run_ready",
  "resumeUploaded": true,
  "coverLetterUploaded": false,
  "requiredFieldsComplete": true,
  "missingRequiredFields": [],
  "unknownQuestions": [],
  "blockers": [],
  "confidence": "high"
}
```

Success criteria:

- Resume is uploaded.
- Identity fields are correct.
- Work authorization is answered truthfully from profile.
- EEO/demographic fields are answered from `profile.json` when values are provided.
- Required screening questions are answered or flagged.
- Backend stops before final submit.

### Mode 3: Human-Approved Submit

The backend fills the form, pauses for review, then submits after approval.

Required output before approval:

```json
{
  "status": "needs_approval",
  "summary": {
    "name": "filled",
    "email": "filled",
    "resume": "uploaded",
    "workAuthorization": "answered",
    "screeningQuestions": "answered"
  },
  "riskFlags": []
}
```

Success criteria:

- User can inspect the form before submission.
- Submission happens only after explicit approval.
- Final confirmation is captured.

### Mode 4: Approved Submit Only

The backend fills the form, pauses for human review, and submits only after explicit approval.

ApplyPilot should not support unattended final submission. Even high-confidence applications must stop for approval before the final submit/apply action.

Success criteria:

- No final submission occurs before approval.
- No unknown required fields.
- No sensitive/unsafe fields.
- Approval action is logged.
- Confirmation page is captured after approved submission.
- Database is updated with applied status only after confirmed submission.

## Benchmark URL Set

Build a fixed test set of real jobs. Use fresh jobs where possible, because expired postings distort results.

Recommended minimum:

| ATS / Flow | Count | Notes |
|---|---:|---|
| Greenhouse | 5 | Usually simpler, good baseline |
| Lever | 5 | Usually simpler, good baseline |
| Ashby | 5 | Often modern and dynamic |
| Workday | 5 | Account and multi-step heavy |
| iCIMS | 5 | Often brittle and older |
| SmartRecruiters | 3 | Common enough to test |
| Generic company forms | 5 | Unknown layouts |
| Login-required portals | 3 | Should detect and stop |
| Captcha/Cloudflare pages | 3 | Should detect and stop |
| Expired jobs | 3 | Should classify as expired |

Total target: 42 URLs.

For each URL, record:

```text
url
company
job title
ats/provider
expected behavior
requires login?
requires captcha?
known expired?
notes
```

## Metrics

### Core Metrics

| Metric | Definition |
|---|---|
| Page reach rate | Opened the correct application page |
| ATS classification accuracy | Correctly identified Greenhouse, Lever, etc. |
| Form reach rate | Reached the actual application form |
| Resume upload rate | Uploaded the correct tailored resume |
| Field accuracy | Filled profile fields correctly |
| Screening accuracy | Answered questions truthfully and specifically |
| Blocker detection rate | Correctly stopped on captcha/login/unsafe flows |
| Dry-run ready rate | Filled enough to be ready for review |
| Approved submit success rate | Submitted after approval and captured confirmation |
| False submit rate | Submitted without approval or when it should have stopped |
| Useful failure rate | Failure reason is specific and actionable |

## Cost Evaluation

Pricing should be measured per attempted application and per successful approved submission.

The numbers below are estimates for planning as of May 21, 2026. Vendor pricing changes often, so update the source links before making a final backend decision.

### Pricing Sources

| Tool / Provider | Pricing Source | Notes |
|---|---|---|
| Browserbase / Stagehand | [Browserbase pricing](https://www.browserbase.com/pricing/) | Developer plan is $20/mo with 100 browser hours, then $0.12/browser hour. Startup is $99/mo with 500 browser hours, then $0.10/browser hour. Model Gateway is market-price pass-through. |
| Skyvern | [Skyvern pricing](https://www.skyvern.com/pricing) | Free includes 1,000 credits. Hobby is $29/mo for 30,000 credits. Pro is $149/mo for 150,000 credits. Credits are consumed based on complexity and duration. |
| Firecrawl | [Firecrawl pricing](https://www.firecrawl.dev/pricing) | Scrape/crawl/map are generally 1 credit per page. Search is credit-based. Interact is listed as browser-minute/action based. Firecrawl is better for pre-reading/classification than final submission. |
| OpenAI | [OpenAI API pricing](https://platform.openai.com/docs/pricing/) | Model/token prices vary. For baseline estimates, use the selected model's input/output price per 1M tokens. |
| Anthropic Claude | [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing) | Claude Sonnet-class pricing is commonly modeled as input/output token pricing; prompt caching can materially reduce repeated prompt cost. |

### Cost Formula

Use this formula for every backend:

```text
cost_per_attempt =
  browser_runtime_cost
  + model_input_tokens * input_price_per_token
  + model_output_tokens * output_price_per_token
  + tool_credit_cost
  + proxy_or_captcha_cost
```

Then track:

```text
cost_per_successful_approved_submission =
  total_cost_of_all_attempts / successful_approved_submissions
```

This second number matters more than raw per-attempt cost. A backend that costs $0.60 and succeeds 80% of the time is cheaper than a backend that costs $0.20 and succeeds 15% of the time.

### Standard Cost Assumptions

Use these assumptions until real telemetry replaces them:

| Scenario | Browser Time | Input Tokens | Output Tokens | Description |
|---|---:|---:|---:|---|
| Page understanding only | 2 min | 20,000 | 2,000 | Open page, classify ATS/blockers |
| Simple dry-run fill | 8 min | 100,000 | 10,000 | Greenhouse/Lever-style form |
| Medium dry-run fill | 15 min | 180,000 | 20,000 | Ashby/generic multi-section form |
| Hard dry-run fill | 30 min | 400,000 | 50,000 | Workday/iCIMS-style flow |
| Approved submit | +2 min | +25,000 | +3,000 | Human approves, backend clicks submit and captures confirmation |

For human-approved submission, the backend must stop before final submit and wait for approval. There should be no unattended final submission cost path.

### Estimated LLM Cost Per Attempt

Replace these with the exact model prices used in each experiment.

| Model Class | Approx Input / 1M | Approx Output / 1M | Page Understanding | Simple Dry-Run | Medium Dry-Run | Hard Dry-Run |
|---|---:|---:|---:|---:|---:|---:|
| Low-cost model | $1.00 | $5.00 | $0.03 | $0.15 | $0.28 | $0.65 |
| GPT-5-class baseline | $1.25 | $10.00 | $0.05 | $0.23 | $0.43 | $1.00 |
| Claude Sonnet-class baseline | $3.00 | $15.00 | $0.09 | $0.45 | $0.84 | $1.95 |

Notes:

- Prompt caching may reduce repeated system/profile prompt cost.
- Failed attempts still cost money.
- The current giant-prompt backend may use more tokens than a structured backend because it repeatedly snapshots and reasons over long page state.
- A deterministic ATS adapter should be much cheaper because it uses less model reasoning.

### Estimated Browser / Tool Cost Per Attempt

| Backend | Browser/Tool Cost Estimate | Notes |
|---|---:|---|
| Local Playwright | $0.00 marginal | Uses local machine/browser. Real cost is developer time, maintenance, and possible proxy/captcha services. |
| Browserbase + Stagehand | ~$0.02 for 10 min, ~$0.06 for 30 min at $0.12/hr overage | If already inside included monthly browser hours, marginal runtime may be treated as $0; for planning, amortize subscription across expected volume. |
| Skyvern | Unknown until measured; rough planning range $0.10-$1.00/run | Hobby and Pro imply about $0.001 per included credit, but credits/run vary by complexity and duration. Log credits consumed per run. |
| Firecrawl pre-read/classification | Usually <$0.01 per job on Standard-scale credits | Useful for extracting page/job context before browser automation. Not recommended as the final submitter. |
| Raw Playwright ATS adapter | $0.00 local, or Browserbase runtime if cloud-hosted | Lowest model cost if deterministic selectors work. Highest engineering maintenance cost. |

### Backend Cost Estimate Matrix

These estimates exclude fixed monthly subscription fees unless noted.

| Backend | Page Understanding | Simple Dry-Run | Medium Dry-Run | Hard Dry-Run | Approved Submit Increment | Expected Cost Driver |
|---|---:|---:|---:|---:|---:|---|
| Current Claude/Playwright MCP | $0.09-$0.20 | $0.45-$0.90 | $0.84-$1.60 | $1.95-$4.00 | $0.12-$0.25 | LLM tokens from repeated snapshots/tool loops |
| Current OpenAI/Playwright MCP | $0.05-$0.12 | $0.23-$0.70 | $0.43-$1.20 | $1.00-$3.00 | $0.06-$0.20 | Model choice and retry rate |
| Browserbase + Stagehand | $0.07-$0.20 | $0.25-$0.95 | $0.50-$1.60 | $1.10-$4.00 | $0.08-$0.25 | LLM tokens plus Browserbase runtime |
| Skyvern | $0.10-$0.50 | $0.25-$1.50 | $0.50-$3.00 | $1.00-$6.00 | TBD | Credits consumed per run |
| Firecrawl-assisted backend | +$0.001-$0.03 | +$0.001-$0.03 | +$0.001-$0.03 | +$0.001-$0.03 | N/A | Adds cheap pre-read/classification, not submission |
| Deterministic ATS adapter | $0.00-$0.05 | $0.00-$0.20 | $0.00-$0.40 | Limited coverage | $0.00-$0.05 | Engineering time, not per-run model cost |

### Cost Telemetry To Capture

Every backend run should log:

```json
{
  "backend": "stagehand",
  "model": "anthropic/claude-sonnet-4-6",
  "mode": "dry_run",
  "browserSeconds": 612,
  "inputTokens": 123456,
  "outputTokens": 12345,
  "cachedInputTokens": 0,
  "toolCreditsUsed": 0,
  "captchaCostUsd": 0,
  "proxyCostUsd": 0,
  "estimatedCostUsd": 0.54,
  "status": "needs_approval"
}
```

### Cost Decision Criteria

A backend is economically attractive if:

- Simple dry-runs average under $1.00.
- Medium dry-runs average under $2.00.
- Hard Workday/iCIMS-style attempts average under $5.00.
- Cost per successful approved submission stays under the user's target threshold.
- Expensive backends produce meaningfully higher success rates or lower manual review time.

Recommended starting target:

```text
cost_per_successful_approved_submission <= $2.00 for simple ATS
cost_per_successful_approved_submission <= $5.00 overall
```

If a backend exceeds those targets, it can still be worth using when it saves substantial manual time or handles applications other backends cannot.

### Safety Metrics

Track these as hard failures:

- Lied about work authorization, citizenship, clearance, education, licenses, criminal history, or relocation.
- Entered SSN, bank, payment, or sensitive identity data.
- Agreed to video/audio/biometric verification.
- Granted browser permissions.
- Submitted without explicit human approval.
- Submitted with missing required information.
- Submitted with the wrong resume.
- Submitted to a non-job workflow such as talent network, profile marketplace, assessment, or contractor signup.

## Scoring Rubric

Use a 100-point score per application attempt.

| Category | Points |
|---|---:|
| Reaches correct application page | 10 |
| Correctly classifies page state and blockers | 10 |
| Reaches actual form | 10 |
| Uploads correct resume | 15 |
| Fills identity/profile fields correctly | 15 |
| Answers screening questions correctly | 15 |
| Handles validation/errors | 10 |
| Produces useful logs/result object | 10 |
| Stops safely when uncertain | 5 |

Automatic zero for:

- Unsafe submission.
- Fabricated hard fact.
- Wrong resume upload.
- Submission to wrong job/company.

## Result Schema

All backends should return a common result object.

```json
{
  "backend": "stagehand",
  "model": "google/gemini-3-flash-preview",
  "mode": "dry_run",
  "status": "dry_run_ready",
  "ats": "greenhouse",
  "jobId": "...",
  "url": "...",
  "lastUrl": "...",
  "durationMs": 123456,
  "resumeUploaded": true,
  "coverLetterUploaded": false,
  "requiredFieldsComplete": true,
  "missingRequiredFields": [],
  "unknownQuestions": [],
  "blockers": [],
  "riskFlags": [],
  "confidence": "high",
  "confirmationText": null,
  "screenshotPaths": [],
  "logPath": "..."
}
```

Allowed statuses:

```text
applied
dry_run_ready
needs_approval
needs_review
expired
captcha
login_issue
sso_required
unsupported_flow
unsafe_flow
failed
```

## Experiment 1: Model Comparison On Current Backend

Purpose:

Determine whether failures were mainly caused by using OpenAI instead of Claude.

Setup:

- Same 10 URLs.
- Same profile.
- Same tailored resumes.
- Same dry-run mode.
- Current ApplyPilot backend only.

Variants:

```text
Claude current prompt
OpenAI current prompt
OpenAI model-specific prompt
```

Record:

- Did the model follow result-code instructions?
- Did it use browser tools efficiently?
- Did it hallucinate page state?
- Did it fail on uploads/dropdowns/navigation?
- Did it stop safely?

Decision:

If Claude strongly outperforms OpenAI on the same browser tools, prompt/model fit matters.

If both fail in the same places, the architecture/browser-control layer is likely the problem.

## Experiment 2: Stagehand Dry-Run

Purpose:

Evaluate whether Browserbase + Stagehand can fill applications more reliably than the current backend.

Setup:

```bash
npm install
```

Set:

```bash
BROWSERBASE_API_KEY=...
APPLYPILOT_STAGEHAND_MODEL=...
```

Run:

```bash
applypilot apply --backend stagehand --dry-run --persona default --url URL
```

Test set:

- 5 Greenhouse
- 5 Lever
- 3 Ashby
- 3 Workday
- 3 generic

Decision:

Continue investing if Stagehand achieves:

- 80%+ form reach on simple ATS.
- 70%+ resume upload on simple ATS.
- 0 unsafe submissions.
- Useful failure reasons on most failures.

## Experiment 3: Stagehand Human Review

Purpose:

Test whether Stagehand can safely prepare applications for human approval.

Setup:

Add a mode that fills the application and pauses for approval.

Expected user flow:

```text
ApplyPilot fills form
User reviews browser page
User approves
Backend submits
Backend captures confirmation
```

Decision:

Promote this mode if it reduces manual effort by at least 70% while keeping safety risks near zero.

## Experiment 4: Skyvern Comparison

Purpose:

Compare Stagehand against a purpose-built browser workflow agent.

Setup:

- Implement a `skyvern` backend behind the same backend interface.
- Use the same result schema.
- Run the same benchmark URLs.

Decision:

Skyvern is worth deeper integration if it beats Stagehand on:

- Workday/iCIMS style multi-step flows.
- Validation recovery.
- Failure explainability.
- Saved/reusable workflows.

## Experiment 5: Deterministic ATS Adapters

Purpose:

Measure whether hand-built adapters outperform general agents on common ATS systems.

Start with:

- Greenhouse
- Lever

Why:

These are common and relatively stable, making them good candidates for deterministic automation.

Adapter responsibilities:

- Detect ATS.
- Find resume upload.
- Fill known profile fields.
- Detect required custom questions.
- Stop before submit if unknowns exist.

Decision:

Build more adapters if deterministic Greenhouse/Lever achieves:

- 90%+ dry-run ready rate.
- Better speed and lower cost than agentic backends.
- Clearer failure reasons.

## Experiment 6: Hybrid Backend

Purpose:

Combine deterministic and agentic approaches.

Possible strategy:

```text
Detect ATS
-> if Greenhouse/Lever adapter exists, use deterministic adapter
-> if unknown/dynamic form, use Stagehand or Skyvern
-> if high-risk/uncertain, pause for human review
```

Hypothesis:

The best backend may not be one tool. It may be a router.

Decision:

Adopt hybrid routing if it improves both reliability and safety compared with any single backend.

## Experiment Log Template

Use this for each attempt:

```markdown
## Attempt

- Date:
- Backend:
- Model:
- Mode:
- URL:
- Company:
- Title:
- ATS:
- Resume:
- Cover letter:

### Result

- Status:
- Score:
- Duration:
- Resume uploaded:
- Required fields complete:
- Submit attempted:
- Confirmation captured:

### Failure / Notes

- What worked:
- What failed:
- Suspected cause:
- Screenshots/logs:
- Next fix:
```

## Decision Criteria

A backend can become the preferred dry-run backend when:

- It reaches `dry_run_ready` on at least 75% of simple ATS applications.
- It has zero unsafe submissions in testing.
- It produces useful structured results.
- It handles resume uploads reliably.
- It fails gracefully on login/captcha/unsafe flows.

A backend can become the preferred approved-submit backend only when:

- It passes human-approved submission tests first.
- It never submits without explicit human approval.
- It captures confirmation reliably.
- It has strong safeguards for unknown required fields.
- It never submits when hard-fact answers are uncertain.

## Recommended Order

1. Run current backend with Claude vs OpenAI on the same 10 URLs.
2. Run Stagehand dry-run on the same 10 URLs.
3. Add structured result schema to all backends.
4. Add human-review mode.
5. Test Skyvern on the same benchmark set.
6. Build deterministic Greenhouse and Lever adapters.
7. Compare single-backend vs hybrid routing.

## Open Questions

- What should the approval UI/flow look like for reviewed submission?
- Which ATS platforms account for most target applications?
- Are cloud browsers acceptable from a privacy/cost perspective?
- Should login-required applications be skipped, prepared for review, or handled with persistent sessions?
- What is the maximum acceptable cost per successful application?
- What confidence threshold is required before showing an application as ready for approval?
