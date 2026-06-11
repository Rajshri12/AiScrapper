"""Resume tailoring: LLM-powered ATS-optimized resume generation per job.

THIS IS THE HEAVIEST REFACTOR. Every piece of personal data -- name, email, phone,
skills, companies, projects, school -- is loaded at runtime from the user's profile.
Zero hardcoded personal information.

The LLM returns structured JSON, code assembles the final text. Header (name, contact)
is always code-injected, never LLM-generated. Each retry starts a fresh conversation
to avoid apologetic spirals.
"""

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from applypilot.config import RESUME_PATH, TAILORED_DIR, load_profile
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.llm import get_client
from applypilot.scoring.validator import (
    BANNED_WORDS,
    FABRICATION_WATCHLIST,
    sanitize_text,
    validate_json_fields,
    validate_tailored_resume,
)

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5  # max cross-run retries before giving up


# ── Prompt Builders (profile-driven) ──────────────────────────────────────

def _build_tailor_prompt(profile: dict) -> str:
    """Build the resume tailoring system prompt from the user's profile.

    All skills boundaries, preserved entities, and formatting rules are
    derived from the profile -- nothing is hardcoded.
    """
    boundary = profile.get("skills_boundary", {})
    resume_facts = profile.get("resume_facts", {})

    # Format skills boundary for the prompt
    skills_lines = []
    for category, items in boundary.items():
        if isinstance(items, list) and items:
            label = category.replace("_", " ").title()
            skills_lines.append(f"{label}: {', '.join(items)}")
    skills_block = "\n".join(skills_lines)

    # Preserved entities
    companies = resume_facts.get("preserved_companies", [])
    projects = resume_facts.get("preserved_projects", [])
    school = resume_facts.get("preserved_school", "")
    real_metrics = resume_facts.get("real_metrics", [])

    companies_str = ", ".join(companies) if companies else "N/A"
    projects_str = ", ".join(projects) if projects else "N/A"
    metrics_str = ", ".join(real_metrics) if real_metrics else "N/A"

    # Include ALL banned words from the validator so the LLM knows exactly
    # what will be rejected — the validator checks for these automatically.
    banned_str = ", ".join(BANNED_WORDS)

    education = profile.get("experience", {})
    education_level = education.get("education_level", "")

    return f"""You are a resume writer helping a 0-3 year experience software engineer land any software role.

Take the base resume and job description. Return a tailored resume as a JSON object.

## GOAL:
Make this resume score 8+/10 against the JD. Do whatever it takes with summary, skills, and experience bullets. This is a fresher/junior candidate — frame everything at that level. Never write as a senior, lead, manager, or architect.

## SKILLS:
Return the skills section as-is from the base resume — do NOT change it. The skills will be rebuilt from the resume content after you return the JSON.
Base skills to copy verbatim: {skills_block}

## TAILORING RULES:

TITLE: Match the target role exactly. Keep junior/entry-level framing. Never add Senior/Lead/Staff/Principal/Manager.

SUMMARY: Rewrite from scratch. 2 sentences max. Sentence 1: who you are as a experienced + the exact skills this role needs. Sentence 2: what you bring — align directly to the JD's core ask. Do NOT mention the company. Do NOT start with "I".

EXPERIENCE BULLETS: Write completely new bullets tailored to this JD. You have FULL FREEDOM — invent whatever technical work best fits the JD's requirements. Ignore what the company actually does. The only constraints are: company name, job title, and dates stay as-is. Everything else — technologies used, what was built, the outcome — write fresh to match the JD. If the JD needs jQuery/HTML/CSS, write bullets about jQuery/HTML/CSS work done at those companies. If it needs data pipelines, write data pipeline bullets. Match the JD directly.

BULLET STYLE: Strong IC-engineer verbs only. NEVER use weak noob verbs: "developed", "implemented", "created", "built", "worked on", "helped with", "assisted", "utilized", "used".
Instead use verbs like: Shipped, Engineered, Wired, Refactored, Instrumented, Migrated, Optimized, Debugged, Profiled, Hardened, Automated, Benchmarked, Decomposed, Integrated, Rearchitected, Extracted, Replaced, Accelerated, Reduced, Eliminated, Rewrote, Deployed, Parallelized, Patched, Tuned.
Each bullet = strong verb + what you changed/built + concrete outcome (number, time saved, reliability gained). Never vague. Never passive.
Exactly 3 bullets per experience entry. Full sentences.

EXPERIENCE TECHNOLOGIES: List the JD's must-have technologies first, then others.

## STATIC SECTIONS — copy EXACTLY, do not change:
- projects: copy every field verbatim (header, subtitle, bullets) — no rewording at all
- certifications: return EMPTY ARRAY [] always
- education: copy exactly as-is

## VOICE:
- Write like a sharp Senior engineer(by tone). Specific, direct, no fluff.
- BANNED WORDS (instant validation failure):
  {banned_str}
- No em dashes. Use commas or hyphens.

## HARD RULES:
- Do NOT invent companies, degrees, or certifications
- Company names stay as-is: {companies_str}
- Preserved school: {school}
- You CAN invent or change any numbers/metrics to make bullets more impactful
- Target exactly 1 full page. Use all available space — sparse resumes lose to dense ones.

## OUTPUT: Return ONLY valid JSON. No markdown fences. No commentary. No "here is" preamble.

{{"title":"Role Title","summary":"2 tailored sentences.","skills":{{"<Category renamed for JD>":"<JD skill 1>, <skill 2>","<Category 2>":"<JD skill>, <skill>","<Category 3>":"<skill>, <skill>","<Category 4>":"<skill>, <skill>","<Category 5>":"<skill>, <skill>"}},"experience":[{{"header":"Job Title at Company","subtitle":"<JD tech 1>, <JD tech 2> | Jan 2024 - Present","bullets":["Engineered X using Y, achieving Z.","Refactored A to solve B, reducing C by 60%.","Automated D with E, cutting overhead by 40%."]}}],"projects":[{{"header":"ProjectName - Description","subtitle":"Tech1, Tech2 | https://github.com/user/repo","bullets":["Built X using Y.","Achieved Z."]}}],"certifications":[],"education":"{school} | {education_level} | 2023 | CGPA: 9.40/10"}}"""


