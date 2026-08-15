# Outreach Manager v2.1

> **Autonomous B2B Lead Discovery, Zero-Navigation Stealth Enrichment, Bayesian ML Qualification, and Multi-Turn Outreach Campaign Engine.**

Outreach Manager v2.1 is a self-hosted, production-grade campaign execution engine built for B2B lead discovery, qualification, and automated outreach. Built with modern Python 3.12, Django 6.0 CRM, Playwright browser automation, and Bayesian machine learning, it enables growth teams, founders, and sales engineers to run targeted outreach campaigns with zero subscription lock-in, complete data privacy, and max-level account safety.

Unlike legacy automation tools that rely on static lists or aggressive profile scraping, Outreach Manager v2.1 operates as an intelligent agentic system. You define campaign objectives and buyer personas, and Outreach Manager handles the entire lifecycle — from zero-navigation search card discovery on LinkedIn and Bayesian ML qualification to personalized multi-turn messaging, status synchronization, and connection request lifecycle management.

All deal states, message histories, telemetry logs, and browser sessions remain 100% under your control on your own infrastructure.

---

## ⚡ Key Features (v2.0)

- 🤖 **Zero-Navigation Lead Discovery & Enrichment**: Discovers target profiles directly off search result cards and constructs enriched `Lead` records without opening individual candidate profile pages (`navigate=False`). **Reduces profile page views by 90–95%**, completely preventing high-volume scraping detection.
- 🎯 **Humanized Routine Planner**: Calculates human-like session start times based on customizable working windows (`working_windows`), active days (`active_days`), and micro-jitter delays to mirror natural human activity patterns.
- 🔄 **Automatic Execution Mode**: Flexible execution modes supporting `interactive`, `scheduled` (Windows Task Scheduler / cron), and `automatic` daemon loops (`python manage.py rundaemon`).
- 📈 **AI Usage Analytics & Telemetry**: Thread-safe tracking of LLM API consumption (primary vs. fallback providers, input/output token estimation, latency, structured output calls, and provider health metrics).
- 📜 **Session History & Flight Recorder**: Neutral `SessionRecorder` persisting execution session history (`SessionHistory`), workflow breakdowns, error accounting, and interactive CLI/web reporting.
- 🛡️ **Full Stealth & Human Simulation**: Playwright browser automation with anti-detection stealth injection, Bezier-curve mouse pathing, human typing cadence, and persistent Chrome profile session caching.
- 🧠 **Context-Aware Bayesian ML & LLM Qualification**: Ranks candidates using a Gaussian Process regressor on fastembed vector embeddings and qualifies target decision-makers using LLM prompts designed to prevent false `wrong-fit` rejections.
- 🔄 **Robust Browser Recovery**: Detects browser context deaths, automatically rebuilds state, and resumes active deal operations without losing progress or wasting AI tokens.
- 💬 **Multi-Turn AI Messaging & LLM Safety**: Personalized connection notes, first-message openers, and follow-ups powered by `pydantic-ai`. Includes response sanitization and HTTP 429 quota exhaustion handling.
- 📅 **Lifecycle & Withdrawal Management**: Automatically tracks pending connection requests and withdraws unanswered requests older than 7 days.
- 📊 **CRM Dashboard & Real-Time Console**: Integrated web dashboard to monitor campaigns, deal funnels, execution session logs, and live terminal console output.

---

## 🏗️ Architecture Overview

Outreach Manager v2.0 follows a session-driven, modular architecture where execution is managed by the `SessionExecutor`, coordinated by a state-aware `WorkflowScheduler`, and recorded by the neutral `SessionRecorder`:

```
+-------------------------------------------------------------+
|                 Routine Planner / Execution Router          |
+-------------------------------------------------------------+
                               |
                               v
+-------------------------------------------------------------+
|                       Session Executor                      |
+-------------------------------------------------------------+
                               |
                               v
+-------------------------------------------------------------+
|                      Workflow Scheduler                     |
+-------------------------------------------------------------+
                               |
       +------------+-----------+-----------+------------+
       |            |                       |            |
       v            v                       v            v
   +-------+   +---------+            +-----------+  +---------+
   |Connect|   |  Reply  |            | Follow-Up |  |CheckPend|
   +-------+   +---------+            +-----------+  +---------+
       |            |                       |            |
       +------------+-----------+-----------+------------+
                               |
            +------------------+------------------+
            |                                     |
            v                                     v
+-----------------------+             +-----------------------+
|  AccountSession / UI  |             |    SessionRecorder    |
| (Playwright / Chrome) |             |  (SessionHistory /    |
|                       |             |   AIUsageLog Telemetry)
+-----------------------+             +-----------------------+
```

For full technical details, see [ARCHITECTURE.md](ARCHITECTURE.md).

---

## 💡 Project Philosophy

