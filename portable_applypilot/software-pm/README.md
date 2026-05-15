# Portable ApplyPilot Data - software-pm

This folder contains a portable copy of the local `software-pm` ApplyPilot data:

- `applypilot.sqlite` - copy of the local ApplyPilot SQLite database
- `personas/software-pm/` - persona profile, resume, PDF, and search config
- `tailored_resumes/software-pm/` - generated resume drafts, job descriptions, and validation reports
- `manual_apply_index_software-pm*.csv` - manual apply indexes
- `imports/` - prior manual apply import files

It intentionally does not include `C:\Users\bsing\.applypilot\.env` or any API keys.

## Restore On Another Windows Laptop

From a fresh clone of this repo:

```powershell
cd C:\Users\<you>\OneDrive\Documents\Projects\ApplyPilot

py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
python -m pip install python-jobspy
```

Create the ApplyPilot data folders and copy the exported files:

```powershell
$src = ".\portable_applypilot\software-pm"
$dst = "$env:USERPROFILE\.applypilot"

New-Item -ItemType Directory -Force -Path $dst | Out-Null
Copy-Item "$src\applypilot.sqlite" "$dst\applypilot.db" -Force
Copy-Item "$src\manual_apply_index_software-pm*.csv" "$dst\" -Force
Copy-Item "$src\personas" "$dst\" -Recurse -Force
Copy-Item "$src\tailored_resumes" "$dst\" -Recurse -Force
Copy-Item "$src\imports" "$dst\" -Recurse -Force
```

Then create `C:\Users\<you>\.applypilot\.env` on that laptop with your own API key:

```text
OPENAI_API_KEY=your_key_here
LLM_MODEL=gpt-4o-mini
```

Verify:

```powershell
applypilot doctor --persona software-pm
applypilot status --persona software-pm
```