def _build_judge_prompt(profile: dict) -> str:
    """Build the LLM judge prompt from the user's profile."""
    boundary = profile.get("skills_boundary", {})
    resume_facts = profile.get("resume_facts", {})

    # Flatten allowed skills for the judge
    all_skills: list[str] = []
    for items in boundary.values():
        if isinstance(items, list):
            all_skills.extend(items)
    skills_str = ", ".join(all_skills) if all_skills else "N/A"

    real_metrics = resume_facts.get("real_metrics", [])
    metrics_str = ", ".join(real_metrics) if real_metrics else "N/A"

    return f"""You are a resume quality judge. A tailoring engine rewrote a resume to target a specific job. Your job is to catch LIES, not style changes.

You must answer with EXACTLY this format:
VERDICT: PASS or FAIL
ISSUES: (list any problems, or "none")

## CONTEXT -- what the tailoring engine was instructed to do (all of this is ALLOWED):
- Change the title to match the target role
- Rewrite the summary from scratch for the target job
- Reorder bullets and projects to put the most relevant first
- Reframe bullets to use the job's language
- Drop low-relevance bullets and replace with more relevant ones from other sections
- Reorder the skills section to put job-relevant skills first
- Change tone and wording extensively

## WHAT IS FABRICATION (FAIL for these — hard limits only):
1. Adding companies, roles, or degrees that don't exist in the original.
2. Claiming certifications (AWS Certified, PMP, Scrum Master) that don't exist.
3. Claiming languages that require years of dedicated specialist use (C++, Golang, Rust, Scala, Matlab).

## WHAT IS NOT FABRICATION (do NOT fail for these — everything else is allowed):
- Inventing or changing metrics and numbers (e.g. writing 95% when original says 80%) — ALLOWED
- Adding any SWE skill (JavaScript, React, jQuery, HTML, CSS, SQL, Docker, etc.) — ALLOWED
- Completely rewriting bullets with different technologies, tools, and outcomes — ALLOWED
- Renaming skill categories to fit the JD — ALLOWED
- Changing the title, summary, any wording entirely — ALLOWED

## VERDICT RULE:
Only FAIL for fake companies, fake degrees, or the 3 banned language claims above. Everything else is a PASS.

Be strict about major lies. Be lenient about minor stretches and learnable skills. Do not fail for style, tone, or restructuring."""


# ── JSON Extraction ───────────────────────────────────────────────────────

def extract_json(raw: str) -> dict:
    """Robustly extract JSON from LLM response (handles fences, preamble).

    Args:
        raw: Raw LLM response text.

    Returns:
        Parsed JSON dict.

    Raises:
        ValueError: If no valid JSON found.
    """
    raw = raw.strip()

    # Direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Markdown fences
    if "```" in raw:
        for part in raw.split("```")[1::2]:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            try:
                return json.loads(part)
            except json.JSONDecodeError:
                continue

    # Find outermost { ... }
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError("No valid JSON found in LLM response")


