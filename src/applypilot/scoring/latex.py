"""LaTeX resume generation for ApplyPilot.

Fills Jake's Resume .tex template with tailored JSON data from the LLM,
then compiles to PDF via pdflatex.

Flow:
  tailor.py  ->  JSON data dict
  latex.py   ->  .tex file  ->  pdflatex  ->  .pdf

Requires MiKTeX or TeX Live: https://miktex.org/download
"""

import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LaTeX escape
# ---------------------------------------------------------------------------

_LATEX_ESCAPE = str.maketrans({
    "&":  r"\&",
    "%":  r"\%",
    "$":  r"\$",
    "#":  r"\#",
    "_":  r"\_",
    "{":  r"\{",
    "}":  r"\}",
    "~":  r"\textasciitilde{}",
    "^":  r"\textasciicircum{}",
    "\\": r"\textbackslash{}",
    "–": "--",   # en dash → LaTeX double hyphen
    "—": "---",  # em dash → LaTeX triple hyphen
    "‘": "`",    # left single quote
    "’": "'",    # right single quote
    "“": "``",   # left double quote
    "”": "''",   # right double quote
})


def _e(text: str) -> str:
    """Escape a string for safe inclusion in LaTeX."""
    return str(text).translate(_LATEX_ESCAPE)


# ---------------------------------------------------------------------------
# Template builder
# ---------------------------------------------------------------------------

