# ⏱️ Chronos-Git

> **"The architect who codes while thinking."**
> *A solution to forgetting. A system for discipline.*

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)](LICENSE)
[![Security](https://img.shields.io/badge/Security-GPG%20Signed-red?style=for-the-badge)](docs/SECURITY.md)

---

## 🎯 Vision

> *"The human brain is built to create, not to remember to type `git push` every night at 10pm. Chronos-Git is the persistence layer of your discipline."*

In a world of micro-services and continuous deployment, **manual management of branches and commits is a cognitive drain**. Chronos-Git lets developers:

- 📅 **Plan releases** across days, weeks, or months
- 🌿 **Orchestrate branches** with automated periodic merges
- 🔁 **Self-heal** after PC shutdowns — catching up missed commits with correct historical dates
- 👁️ **Detect drift** — modified or new local files not yet tracked
- 🔐 **Stay secure** — zero plaintext credentials, GPG-signed commits

---

## 🏗️ Architecture Technique

```
chronos-git/
├── chronos/
│   ├── core/
│   │   ├── database.py       # SQLite state management (task queue)
│   │   ├── scanner.py        # Folder scanner (sort by creation date)
│   │   ├── scheduler.py      # Smart distribution + jitter algorithm
│   │   ├── executor.py       # Git operations engine (GitPython)
│   │   ├── catchup.py        # Self-healing catch-up engine
│   │   └── drift.py          # Drift detection (modified + untracked)
│   ├── gui/
│   │   ├── app.py            # Main CustomTkinter window
│   │   ├── dashboard.py      # Dashboard with timeline visualization
│   │   ├── planner.py        # Interactive planning table
│   │   └── notifier.py       # Desktop notifications
│   ├── cli/
│   │   └── main.py           # Typer-based CLI interface
│   ├── security/
│   │   ├── keyring_manager.py # OS keyring (zero plaintext secrets)
│   │   ├── hasher.py          # SHA-256 file integrity checks
│   │   └── gpg_signer.py      # GPG commit signing
│   └── utils/
│       ├── logger.py          # Loguru structured logging
│       ├── config.py          # YAML config loader
│       └── github_api.py      # PyGithub for PR automation
├── tests/                     # Unit & integration tests
├── docs/                      # Extended documentation
├── plan.yaml.example          # Config template
├── requirements.txt
└── main.py                    # Entry point
```

### Core Design Principles

| Principle | Implementation |
|-----------|---------------|
| **State Management** | SQLite for a robust, crash-resistant task queue |
| **Security First** | OS Keyring — zero passwords in plaintext |
| **Smart Catch-up** | `GIT_COMMITTER_DATE` to preserve logical chronology |
| **Drift Detection** | `git status` + filesystem scan before every push |
| **Human-in-the-Loop** | Confirms modified/new files before acting |
| **Atomic Commits** | One logical change per commit |

---

## 🚀 Quick Start

### Installation

```bash
git clone https://github.com/yourusername/chronos-git.git
cd chronos-git
pip install -r requirements.txt

# Store your GitHub token securely (never in plaintext)
python main.py security setup-token

# Launch GUI
python main.py gui

# Launch CLI
python main.py cli --help
```

### Plan a 30-day folder

```bash
# CLI — Auto mode (sorts by creation date, distributes evenly)
python main.py cli plan --folder ./30-days-scripting --days 30 --repo https://github.com/you/repo

# CLI — Custom config mode
python main.py cli plan --config plan.yaml
```

### Using a YAML config (full control)

```yaml
project: 30-days-scripting
repo_path: /home/user/projects/30-days-scripting
remote: origin
strategy: daily          # daily | weekly | custom
start_date: 2026-04-01
branch: feat/30-days-challenge
merge_into: main
merge_every: friday      # friday | monday | 6days | on_complete

overrides:
  - file: "secret_draft.py"
    action: skip
  - file: "final_project.py"
    date: "2026-04-30 23:59:00"
    message: "🚀 feat: 30-day challenge COMPLETED"
    branch: main
```

---

## 🛡️ Security

See [docs/SECURITY.md](docs/SECURITY.md) for the complete threat model.

**Key principles:**
- ✅ GitHub tokens stored in OS Keyring (Windows Credential Manager / macOS Keychain / Linux Secret Service)
- ✅ Fine-grained PAT — only access to specific repos
- ✅ GPG-signed commits (Verified badge on GitHub)
- ✅ SHA-256 file integrity check before every commit
- ✅ Path traversal protection via `pathlib`
- ✅ Kill switch (emergency stop all scheduled tasks)
- ✅ `Gitleaks` audit before first push

---

## 🎖️ GitHub Trophy Strategy

Chronos-Git is designed to maximize your GitHub profile authenticity:

- **Atomic commits** → readable history
- **Branch → PR → Merge workflow** → unlocks Pull Shark badge
- **Consistent daily activity** → Streak maintenance
- **Varied commit prefixes** (`feat:`, `fix:`, `docs:`, `refactor:`) → professional history
- **Auto-generated CHANGELOG.md** → demonstrates release management skills

---

## 📄 License

MIT — See [LICENSE](LICENSE)