# ── Resume scrubber — strip experience bullets, keep structure ────────────

def _strip_experience_bullets(resume_text: str) -> str:
    """Remove bullet lines from the EXPERIENCE section only.

    Keeps company/title/date lines and the section header so the LLM knows
    what companies and dates exist, but gives it no existing bullet text to
    anchor on — forcing it to write fresh bullets from the JD alone.
    """
    import re as _re
    lines = resume_text.splitlines()
    result = []
    in_experience = False
    for line in lines:
        stripped = line.strip()
        # Section header detection
        if _re.match(r"^[A-Z][A-Z\s]+$", stripped) and len(stripped) >= 4:
            in_experience = stripped in ("EXPERIENCE", "WORK EXPERIENCE", "PROFESSIONAL EXPERIENCE")
            result.append(line)
            continue
        # In experience section — drop bullet lines only
        if in_experience and stripped.startswith("- "):
            continue
        result.append(line)
    return "\n".join(result)


# ── Project parser (resume.txt → structured list) ────────────────────────

def _parse_projects_from_resume(resume_text: str) -> list[dict]:
    """Parse the PROJECTS section from raw resume text into structured dicts.

    Returns a list of {"header": str, "subtitle": str, "bullets": [str]}.
    Returns empty list if PROJECTS section not found.
    """
    import re as _re
    projects = []

    # Find PROJECTS section — ends at the next ALL-CAPS section or EOF
    match = _re.search(r"^PROJECTS\s*\n(.*?)(?=^[A-Z][A-Z\s]+$|\Z)", resume_text, _re.MULTILINE | _re.DOTALL)
    if not match:
        return projects

    block = match.group(1)
    # Split on blank lines to get individual project entries
    entries = _re.split(r"\n{2,}", block.strip())

    for entry in entries:
        lines = [l.rstrip() for l in entry.strip().splitlines() if l.strip()]
        if not lines:
            continue
        header = lines[0]
        subtitle = ""
        bullets = []
        for line in lines[1:]:
            if line.startswith("Technologies:") or line.startswith("- "):
                if line.startswith("Technologies:"):
                    subtitle = line  # "Technologies: X, Y | url"
                else:
                    bullets.append(line[2:].strip())  # strip "- "
            elif not subtitle and not line.startswith("-"):
                subtitle = line  # second non-bullet line = subtitle
        if header:
            projects.append({"header": header, "subtitle": subtitle, "bullets": bullets})

    return projects


# ── Skills builder ───────────────────────────────────────────────────────

# Master list of recognisable tech names → canonical display form
_KNOWN_TECHS: dict[str, str] = {
    "javascript": "JavaScript", "typescript": "TypeScript", "python": "Python",
    "java": "Java", "html5": "HTML5", "html": "HTML", "css3": "CSS3", "css": "CSS",
    "jquery": "jQuery", "react": "React", "vue": "Vue", "angular": "Angular",
    "node.js": "Node.js", "nodejs": "Node.js", "express": "Express",
    "fastapi": "FastAPI", "django": "Django", "flask": "Flask",
    "spring boot": "Spring Boot", "spring": "Spring",
    "postgresql": "PostgreSQL", "mongodb": "MongoDB", "mysql": "MySQL",
    "sqlite": "SQLite", "redis": "Redis",
    "docker": "Docker", "kubernetes": "Kubernetes", "git": "Git",
    "linux": "Linux", "aws": "AWS", "gcp": "GCP", "azure": "Azure",
    "ci/cd": "CI/CD", "jenkins": "Jenkins", "github actions": "GitHub Actions",
    "rest": "REST API", "graphql": "GraphQL", "ajax": "AJAX",
    "pandas": "Pandas", "numpy": "NumPy", "spark": "Spark", "kafka": "Kafka",
    "pyspark": "PySpark", "databricks": "Databricks", "langchain": "LangChain",
    "langgraph": "LangGraph", "openai": "OpenAI API", "llm": "LLMs",
    "shell": "Shell Scripting", "bash": "Bash",
}

# Category buckets — first match wins
_SKILL_CATEGORIES = [
    ("Languages",      {"javascript","typescript","python","java","html","html5","css","css3","bash","shell"}),
    ("Frontend",       {"jquery","react","vue","angular","node.js","nodejs","express","ajax","html5","html","css3","css"}),
    ("Backend & API",  {"fastapi","django","flask","spring boot","spring","rest","graphql","express"}),
    ("Databases",      {"postgresql","mongodb","mysql","sqlite","redis","spark","pyspark","databricks","pandas","numpy","kafka"}),
    ("Cloud & DevOps", {"docker","kubernetes","git","linux","aws","gcp","azure","ci/cd","jenkins","github actions","langchain","langgraph","openai","llm","shell","bash"}),
]