- **Account Protection & Anti-Detection First**: Zero-navigation profile enrichment, conservative rate limits (12-25s delays, 5 max per page), and human-like micro-behavior protect your LinkedIn account from flags.
- **Session-Based Execution**: Work is executed in discrete, bounded runs with clean summary metrics. No runaway background processes or uncontrolled state drift.
- **Single Responsibility per Component**: Each workflow task (`Connect`, `Reply`, `FollowUp`, `CheckPending`, `Extract`) owns its domain exclusively.
- **Reliability & Full Data Ownership**: Explicit error boundaries, surgical UI pre-validation before LLM calls, and complete local storage of all deal states, chat logs, and browser profiles.

---

## 📦 Quick Start & Installation

### Prerequisites

- **Python**: 3.11 or 3.12
- **Google Chrome** (for native browser mode) or **Chromium** (via Playwright)
- **Git**

### 1. Windows One-Click Setup

Double-click `setup.bat` or run in PowerShell:
```powershell
.\setup.bat
```
This script sets up Python dependencies, downloads Playwright browser binaries, runs Django migrations, and initializes the CRM.

### 2. Manual Setup (Linux / macOS / Windows)

```bash
# Clone the Repository
git clone https://github.com/flowaisys/Outreach-Manager.git
cd Outreach-Manager

# Create Virtual Environment
python -m venv .venv
source .venv/bin/activate  # Windows: .\.venv\Scripts\activate

# Install Dependencies & Playwright
pip install -r requirements/local.txt
playwright install --with-deps chromium

# Initialize Database & Setup CRM
python manage.py migrate --no-input
python manage.py setup_crm
```

---

## ⚙️ Configuration

Configure Outreach Manager v2.0 via environment variables (`.env`) or central configuration (`outreach_manager/core/conf.py`).

### Key Runtime Settings

| Parameter | Description | Default |
|---|---|---|
| `ENABLE_ACTIVE_HOURS` | Restrict execution to active working hours | `True` (9 AM - 6 PM) |
| `enrich_min_delay_seconds` | Minimum delay between lead enrichments | `12` |
| `enrich_max_delay_seconds` | Maximum delay between lead enrichments | `25` |
| `enrich_max_per_page` | Maximum leads enriched per search page | `5` |
| `min_action_interval` | Minimum delay between write actions | `180s` (3 min) |
| `CHECK_PENDING_DAILY_CAP` | Daily cap on pending connection checks | `50` |
| `AI_MODEL` | Provider and model identifier (`provider:model`) | `openai:gpt-4o` |
| `LLM_API_KEY` | API Key for primary LLM provider | Required |

---

## 🚀 Execution Modes

### 1. Interactive CRM Control Dashboard

Launch the Django web application and CRM control center:
```bash
python manage.py runserver
```
or double-click `start.bat` on Windows.
Open [http://localhost:8000/](http://localhost:8000/) to view campaign funnels, trigger execution cycles, and inspect session history metrics.

### 2. Automatic Daemon Mode (`rundaemon`)

Run a bounded, single-session outreach cycle:
```bash
python manage.py rundaemon
```

To run a single cycle and exit immediately when no tasks remain due:
```bash
python manage.py rundaemon --exit-on-empty
```

---

## 📂 Project Structure

```
Outreach-Manager/
├── outreach_manager/         # Core application package
│   ├── core/                 # Session executor, scheduler, routine planner, AI usage tracker
│   │   ├── llm/              # Multi-provider LLM client, runner, and usage tracker
│   │   ├── templates/core/   # CRM Dashboard HTML templates
│   │   └── management/       # Django CLI commands (rundaemon, setup_crm)
│   ├── crm/                  # Lead, Deal, Campaign models & CRM logic
│   ├── linkedin/             # Playwright browser engine, tasks, ML qualification
│   │   ├── browser/          # Stealth profile, human actions & UI validation
│   │   ├── db/               # Leads DB operations & zero-navigation enrichment
│   │   └── tasks/            # Connect, Reply, FollowUp, CheckPending tasks
│   ├── chat/                 # Multi-turn chat message models & history
│   └── emails/               # Email delivery channels
├── compose/                  # Docker container assets
├── docs/                     # Technical documentation & configuration guides
├── scripts/                  # Maintenance & diagnostic scripts
├── tests/                    # Test suite (pytest)
├── manage.py                 # Django management entrypoint
└── start.bat / setup.bat     # Windows launcher scripts
```

---

## 🧪 Development & Testing

Run the full pytest suite:
```bash
pytest tests/ -v
```
All 115+ core unit tests verify workflow isolation, routine planner logic, zero-navigation enrichment, browser recovery, LLM quota handling, and session telemetry.

---

## 📄 License

Distributed under the GNU General Public License v3.0 (`LICENCE.md`).
