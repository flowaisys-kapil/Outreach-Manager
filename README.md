# Outreach Manager

> **Autonomous B2B lead discovery, ML qualification, and multi-turn outreach campaign engine.**

Outreach Manager is a self-hosted, production-grade campaign execution engine designed for B2B lead discovery, qualification, and automated outreach. Built with modern Python, Django CRM, and Playwright browser automation, it enables growth teams, founders, and sales engineers to run targeted outreach campaigns with zero subscription lock-in, complete data privacy, and robust account safety.

Unlike legacy platforms that rely on static lists, Outreach Manager functions as a self-learning agentic system. You define your campaign objectives and target buyer personas, and Outreach Manager handles the entire lifecycle — from discovering profiles on LinkedIn and Bayesian ML qualification to personalized multi-turn messaging, status tracking, and connection request lifecycle management.

All deal states, message histories, logs, and browser sessions remain 100% under your control on your own infrastructure.

---

## ⚡ Key Features

- 🤖 **Autonomous Lead Discovery & Bayesian ML Qualification**: Discovers target profiles via AI-generated search queries and ranks them using a Gaussian Process regressor on fastembed vector embeddings.
- ⚡ **Session-Based Batch Execution**: Executes outreach in discrete, bounded sessions. Workflows process eligible deals in natural batches, avoiding rate limits and anti-bot flags.
- 🛡️ **Stealth Fingerprinting & Human Simulation**: Playwright automation with anti-detection JS injection, Bezier-curve mouse paths, human typing cadence, and session persistence.
- 🔄 **Robust Browser Recovery**: Detects browser crashes or context deaths, rebuilds state automatically, and resumes the active deal without losing progress or wasting AI tokens.
- 💬 **Multi-Turn AI Messaging & LLM Safety**: Personalized connection notes, first-message openers, and follow-ups powered by `pydantic-ai`. Includes response sanitization and HTTP 429 quota exhaustion handling.
- 📅 **Lifecycle & Withdrawal Management**: Automatically checks pending connection requests, tracks acceptances, and withdraws unanswered requests older than 7 days.
- 📊 **CRM Dashboard & Real-Time Console**: Integrated web interface to monitor campaigns, deal funnels, execution logs, and live terminal console output.
- 📜 **Production Logging & Error Isolation**: 3-tier logging architecture separating concise operational timelines, recoverable warnings, and detailed tracebacks for unexpected bugs.

---

## 🏗️ Architecture Overview

Outreach Manager uses a modular, session-driven architecture where execution is orchestrated by the `SessionExecutor` and coordinated by a state-aware `WorkflowScheduler`:

```
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
                               v
+-------------------------------------------------------------+
|                     AccountSession / UI                     |
|              (Playwright Browser / Django CRM DB)           |
+-------------------------------------------------------------+
```

For full technical details, see [ARCHITECTURE.md](ARCHITECTURE.md).

---

## 💡 Project Philosophy

- **Session-Based Execution**: Work is executed in discrete, bounded runs with clear summary metrics. No runaway background processes or uncontrolled state drift.
- **Single Responsibility per Component**: Each workflow task (`Connect`, `Reply`, `FollowUp`, `CheckPending`, `Extract`) owns its domain exclusively.
- **Reliability Over Cleverness**: Explicit error boundaries, surgical UI pre-validation before LLM calls, and single-attempt deal resumption on browser recovery.
- **Full Data Ownership**: All deal states, chat summaries, logs, and browser profiles stay locally on your machine.

---

## 📦 Installation

### Prerequisites

- **Python**: 3.11 or 3.12
- **Google Chrome** (for native browser mode) or **Chromium** (via Playwright)
- **Git**

### Quick Setup (Windows)

Double-click `setup.bat` or run:
```cmd
setup.bat
```
This script installs dependencies, downloads Playwright browser binaries, runs database migrations, and initializes the CRM.

### Manual Setup (Linux / macOS / Windows)

1. **Clone the Repository**:
   ```bash
   git clone https://github.com/flowaisys/Outreach-Manager.git
   cd Outreach-Manager
   ```