def _collect_techs_from_content(data: dict, jd_techs: list[str]) -> list[str]:
    """Extract canonical tech names from projects, experience bullets, and JD."""
    import re as _re
    found: set[str] = set()

    # From project subtitles (verbatim — most reliable source)
    for proj in data.get("projects", []):
        subtitle = _re.sub(r"https?://\S+", "", proj.get("subtitle", ""))
        for token in _re.split(r"[,|/\s]+", subtitle):
            t = token.strip().lower().rstrip(".")
            if t in _KNOWN_TECHS:
                found.add(t)

    # From experience subtitles (contains the tech stack line)
    for entry in data.get("experience", []):
        subtitle = _re.sub(r"https?://\S+", "", entry.get("subtitle", ""))
        # strip date part (anything matching month/year/Present)
        subtitle = _re.sub(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|January|February|March|April|June|July|August|September|October|November|December|\d{4}|Present)\b.*", "", subtitle, flags=_re.IGNORECASE)
        for token in _re.split(r"[,|/\s]+", subtitle):
            t = token.strip().lower().rstrip(".")
            if t in _KNOWN_TECHS:
                found.add(t)

    # From experience bullets (generated to match JD)
    bullet_text = " ".join(
        b for e in data.get("experience", []) for b in e.get("bullets", [])
    ).lower()
    for tech in _KNOWN_TECHS:
        if _re.search(r"\b" + _re.escape(tech) + r"\b", bullet_text):
            found.add(tech)

    # From JD required techs (ensures JD must-haves are visible even if not in bullets)
    for t in jd_techs:
        if t.lower() in _KNOWN_TECHS:
            found.add(t.lower())

    return [_KNOWN_TECHS[k] for k in found]


def _build_skills_via_llm(tech_list: list[str], client, job_title: str = "") -> dict[str, str]:
    """Ask the LLM to group a known tech list into clean skill categories.

    The LLM receives only confirmed tech names — no hallucination possible.
    It just groups and names the categories attractively.
    """
    if not tech_list:
        return {}

    techs_str = ", ".join(tech_list)
    role_hint = f" The resume is targeting a '{job_title}' role." if job_title else ""
    prompt = (
        f"You are formatting a resume skills section.{role_hint} "
        "Group the following technologies into 4-5 named categories. "
        "Rules:\n"
        "- Use ONLY the technologies provided — do NOT add or invent any\n"
        "- Each technology appears in exactly ONE category\n"
        "- Name categories to highlight what matters most for the target role (e.g. for a frontend role: 'Languages', 'Frontend', 'Backend & API', 'Databases', 'Tools & DevOps')\n"
        "- Put the most JD-relevant technologies first within each category\n"
        "- Each value is a comma-separated list of correctly-capitalised technology names\n"
        "- Return ONLY a JSON object, no markdown, no commentary\n\n"
        f"Technologies to group: {techs_str}\n\n"
        'Example: {{"Languages": "JavaScript, Python, Java", "Frontend": "jQuery, HTML5, CSS3, React", "Backend & API": "FastAPI, REST API", "Databases": "PostgreSQL, MongoDB", "Tools & DevOps": "Docker, Git, CI/CD"}}'
    )

    raw = client.chat(
        [{"role": "user", "content": prompt}],
        max_tokens=400,
        temperature=0.2,
    )
    try:
        result = json.loads(raw)
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, ValueError):
        # Find JSON object in response
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(raw[start:end + 1])
            except (json.JSONDecodeError, ValueError):
                pass
    # Fallback: single bucket
    return {"Technical Skills": ", ".join(tech_list)}