def build_tex(data: dict, profile: dict) -> str:
    """Build a Jake's Resume .tex document from tailored JSON + profile.

    Args:
        data:    LLM-generated resume JSON (same schema as tailor.py output).
        profile: User profile dict from load_profile().

    Returns:
        Complete LaTeX source string.
    """
    personal = profile.get("personal", {})
    name     = _e(personal.get("full_name", ""))
    phone    = _e(personal.get("phone", ""))
    email    = personal.get("email", "")        # used in href, escaped below
    city     = _e(personal.get("city", ""))
    state    = _e(personal.get("province_state", ""))
    linkedin = personal.get("linkedin_url", "")
    github   = personal.get("github_url", "")

    location_str = f"{city}, {state}" if city and state else city or state

    # Contact items  (phone | email | location | linkedin | github)
    contact_parts = []
    if phone:
        contact_parts.append(rf"Phone: \href{{tel:{phone}}}{{{phone}}}")
    if email:
        contact_parts.append(rf"\href{{mailto:{_e(email)}}}{{{_e(email)}}}")
    if location_str:
        contact_parts.append(f"Location: {location_str}")
    if linkedin:
        display = linkedin.replace("https://", "").replace("http://", "")
        contact_parts.append(rf"\href{{{linkedin}}}{{{_e(display)}}}")
    if github:
        display = github.replace("https://", "").replace("http://", "")
        contact_parts.append(rf"\href{{{github}}}{{{_e(display)}}}")
    portfolio = personal.get("portfolio_url", "") or personal.get("website_url", "")
    if portfolio:
        display = portfolio.replace("https://", "").replace("http://", "")
        contact_parts.append(rf"\href{{{portfolio}}}{{{_e(display)}}}")

    contact_line = r" \quad ".join(contact_parts)

    # ── Experience ──────────────────────────────────────────────────────────
    exp_entries = data.get("experience", [])
    exp_lines = []
    for i, entry in enumerate(exp_entries):
        # Format header as "\textbf{Title} $|$ \textbf{Company}" if "at" separator present
        raw_header = entry.get("header", "")
        if " at " in raw_header:
            title_part, company_part = raw_header.split(" at ", 1)
            header = rf"\textbf{{{_e(title_part)}}} $|$ \textbf{{{_e(company_part)}}}"
        elif " | " in raw_header:
            title_part, company_part = raw_header.split(" | ", 1)
            header = rf"\textbf{{{_e(title_part)}}} $|$ \textbf{{{_e(company_part)}}}"
        else:
            header = rf"\textbf{{{_e(raw_header)}}}"
        subtitle_raw = entry.get("subtitle", "")
        date_str = ""
        tech_str = ""
        if "|" in subtitle_raw:
            parts = [p.strip() for p in subtitle_raw.split("|")]
            date_pattern = re.compile(
                r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|"
                r"January|February|March|April|May|June|July|August|September|October|November|December|"
                r"\d{4}|Present)\b",
                re.IGNORECASE,
            )
            # location pattern — city, country/state combos, skip these parts
            location_pattern = re.compile(r"\b(India|USA|UK|Remote|Bengaluru|Mumbai|Delhi|Hyderabad|Pune|Chennai|Bangalore)\b", re.IGNORECASE)
            date_parts = [p for p in parts if date_pattern.search(p)]
            tech_parts = [p for p in parts if not date_pattern.search(p) and not location_pattern.search(p)]
            date_str = _e(date_parts[0]) if date_parts else ""
            tech_str = _e(", ".join(tech_parts)) if tech_parts else ""
        else:
            tech_str = _e(subtitle_raw)

        exp_lines.append(
            rf"  \resumeSubheading"
            "\n"
            rf"    {{{header}}}{{{date_str}}}{{}}{{}}"
            "\n"
            r"    \vspace{-10pt}"
        )
        exp_lines.append(r"    \resumeItemListStart")
        for bullet in entry.get("bullets", []):
            exp_lines.append(rf"  \resumeItem{{{_e(bullet)}}}")
        if tech_str:
            exp_lines.append(
                rf"  \resumeItem{{\textbf{{Technologies:}} {_e(tech_str)}}}"
            )
        exp_lines.append(r"    \resumeItemListEnd")
        # spacing between entries only, not after the last one
        if i < len(exp_entries) - 1:
            exp_lines.append(r"    \vspace{10pt}")

    exp_block = "\n".join(exp_lines)

    # ── Skills ──────────────────────────────────────────────────────────────
    skills_lines = []
    skills = data.get("skills", {})
    if isinstance(skills, dict):
        for cat, val in skills.items():
            skills_lines.append(
                rf"    \item \textbf{{{_e(cat)}:}} {_e(str(val))}"
            )
    skills_block = "\n".join(skills_lines)

    # ── Projects ────────────────────────────────────────────────────────────
    proj_entries = data.get("projects", [])
    proj_lines = []
    for i, entry in enumerate(proj_entries):
        header   = _e(entry.get("header", ""))
        subtitle = entry.get("subtitle", "")

        # Extract URL — match https://... or bare github.com/... / gitlab.com/...
        url_match = re.search(r"https?://\S+|(?:github|gitlab|bitbucket)\.com/\S+", subtitle)
        proj_url_raw = url_match.group(0) if url_match else ""

        # Normalise to full URL for \href
        if proj_url_raw and not proj_url_raw.startswith("http"):
            proj_url = "https://" + proj_url_raw
        else:
            proj_url = proj_url_raw

        # Extract tech stack — strip the URL and surrounding separators
        tech_part = re.sub(r"https?://\S+|(?:github|gitlab|bitbucket)\.com/\S+", "", subtitle)
        tech_part = re.sub(r"\s*\|\s*$", "", tech_part).strip(" |,").strip()

        if proj_url_raw:
            url_display = proj_url_raw.rstrip("/").rstrip(".git")
            url_cell = rf"\href{{{proj_url}}}{{{_e(url_display)}}}"
        else:
            url_cell = ""

        # Layout: bold project name left, github link right — same as sample .tex
        proj_lines.append(
            rf"  \resumeSubheading"
            "\n"
            rf"    {{\textbf{{{header}}}}}{{{url_cell}}}{{}}{{}}"
            "\n"
            r"  \vspace{-8pt}"
        )
        proj_lines.append(r"  \resumeItemListStart")

        bullets = entry.get("bullets", [])
        for bullet in bullets:
            proj_lines.append(rf"  \resumeItem{{{_e(bullet)}}}")

        # Append tech stack as a bold "Technologies:" bullet — no separate subtitle row
        if tech_part:
            proj_lines.append(
                rf"  \resumeItem{{\textbf{{Technologies:}} {_e(tech_part)}}}"
            )

        proj_lines.append(r"  \resumeItemListEnd")
        # small gap between project entries, not after the last one
        if i < len(proj_entries) - 1:
            proj_lines.append(r"  \vspace{4pt}")

    proj_block = "\n".join(proj_lines)

    # ── Education ───────────────────────────────────────────────────────────
    # LLM may return:
    #   "School\nDegree | Year\nGPA: x"   (newline-separated)
    #   "School | Degree | Year | GPA"     (pipe-separated)
    #   or a flat string
    edu_raw  = str(data.get("education", ""))
    edu_name = ""
    edu_year = ""
    edu_deg  = ""
    edu_gpa  = ""

    if "|" in edu_raw and "\n" not in edu_raw:
        # Pure pipe-separated: School | Degree | Year | GPA
        edu_parts = [p.strip() for p in edu_raw.split("|")]
        edu_name = edu_parts[0] if len(edu_parts) > 0 else ""
        edu_deg  = edu_parts[1] if len(edu_parts) > 1 else ""
        edu_year = edu_parts[2] if len(edu_parts) > 2 else ""
        edu_gpa  = edu_parts[3] if len(edu_parts) > 3 else ""
    elif "\n" in edu_raw:
        # Newline-separated — line 0 = school, line 1 = degree (may contain year), line 2 = GPA
        lines = [l.strip() for l in edu_raw.splitlines() if l.strip()]
        edu_name = lines[0] if len(lines) > 0 else ""
        # Line 1 may be "Degree | Year" or just "Degree"
        if len(lines) > 1:
            if "|" in lines[1]:
                deg_parts = [p.strip() for p in lines[1].split("|")]
                edu_deg  = deg_parts[0]
                edu_year = deg_parts[1] if len(deg_parts) > 1 else ""
            else:
                edu_deg = lines[1]
        # Line 2 may be "CGPA: x" or "GPA: x"
        if len(lines) > 2:
            edu_gpa = re.sub(r"(?i)^(cgpa|gpa)\s*[:\-]\s*", "", lines[2]).strip()
    else:
        edu_name = edu_raw

    # Fall back to profile education
    facts = profile.get("resume_facts", {})
    if not edu_name:
        edu_name = facts.get("preserved_school", "")
    if not edu_deg:
        edu_deg = profile.get("experience", {}).get("education_level", "")
    # Fill year and GPA from resume_facts if LLM didn't include them
    if not edu_year:
        edu_year = facts.get("graduation_year", "")
    if not edu_gpa:
        edu_gpa = facts.get("gpa", "")

    edu_block = (
        r"  \resumeSubheading"
        "\n"
        rf"    {{{_e(edu_name)}}}{{{_e(edu_year)}}}{{{_e(edu_deg)}}}{{{_e(edu_gpa)}}}"
    )

    # ── Summary ─────────────────────────────────────────────────────────────
    summary = _e(data.get("summary", ""))
    title   = _e(data.get("title", "Software Engineer"))

    cert_block = ""  # certifications disabled

    # ── Achievements (pass through from profile if present) ─────────────────
    achievements = profile.get("achievements", [])
    ach_block = ""
    if achievements:
        ach_items = "\n".join(
            rf"      \resumeItem{{{_e(a)}}}" for a in achievements
        )
        ach_block = rf"""
%-----------Achievements-----------
\section{{Achievements}}
\vspace{{-25pt}}
\resumeSubHeadingListStart
  \resumeSubheading{{}}{{}}{{}}{{}}
    \resumeItemListStart
{ach_items}
    \resumeItemListEnd
\resumeSubHeadingListEnd
\vspace{{-10pt}}
"""

    # ── Full document ────────────────────────────────────────────────────────
    return rf"""\documentclass[letterpaper,10pt]{{article}}
\usepackage{{latexsym}}
\usepackage[empty]{{fullpage}}
\usepackage{{titlesec}}
\usepackage{{marvosym}}
\usepackage[usenames,dvipsnames]{{xcolor}}
\definecolor{{linkcolor}}{{HTML}}{{2C3E8C}}
\usepackage{{verbatim}}
\usepackage{{enumitem}}
\setlist[itemize]{{label=\textbullet}}
\usepackage[colorlinks=true, urlcolor=linkcolor, linkcolor=black, citecolor=black]{{hyperref}}
\usepackage{{fancyhdr}}
\usepackage[english]{{babel}}
\usepackage{{tabularx}}
\input{{glyphtounicode}}

\pagestyle{{fancy}}
\fancyhf{{}}
\fancyfoot{{}}
\renewcommand{{\headrulewidth}}{{0pt}}
\renewcommand{{\footrulewidth}}{{0pt}}

\addtolength{{\oddsidemargin}}{{-0.5in}}
\addtolength{{\evensidemargin}}{{-0.5in}}
\addtolength{{\textwidth}}{{1in}}
\addtolength{{\topmargin}}{{-.5in}}
\addtolength{{\textheight}}{{1.2in}}

\urlstyle{{same}}
\raggedbottom
\raggedright
\setlength{{\tabcolsep}}{{0in}}

\titleformat{{\section}}{{
  \vspace{{-4pt}}\scshape\raggedright\large
}}{{}}{{0em}}{{}}[\color{{black}}\titlerule \vspace{{-5pt}}]

\pdfgentounicode=1

\newcommand{{\resumeItem}}[1]{{\item {{#1}}}}

\newcommand{{\resumeSubheading}}[4]{{%
  \item%
    \begin{{tabular*}}{{0.97\textwidth}}[t]{{l@{{\extracolsep{{\fill}}}}r}}
      \textbf{{#1}} & #2 \\
      \textit{{#3}} & \textit{{#4}} \\
    \end{{tabular*}}%
}}

\newcommand{{\resumeSubHeadingListStart}}{{%
  \begin{{itemize}}[leftmargin=0.15in, label={{}}, itemsep=0pt, topsep=0pt, parsep=0pt, partopsep=0pt]%
}}
\newcommand{{\resumeSubHeadingListEnd}}{{\end{{itemize}}}}
\newcommand{{\resumeItemListStart}}{{\begin{{itemize}}[itemsep=0pt, topsep=0pt, parsep=0pt, partopsep=0pt]}}
\newcommand{{\resumeItemListEnd}}{{\end{{itemize}}}}

\begin{{document}}
\begin{{center}}
    \textbf{{\Huge \scshape {name}}} \\
    \vspace{{3pt}}
    {contact_line}
\end{{center}}

%-----------Summary-----------
\section{{Summary}}
\begin{{itemize}}[leftmargin=0.15in, label={{}}, itemsep=1pt, topsep=0pt]
    \item {summary}
\end{{itemize}}

%-----------Experience-----------
\section{{Experience}}
  \resumeSubHeadingListStart
{exp_block}
  \resumeSubHeadingListEnd

%-----------Skills-----------
\section{{Skills}}
\vspace{{3pt}}
\begin{{itemize}}[leftmargin=0.15in, label={{}}, itemsep=1pt, topsep=0pt, parsep=0pt]
{skills_block}
\end{{itemize}}
\vspace{{-5pt}}

{cert_block}
%-----------Projects-----------
\section{{Projects}}
\resumeSubHeadingListStart
{proj_block}
\resumeSubHeadingListEnd
{ach_block}
%-----------Education-----------
\section{{Education}}
  \resumeSubHeadingListStart
{edu_block}
  \resumeSubHeadingListEnd

\end{{document}}
"""


