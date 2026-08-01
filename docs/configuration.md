# Outreach Manager Configuration Guide

This document covers all configuration options for **Outreach Manager v1.0**, including environment variables (`.env`), AI providers, browser options, database settings, and campaign preferences.

---

## 1. Environment Variables (`.env`)

Outreach Manager loads environment variables from the `.env` file at the repository root. You can configure variables manually or through the interactive onboarding wizard (`python manage.py rundaemon`).

### Core Settings Table

| Variable | Description | Default / Example |
|:---|:---|:---|
| `AI_MODEL` | Provider and model identifier (`provider:model`). | `openai:gpt-4o` |
| `LLM_API_KEY` | API Key for your LLM provider. | `sk-...` (Required) |
| `LLM_API_BASE` | Base URL for custom endpoints (OpenAI-compatible endpoints). | `http://localhost:11434/v1` |
| `LINKEDIN_EMAIL` | Login email for your LinkedIn account. | `user@example.com` |
| `LINKEDIN_PASSWORD` | Login password for your LinkedIn account. | `secret` |
| `CONNECT_DAILY_LIMIT` | Daily ceiling for connection requests. | `50` |
| `FOLLOW_UP_DAILY_LIMIT` | Daily ceiling for follow-up messages. | `100` |
| `ENABLE_VNC` | Enable noVNC remote desktop inside Docker containers. | `false` |
| `DJANGO_SETTINGS_MODULE` | Django settings module path. | `outreach_manager.settings` |

---

## 2. AI Provider Configuration (`provider:model`)

Outreach Manager uses `pydantic-ai` to standardize LLM interactions. Every model string **must prefix the provider name** (`provider:model`) to prevent key mismatch errors.

### Supported Providers & Examples

- **OpenAI**: `openai:gpt-4o`, `openai:gpt-4o-mini`
- **Anthropic**: `anthropic:claude-3-5-sonnet-latest`, `anthropic:claude-3-haiku-20240307`
- **Google Gemini**: `google:gemini-1.5-flash`, `google:gemini-1.5-pro`
- **Groq**: `groq:llama-3.3-70b-versatile`
- **Mistral**: `mistral:mistral-large-latest`
- **OpenAI-Compatible (Ollama, vLLM, OpenRouter, Together)**:
  - Set `AI_MODEL="openai_compatible:llama3.2"`
  - Set `LLM_API_BASE="http://localhost:11434/v1"` (or target endpoint)

---

## 3. Browser & Stealth Configuration

Outreach Manager operates in two main browser execution modes:

### Mode A: Native Chrome (Default & Recommended for Local Use)
Connects directly to your local Chrome browser carrying your active LinkedIn login session:
1. Launches Chrome with remote debugging on port `9222`.
2. Connects via Chrome DevTools Protocol (CDP).
3. Script: `run_outreach_manager.ps1` or `start.bat`.

### Mode B: Containerized Playwright (Docker Deployment)
Launches a managed Chromium instance inside Docker:
- Playwright browser cache stored in shared container path `/opt/pw-browsers`.
- Stealth fingerprint scripts (`stealth_profile.py`) automatically inject anti-detection JS for Canvas, WebGL, Navigator, and Permissions.

---

## 4. Logging & Diagnostics Configuration

Outreach Manager features a 3-tier logging architecture configured in `outreach_manager/core/logging.py`:

- **Default (INFO)**: Clean, concise operational updates. Suppresses noisy third-party loggers (`urllib3`, `httpx`, `pydantic_ai`, `playwright`, `fastembed`).
- **Verbose (DEBUG)**: Enable by passing `-v 2` or `--verbosity=2` to management commands.
- **Log Files**: Logs write to `data/outreach.log` and standard console output.

---

## 5. Site & Campaign Settings (Django Admin)

Campaign objectives and buyer personas are stored in the database and manageable via the web dashboard (`http://localhost:8000/admin/`):

- **Campaign (`Campaign` Model)**:
  - `product_docs`: Detailed description of your product/service. Used by AI for profile qualification and message generation.
  - `campaign_objective`: Campaign target persona and outcome goals.
  - `booking_link`: Meeting scheduler link (e.g. `https://cal.com/your-link`) injected into follow-up messages.
- **Site Configuration (`SiteConfig` Model)**:
  - Global `llm_api_key` and fallback system options.

---

## 6. Best Practices & Common Mistakes

- ❌ **Missing Provider Prefix**: Setting `AI_MODEL="gpt-4o"` instead of `AI_MODEL="openai:gpt-4o"`.
- ❌ **Port Conflicts**: Running local Chrome on port `9222` while a headless zombie process is occupying the port. (Launcher scripts `run_outreach_manager.ps1` clean zombie processes automatically).
- ❌ **Excessive Daily Limits**: Setting daily connection limits >100. Keep `CONNECT_DAILY_LIMIT` around 30–50 to protect account health.