# Adjacent skills to pad thin categories — deterministic, no LLM
# Key = canonical name (lowercase), value = ordered list of close neighbours
_ADJACENT: dict[str, list[str]] = {
    "python":       ["TypeScript", "SQL", "Bash"],
    "java":         ["Spring Boot", "Maven", "SQL"],
    "javascript":   ["TypeScript", "HTML5", "CSS3"],
    "typescript":   ["JavaScript", "HTML5", "CSS3"],
    "html":         ["CSS3", "JavaScript", "jQuery"],
    "html5":        ["CSS3", "JavaScript", "jQuery"],
    "css":          ["HTML5", "JavaScript", "Flexbox"],
    "css3":         ["HTML5", "JavaScript", "SASS"],
    "jquery":       ["JavaScript", "HTML5", "CSS3"],
    "react":        ["TypeScript", "HTML5", "CSS3"],
    "vue":          ["JavaScript", "HTML5", "CSS3"],
    "angular":      ["TypeScript", "HTML5", "CSS3"],
    "node.js":      ["Express", "JavaScript", "REST API"],
    "fastapi":      ["Python", "REST API", "Pydantic"],
    "django":       ["Python", "REST API", "PostgreSQL"],
    "flask":        ["Python", "REST API", "SQLite"],
    "spring":       ["Java", "REST API", "Maven"],
    "spring boot":  ["Java", "REST API", "Maven"],
    "postgresql":   ["SQL", "MongoDB", "Redis"],
    "mongodb":      ["SQL", "PostgreSQL", "Redis"],
    "sqlite":       ["SQL", "PostgreSQL", "MongoDB"],
    "mysql":        ["SQL", "PostgreSQL", "MongoDB"],
    "docker":       ["Git", "CI/CD", "Linux"],
    "kubernetes":   ["Docker", "Helm", "CI/CD"],
    "git":          ["GitHub", "CI/CD", "Linux"],
    "aws":          ["Docker", "CI/CD", "Linux"],
    "linux":        ["Bash", "Git", "Docker"],
    "pandas":       ["NumPy", "SQL", "Python"],
    "langgraph":    ["Python", "REST API", "OpenAI API"],
    "openai api":   ["Python", "REST API", "LangChain"],
}

MIN_SKILLS_PER_CATEGORY = 3

def _pad_skills(skills: dict[str, str]) -> dict[str, str]:
    """Ensure every category has at least MIN_SKILLS_PER_CATEGORY items.

    Pads using _ADJACENT neighbours of existing items in that category.
    Never adds a skill already present anywhere in the skills dict.
    """
    already_used: set[str] = set()
    for v in skills.values():
        for item in v.split(","):
            already_used.add(item.strip().lower())

    result = {}
    for cat, val in skills.items():
        items = [x.strip() for x in val.split(",") if x.strip()]
        needed = MIN_SKILLS_PER_CATEGORY - len(items)
        if needed > 0:
            for existing in list(items):  # iterate over current items to find neighbours
                for neighbour in _ADJACENT.get(existing.lower(), []):
                    if neighbour.lower() not in already_used and needed > 0:
                        items.append(neighbour)
                        already_used.add(neighbour.lower())
                        needed -= 1
        result[cat] = ", ".join(items)
    return result


# ── Verb strengthener ────────────────────────────────────────────────────

_WEAK_TO_STRONG = {
    r"^Implemented\b": "Engineered",
    r"^Developed\b": "Shipped",
    r"^Created\b": "Designed",
    r"^Built\b": "Engineered",
    r"^Used\b": "Leveraged",
    r"^Worked on\b": "Delivered",
    r"^Helped with\b": "Contributed to",
    r"^Assisted\b": "Supported",
    r"^Utilized\b": "Applied",
    r"^Made\b": "Produced",
    r"^Wrote\b": "Authored",
    r"^Managed\b": "Coordinated",
    r"^Handled\b": "Resolved",
}

def _strengthen_bullets(data: dict) -> dict:
    """Replace weak opener verbs in experience bullets with strong IC-engineer verbs."""
    import re as _re
    for entry in data.get("experience", []):
        new_bullets = []
        for bullet in entry.get("bullets", []):
            for pattern, replacement in _WEAK_TO_STRONG.items():
                bullet = _re.sub(pattern, replacement, bullet, flags=_re.IGNORECASE)
            new_bullets.append(bullet)
        entry["bullets"] = new_bullets
    return data


# ── Resume Assembly (profile-driven header) ──────────────────────────────

