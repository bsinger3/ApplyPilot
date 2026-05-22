import fs from "node:fs/promises";
import { Stagehand } from "@browserbasehq/stagehand";

const RESULT_PREFIX = "APPLYPILOT_STAGEHAND_RESULT ";

function emitResult(result) {
  console.log(`${RESULT_PREFIX}${JSON.stringify(result)}`);
}

function compactProfile(profile) {
  const personal = profile.personal || {};
  const workAuth = profile.work_authorization || {};
  const compensation = profile.compensation || {};
  const experience = profile.experience || {};
  const eeo = profile.eeo_voluntary || {};

  return {
    fullName: personal.full_name,
    preferredName: personal.preferred_name,
    email: personal.email,
    phone: personal.phone,
    address: personal.address,
    city: personal.city,
    state: personal.province_state,
    country: personal.country,
    postalCode: personal.postal_code,
    linkedinUrl: personal.linkedin_url,
    githubUrl: personal.github_url,
    portfolioUrl: personal.portfolio_url || personal.website_url,
    workAuthorization: workAuth.legally_authorized_to_work,
    sponsorshipRequired: workAuth.require_sponsorship,
    workPermitType: workAuth.work_permit_type,
    salaryExpectation: compensation.salary_expectation,
    salaryCurrency: compensation.salary_currency || "USD",
    totalYearsExperience: experience.years_of_experience_total,
    educationLevel: experience.education_level,
    gender: eeo.gender || "Decline to self-identify",
    raceEthnicity: eeo.race_ethnicity || "Decline to self-identify",
    veteranStatus: eeo.veteran_status || "I am not a protected veteran",
    disabilityStatus: eeo.disability_status || "I do not wish to answer",
  };
}

async function tryUploadVisibleFiles(page, payload) {
  const fileInputs = page.locator('input[type="file"]');
  const count = await fileInputs.count();
  const uploads = [];

  for (let i = 0; i < count; i += 1) {
    const input = fileInputs.nth(i);
    const label = await input.evaluate((el) => {
      const aria = el.getAttribute("aria-label") || "";
      const name = el.getAttribute("name") || "";
      const id = el.getAttribute("id") || "";
      const accept = el.getAttribute("accept") || "";
      return `${aria} ${name} ${id} ${accept}`.toLowerCase();
    }).catch(() => "");

    const wantsCoverLetter = label.includes("cover") && payload.coverLetterPdfPath;
    const filePath = wantsCoverLetter ? payload.coverLetterPdfPath : payload.resumePdfPath;
    if (!filePath) {
      continue;
    }

    try {
      await input.setInputFiles(filePath);
      uploads.push({ index: i, filePath, label });
    } catch (error) {
      uploads.push({ index: i, filePath, label, error: String(error.message || error) });
    }
  }

  return uploads;
}

async function run() {
  const payloadPath = process.argv[2];
  if (!payloadPath) {
    throw new Error("Usage: npm run stagehand:apply -- <payload.json>");
  }
  if (!process.env.BROWSERBASE_API_KEY) {
    throw new Error("BROWSERBASE_API_KEY is required for the Stagehand backend.");
  }

  const payload = JSON.parse(await fs.readFile(payloadPath, "utf8"));
  const profile = compactProfile(payload.profile || {});
  const model = process.env.APPLYPILOT_STAGEHAND_MODEL || "google/gemini-3-flash-preview";
  const stagehand = new Stagehand({
    env: "BROWSERBASE",
    apiKey: process.env.BROWSERBASE_API_KEY,
    model,
    disablePino: true,
  });

  let lastUrl = "";
  let uploads = [];

  try {
    await stagehand.init();
    const page = stagehand.page || stagehand.context.pages()[0];
    await page.goto(payload.url, { waitUntil: "domcontentloaded", timeout: 60000 });
    lastUrl = page.url();

    const pageState = await stagehand.extract(
      "Classify this page. Return the job application status, whether an Apply button or form is present, whether the job appears closed, and any login, captcha, or location eligibility blockers."
    );

    const stateText = JSON.stringify(pageState || {}).toLowerCase();
    if (stateText.includes("closed") || stateText.includes("no longer accepting")) {
      emitResult({ status: "expired", reason: "job_closed", lastUrl, pageState, uploads });
      return;
    }
    if (stateText.includes("captcha")) {
      emitResult({ status: "captcha", reason: "captcha_detected", lastUrl, pageState, uploads });
      return;
    }

    await stagehand.act(
      "Find and click the primary Apply, Apply Now, Start Application, or Continue button. If the application form is already visible, do nothing."
    );
    await page.waitForLoadState("domcontentloaded", { timeout: 15000 }).catch(() => {});
    lastUrl = page.url();

    uploads = await tryUploadVisibleFiles(page, payload);

    await stagehand.act(
      `Fill the job application form with this applicant profile: ${JSON.stringify(profile)}.
Use the resume text only as source material for work-history and screening answers.
Answer hard-fact questions truthfully. For EEO/demographic questions, use the profile-provided answers when available.
Do not grant browser permissions, do not create marketplace profiles, do not do video/audio/ID verification,
and do not enter SSN, bank, or payment information.`
    );

    uploads = uploads.concat(await tryUploadVisibleFiles(page, payload));

    if (payload.coverLetterText) {
      await stagehand.act(
        `If there is a cover-letter text area, paste this cover letter: ${payload.coverLetterText}`
      );
    }

    const review = await stagehand.extract(
      "Review the visible page and return: required fields still missing, validation errors, login/captcha blockers, whether the resume appears uploaded, and whether the page is ready for final submission."
    );

    emitResult({
      status: payload.dryRun ? "dry_run_ready" : "needs_approval",
      reason: payload.dryRun ? "stopped_before_submit" : "human_approval_required",
      lastUrl,
      pageState,
      review,
      uploads,
    });
  } catch (error) {
    emitResult({
      status: "failed",
      reason: String(error.message || error).slice(0, 500),
      lastUrl,
      uploads,
    });
  } finally {
    await stagehand.close().catch(() => {});
  }
}

await run();