# ---------------------------------------------------------------------------
# Compiler
# ---------------------------------------------------------------------------

def find_pdflatex() -> str | None:
    """Return the pdflatex executable path, or None if not installed.

    On Windows, shutil.which() uses the process PATH which may not include
    MiKTeX if it was installed after this process started. We also check the
    known MiKTeX install location directly.
    """
    found = shutil.which("pdflatex")
    if found:
        return found

    if os.name == "nt":
        candidates = [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/MiKTeX/miktex/bin/x64/pdflatex.exe",
            Path(os.environ.get("PROGRAMFILES", "")) / "MiKTeX/miktex/bin/x64/pdflatex.exe",
        ]
        for c in candidates:
            if c.exists():
                return str(c)

    return None


def compile_tex(tex_source: str, output_path: Path) -> Path:
    """Compile LaTeX source to PDF via pdflatex.

    Runs pdflatex twice (for correct page references), in a temp dir,
    then moves the PDF to output_path.

    Args:
        tex_source:  Complete LaTeX source string.
        output_path: Where to write the final PDF.

    Returns:
        Path to the generated PDF.

    Raises:
        FileNotFoundError: pdflatex not on PATH.
        RuntimeError:      pdflatex compilation failed.
    """
    pdflatex = find_pdflatex()
    if not pdflatex:
        raise FileNotFoundError(
            "pdflatex not found. Install MiKTeX from https://miktex.org/download "
            "or TeX Live from https://tug.org/texlive/"
        )

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        tex_file = Path(tmpdir) / "resume.tex"
        tex_file.write_text(tex_source, encoding="utf-8")

        env = os.environ.copy()
        # MiKTeX auto-install packages non-interactively
        env.setdefault("MIKTEX_AUTOINSTALL", "1")

        # Pass just the filename, not the full path — pdflatex chokes on
        # paths with spaces even when quoted. Since cwd=tmpdir, the bare
        # filename resolves correctly.
        cmd = [
            pdflatex,
            "-interaction=nonstopmode",
            "-halt-on-error",
            "resume.tex",
        ]

        for _ in range(2):  # two passes for correct layout
            result = subprocess.run(
                cmd,
                cwd=tmpdir,
                capture_output=True,
                text=True,
                env=env,
                timeout=120,
            )
            if result.returncode != 0:
                # Extract the useful error line from pdflatex output
                error_lines = [
                    l for l in result.stdout.splitlines()
                    if l.startswith("!") or "Error" in l
                ]
                snippet = "\n".join(error_lines[:10]) or result.stdout[-500:]
                raise RuntimeError(
                    f"pdflatex failed (exit {result.returncode}):\n{snippet}"
                )

        pdf_tmp = Path(tmpdir) / "resume.pdf"
        if not pdf_tmp.exists():
            raise RuntimeError("pdflatex ran but no PDF was produced.")

        shutil.move(str(pdf_tmp), str(output_path))
        log.info("LaTeX PDF generated: %s", output_path)
        return output_path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def convert_json_to_pdf(
    data: dict,
    profile: dict,
    output_path: Path,
    save_tex: bool = True,
) -> Path:
    """Build .tex from JSON + profile and compile to PDF.

    Args:
        data:        LLM resume JSON dict.
        profile:     User profile dict.
        output_path: Destination PDF path.
        save_tex:    If True, also save the .tex source alongside the PDF.

    Returns:
        Path to the PDF.
    """
    tex = build_tex(data, profile)

    if save_tex:
        tex_path = Path(output_path).with_suffix(".tex")
        tex_path.write_text(tex, encoding="utf-8")
        log.debug("LaTeX source saved: %s", tex_path)

    return compile_tex(tex, output_path)