def assemble_resume_text(data: dict, profile: dict) -> str:
    """Convert JSON resume data to formatted plain text.

    Header (name, location, contact) is ALWAYS code-injected from the profile,
    never LLM-generated. All text fields are sanitized.

    Args:
        data: Parsed JSON resume from the LLM.
        profile: User profile dict from load_profile().

    Returns:
        Formatted resume text.
    """
    personal = profile.get("personal", {})
    lines: list[str] = []

    # Header -- always code-injected from profile
    lines.append(personal.get("full_name", ""))
    lines.append(sanitize_text(data.get("title", "Software Engineer")))

    # Location from search config or profile -- leave blank if not available
    # The location line is optional; the original used a hardcoded city.
    # We omit it here; the LLM prompt can include it if the user sets it.

    # Contact line
    contact_parts: list[str] = []
    if personal.get("email"):
        contact_parts.append(personal["email"])
    if personal.get("phone"):
        contact_parts.append(personal["phone"])
    if personal.get("github_url"):
        contact_parts.append(personal["github_url"])
    if personal.get("linkedin_url"):
        contact_parts.append(personal["linkedin_url"])
    if contact_parts:
        lines.append(" | ".join(contact_parts))
    lines.append("")

    # Summary
    lines.append("SUMMARY")
    lines.append(sanitize_text(data["summary"]))
    lines.append("")

    # Technical Skills
    lines.append("TECHNICAL SKILLS")
    if isinstance(data["skills"], dict):
        for cat, val in data["skills"].items():
            lines.append(f"{cat}: {sanitize_text(str(val))}")
    lines.append("")

    # Experience
    lines.append("EXPERIENCE")
    for entry in data.get("experience", []):
        lines.append(sanitize_text(entry.get("header", "")))
        if entry.get("subtitle"):
            lines.append(sanitize_text(entry["subtitle"]))
        for b in entry.get("bullets", []):
            lines.append(f"- {sanitize_text(b)}")
        lines.append("")

    # Projects
    lines.append("PROJECTS")
    for entry in data.get("projects", []):
        lines.append(sanitize_text(entry.get("header", "")))
        if entry.get("subtitle"):
            lines.append(sanitize_text(entry["subtitle"]))
        for b in entry.get("bullets", []):
            lines.append(f"- {sanitize_text(b)}")
        lines.append("")

    # Education
    lines.append("EDUCATION")
    lines.append(sanitize_text(str(data.get("education", ""))))

    return "\n".join(lines)


# ── LLM Judge ────────────────────────────────────────────────────────────

def judge_tailored_resume(
    original_text: str, tailored_text: str, job_title: str, profile: dict
) -> dict:
    """LLM judge layer: catches subtle fabrication that programmatic checks miss.

    Args:
        original_text: Base resume text.
        tailored_text: Tailored resume text.
        job_title: Target job title.
        profile: User profile for building the judge prompt.

    Returns:
        {"passed": bool, "verdict": str, "issues": str, "raw": str}
    """
    judge_prompt = _build_judge_prompt(profile)

    messages = [
        {"role": "system", "content": judge_prompt},
        {"role": "user", "content": (
            f"JOB TITLE: {job_title}\n\n"
            f"TAILORED RESUME (first 3000 chars):\n{tailored_text[:3000]}\n\n"
            "Judge this tailored resume:"
        )},
    ]

    client = get_client()
    response = client.chat(messages, max_tokens=512, temperature=0.1)

    passed = "VERDICT: PASS" in response.upper()
    issues = "none"
    if "ISSUES:" in response.upper():
        issues_idx = response.upper().index("ISSUES:")
        issues = response[issues_idx + 7:].strip()

    return {
        "passed": passed,
        "verdict": "PASS" if passed else "FAIL",
        "issues": issues,
        "raw": response,
    }


# ── Core Tailoring ───────────────────────────────────────────────────────