2. **Set Up Python Virtual Environment**:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .\.venv\Scripts\activate
   pip install -r requirements/local.txt
   ```

3. **Install Playwright Browsers**:
   ```bash
   playwright install --with-deps chromium
   ```

4. **Initialize Database & Setup CRM**:
   ```bash
   python manage.py migrate --no-input
   python manage.py setup_crm
   ```

---

## ⚙️ Configuration

Configure Outreach Manager by environment variables (`.env`) or via the interactive onboarding CLI wizard (`python manage.py rundaemon`).

### Key Settings

| Variable | Description | Default |
|---|---|---|
| `AI_MODEL` | Provider and model identifier (`provider:model`) | `openai:gpt-4o` |
| `LLM_API_KEY` | API Key for your LLM provider | Required |
| `LINKEDIN_EMAIL` | LinkedIn account email | Required |
| `LINKEDIN_PASSWORD` | LinkedIn account password | Required |
| `CONNECT_DAILY_LIMIT` | Daily connection request limit | `50` |
| `FOLLOW_UP_DAILY_LIMIT` | Daily follow-up message limit | `100` |

For complete configuration details, see [docs/configuration.md](docs/configuration.md).

---

## 🚀 Running Outreach Manager

### 1. Interactive Control Dashboard

Launch the Django web application and CRM dashboard:

- **Windows**: Run `start.bat` or `powershell -File run_outreach_manager.ps1`
- **CLI**: `python manage.py runserver`

Open [http://localhost:8000/](http://localhost:8000/) in your browser to view the control center, trigger outreach cycles, inspect deal funnels, and monitor execution logs.

### 2. Manual Daemon Execution

Run a bounded outreach execution cycle via the management command:

```bash
python manage.py rundaemon
```

To run a single cycle and exit when no tasks remain due:

```bash
python manage.py rundaemon --exit-on-empty
```

### 3. Docker Deployment

Deploy containerized Outreach Manager with VNC remote desktop support:

```bash
docker compose -f local.yml up --build -d
```

For complete Docker guide and production deployment details, see [docs/docker.md](docs/docker.md).

---

## 📂 Project Structure

```
Outreach-Manager/
├── outreach_manager/         # Application package
│   ├── core/                 # Session executor, scheduler, LLM & logging
│   │   ├── templates/core/   # CRM Dashboard HTML templates
│   │   └── management/       # Django CLI commands (rundaemon, setup_crm)
│   ├── crm/                  # Lead, Deal, Campaign models & CRM logic
│   ├── linkedin/             # Playwright browser engine, tasks, ML qualification
│   │   ├── browser/          # Stealth profile, human actions & UI validation
│   │   └── tasks/            # Connect, Reply, FollowUp, CheckPending tasks
│   ├── chat/                 # Multi-turn chat message models & history
│   └── emails/               # Email delivery channels
├── compose/                  # Docker container assets
├── docs/                     # Technical documentation
├── scripts/                  # Helper & maintenance scripts
├── tests/                    # Test suite (pytest)
├── manage.py                 # Django management entrypoint
├── start.bat / setup.bat     # Windows launcher scripts
└── local.yml                 # Docker Compose specification
```

---

## 🧪 Development & Testing

Run the full pytest suite:

```bash
pytest tests/ -v
```

All 545+ tests verify workflow isolation, scheduler integrity, browser recovery, LLM quota handling, and session error accounting.

---

## 🗺️ Roadmap

- 🔄 **Automatic Mode**: Continuous background daemon scheduler with configurable sleep intervals.
- 🎯 **Routine Planner**: Dynamic time-of-day execution cadence tailored to buyer timezones.
- 🧠 **Conversation Strategy Engine**: Advanced multi-turn conversation goal trees for complex deal qualification.
- 📈 **AI Usage Analytics**: Fine-grained token consumption and provider cost tracking per campaign.
- 📜 **Session History Visualizer**: Interactive timeline viewer for past execution session summaries.

---

## 📄 License

Distributed under the GNU General Public License v3.0 (`LICENCE.md`).

---

## 📚 Documentation Index

- [Architecture Guide](ARCHITECTURE.md)
- [Configuration Guide](docs/configuration.md)
- [Docker Deployment Guide](docs/docker.md)