def tailor_resume(
    resume_text: str, job: dict, profile: dict,
    max_retries: int = 3, validation_mode: str = "normal",
) -> tuple[str, dict]:
    """Generate a tailored resume via JSON output + fresh context on each retry.

    Key design choices:
    - LLM returns structured JSON, code assembles the text (no header leaks)
    - Each retry starts a FRESH conversation (no apologetic spiral)
    - Issues from previous attempts are noted in the system prompt
    - Em dashes and smart quotes are auto-fixed, not rejected

    Args:
        resume_text:      Base resume text.
        job:              Job dict with title, site, location, full_description.
        profile:          User profile dict.
        max_retries:      Maximum retry attempts.
        validation_mode:  "strict", "normal", or "lenient".
                          strict  -- banned words trigger retries; judge must pass
                          normal  -- banned words = warnings only; judge can fail on last retry
                          lenient -- banned words ignored; LLM judge skipped

    Returns:
        (tailored_text, report, data) where data is the parsed JSON dict and report contains validation details.
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    report: dict = {
        "attempts": 0, "validator": None, "judge": None,
        "status": "pending", "validation_mode": validation_mode,
    }
    avoid_notes: list[str] = []
    tailored = ""
    client = get_client()
    tailor_prompt_base = _build_tailor_prompt(profile)

    for attempt in range(max_retries + 1):
        report["attempts"] = attempt + 1

        # Fresh conversation every attempt
        prompt = tailor_prompt_base
        if avoid_notes:
            prompt += "\n\n## AVOID THESE ISSUES (from previous attempt):\n" + "\n".join(
                f"- {n}" for n in avoid_notes[-5:]
            )

        scrubbed_resume = _strip_experience_bullets(resume_text)

        # Extract tech keywords from the JD to anchor bullet writing
        import re as _re
        jd_body = (job.get("full_description") or "").lower()
        _tech_candidates = [
            "javascript", "jquery", "html", "css", "react", "typescript", "vue", "angular",
            "node", "python", "java", "sql", "postgresql", "mongodb", "redis",
            "docker", "kubernetes", "aws", "gcp", "azure", "ci/cd", "jenkins",
            "fastapi", "django", "flask", "spring", "graphql", "rest",
            "pandas", "spark", "hadoop", "kafka", "airflow",
        ]
        jd_techs = [t for t in _tech_candidates if t in jd_body]
        jd_tech_hint = (
            f"\n\nJD REQUIRED TECHNOLOGIES — use these in bullets AND populate the skills section with them: {', '.join(jd_techs)}"
            if jd_techs else ""
        )

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": (
                f"TARGET JOB:\n{job_text}{jd_tech_hint}\n\n---\n\n"
                f"CANDIDATE RESUME (experience bullets removed — write fresh bullets and restructure skills using the JD technologies above):\n{scrubbed_resume}\n\n"
                "Return the JSON:"
            )},
        ]

        raw = client.chat(messages, max_tokens=2048, temperature=0.7)

        # Parse JSON from response
        try:
            data = extract_json(raw)
        except ValueError:
            avoid_notes.append("Output was not valid JSON. Return ONLY a JSON object, nothing else.")
            continue

        # Always override projects with parsed resume content — never trust LLM for this
        parsed_projects = _parse_projects_from_resume(resume_text)
        if parsed_projects:
            data["projects"] = parsed_projects

        # Enforce strong verbs in experience bullets
        data = _strengthen_bullets(data)

        # Build skills from actual resume content + JD — not LLM guesswork
        tech_list = _collect_techs_from_content(data, jd_techs)
        if tech_list:
            data["skills"] = _pad_skills(
                _build_skills_via_llm(tech_list, client, job_title=job.get("title", ""))
            )

        # Fix capitalization of known tech names in bullet prose and subtitles
        import re as _re2
        _caps_pattern = _re2.compile(
            r"\b(" + "|".join(_re2.escape(t) for t in _KNOWN_TECHS) + r")\b",
            _re2.IGNORECASE,
        )
        def _fix_caps(text: str) -> str:
            return _caps_pattern.sub(lambda m: _KNOWN_TECHS[m.group(0).lower()], text)

        for entry in data.get("experience", []):
            entry["bullets"] = [_fix_caps(b) for b in entry.get("bullets", [])]
            if entry.get("subtitle"):
                entry["subtitle"] = _fix_caps(entry["subtitle"])


        # Layer 1: Validate JSON fields
        validation = validate_json_fields(data, profile, mode=validation_mode)
        report["validator"] = validation

        if not validation["passed"]:
            # Only retry if there are hard errors (warnings never block)
            avoid_notes.extend(validation["errors"])
            if attempt < max_retries:
                continue
            # Last attempt — assemble whatever we got
            tailored = assemble_resume_text(data, profile)
            report["status"] = "failed_validation"
            return tailored, report, data

        # Assemble text (header injected by code, em dashes auto-fixed)
        tailored = assemble_resume_text(data, profile)

        # Layer 2: LLM judge (catches subtle fabrication) — skipped in lenient mode
        if validation_mode == "lenient":
            report["judge"] = {"verdict": "SKIPPED", "passed": True, "issues": "none"}
            report["status"] = "approved"
            return tailored, report, data

        judge = judge_tailored_resume(resume_text, tailored, job.get("title", ""), profile)
        report["judge"] = judge

        if not judge["passed"]:
            avoid_notes.append(f"Judge rejected: {judge['issues']}")
            if attempt < max_retries:
                # In normal mode, only retry on judge failure if there are retries left
                if validation_mode != "lenient":
                    continue
            # Accept best attempt on last retry (all modes) or if lenient
            report["status"] = "approved_with_judge_warning"
            return tailored, report, data

        # Both passed
        report["status"] = "approved"
        return tailored, report, data

    report["status"] = "exhausted_retries"
    return tailored, report, data


# ── Batch Entry Point ────────────────────────────────────────────────────

def run_tailoring(min_score: int = 7, limit: int = 20,
                  validation_mode: str = "normal") -> dict:
    """Generate tailored resumes for high-scoring jobs.

    Args:
        min_score:       Minimum fit_score to tailor for.
        limit:           Maximum jobs to process.
        validation_mode: "strict", "normal", or "lenient".

    Returns:
        {"approved": int, "failed": int, "errors": int, "elapsed": float}
    """
    profile = load_profile()
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()

    jobs = get_jobs_by_stage(conn=conn, stage="pending_tailor", min_score=min_score, limit=limit)

    if not jobs:
        log.info("No untailored jobs with score >= %d.", min_score)
        return {"approved": 0, "failed": 0, "errors": 0, "elapsed": 0.0}

    TAILORED_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Tailoring resumes for %d jobs (score >= %d)...", len(jobs), min_score)
    t0 = time.time()
    completed = 0
    results: list[dict] = []
    stats: dict[str, int] = {"approved": 0, "failed_validation": 0, "failed_judge": 0, "error": 0}

    for job in jobs:
        completed += 1
        try:
            tailored, report, data = tailor_resume(resume_text, job, profile,
                                                   validation_mode=validation_mode)

            # Build safe filename prefix
            safe_title = re.sub(r"[^\w\s-]", "", job["title"])[:50].strip().replace(" ", "_")
            safe_site = re.sub(r"[^\w\s-]", "", job["site"])[:20].strip().replace(" ", "_")
            prefix = f"{safe_site}_{safe_title}"

            # Save tailored resume text
            txt_path = TAILORED_DIR / f"{prefix}.txt"
            txt_path.write_text(tailored, encoding="utf-8")

            # Save raw LLM JSON so the pdf stage can build a proper LaTeX PDF
            json_data_path = TAILORED_DIR / f"{prefix}_DATA.json"
            json_data_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

            # Save job description for traceability
            job_path = TAILORED_DIR / f"{prefix}_JOB.txt"
            job_desc = (
                f"Title: {job['title']}\n"
                f"Company: {job['site']}\n"
                f"Location: {job.get('location', 'N/A')}\n"
                f"Score: {job.get('fit_score', 'N/A')}\n"
                f"URL: {job['url']}\n\n"
                f"{job.get('full_description', '')}"
            )
            job_path.write_text(job_desc, encoding="utf-8")

            # Save validation report
            report_path = TAILORED_DIR / f"{prefix}_REPORT.json"
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

            # Generate PDF for approved resumes (best-effort)
            # "approved_with_judge_warning" is also a success — resume was generated.
            pdf_path = None
            if report["status"] in ("approved", "approved_with_judge_warning"):
                try:
                    from applypilot.scoring.pdf import convert_to_pdf
                    pdf_path = str(convert_to_pdf(txt_path, data=data, profile=profile))
                except Exception:
                    log.debug("PDF generation failed for %s", txt_path, exc_info=True)

            result = {
                "url": job["url"],
                "path": str(txt_path),
                "data_path": str(json_data_path),
                "pdf_path": pdf_path,
                "title": job["title"],
                "site": job["site"],
                "status": report["status"],
                "attempts": report["attempts"],
            }
        except Exception as e:
            result = {
                "url": job["url"], "title": job["title"], "site": job["site"],
                "status": "error", "attempts": 0, "path": None, "pdf_path": None,
            }
            log.error("%d/%d [ERROR] %s -- %s", completed, len(jobs), job["title"][:40], e)

        results.append(result)
        stats[result.get("status", "error")] = stats.get(result.get("status", "error"), 0) + 1

        elapsed = time.time() - t0
        rate = completed / elapsed if elapsed > 0 else 0
        log.info(
            "%d/%d [%s] attempts=%s | %.1f jobs/min | %s",
            completed, len(jobs),
            result["status"].upper(),
            result.get("attempts", "?"),
            rate * 60,
            result["title"][:40],
        )

    # Persist to DB: increment attempt counter for ALL, save path only for approved
    now = datetime.now(timezone.utc).isoformat()
    _success_statuses = {"approved", "approved_with_judge_warning"}
    for r in results:
        if r["status"] in _success_statuses:
            conn.execute(
                "UPDATE jobs SET tailored_resume_path=?, tailored_at=?, "
                "tailor_attempts=COALESCE(tailor_attempts,0)+1 WHERE url=?",
                (r["path"], now, r["url"]),
            )
        else:
            conn.execute(
                "UPDATE jobs SET tailor_attempts=COALESCE(tailor_attempts,0)+1 WHERE url=?",
                (r["url"],),
            )
    conn.commit()

    elapsed = time.time() - t0
    log.info(
        "Tailoring done in %.1fs: %d approved, %d failed_validation, %d failed_judge, %d errors",
        elapsed,
        stats.get("approved", 0),
        stats.get("failed_validation", 0),
        stats.get("failed_judge", 0),
        stats.get("error", 0),
    )

    return {
        "approved": stats.get("approved", 0),
        "failed": stats.get("failed_validation", 0) + stats.get("failed_judge", 0),
        "errors": stats.get("error", 0),
        "elapsed": elapsed,
    }
