# PRIVACY NOTICE – Outreach Manager

**Last Updated: August 2026**

This Privacy Notice outlines how data is handled by **Outreach Manager**, an open-source, self-hosted campaign execution engine. Outreach Manager is designed around a **privacy-first, local-only execution model**. 

---

## 1. Local-Only Data Architecture

Outreach Manager runs entirely within your self-hosted infrastructure. All data processed by the application — including target lead profiles, campaign objectives, message logs, AI response caches, vector embeddings, and browser session cookies — is stored exclusively on your local machine or server.

- **No Remote Telemetry**: Outreach Manager does not collect, transmit, or centralize usage statistics, lead databases, or account telemetry to any external servers.
- **No Shared Contact Pools**: All profile discovery, qualification scoring, and message history remain 100% private to your local deployment.

---

## 2. Types of Data Processed

When operating an Outreach Manager campaign, the application processes the following data types locally:

| Category | Data Fields | Storage Location |
|:---|:---|:---|
| **Account Credentials** | LinkedIn email and password (or session cookies) | Local `.env` / Browser Storage (`data/chrome_profile/`) |
| **Lead Profiles** | LinkedIn public URLs, names, titles, company names, profile descriptions | Local SQLite Database (`data/db.sqlite3`) |
| **Vector Embeddings** | 384-dimensional numeric vector arrays computed locally via fastembed | Local SQLite Database (`data/db.sqlite3`) |
| **Outreach History** | Generated connection notes, follow-up messages, chat thread summaries, state timestamps | Local SQLite Database (`data/db.sqlite3`) |
| **System Diagnostics** | Execution timelines, warning logs, and error tracebacks | Local Log File (`data/outreach.log`) |

---

## 3. Data Controller Responsibilities

When deploying Outreach Manager, **you (the operator) act as the Data Controller** for all personal data collected and processed through your campaigns.

As the Data Controller, you are responsible for:
- Ensuring a valid legal basis for processing B2B contact data under applicable privacy laws (e.g. GDPR, CCPA, LGPD).
- Honoring data subject access, correction, and erasure requests from prospects in your pipeline.
- Securing your local server environment, database files, and API credentials.

---

## 4. Third-Party Integrations (LLM Providers)

Outreach Manager interfaces with third-party LLM providers (e.g., OpenAI, Anthropic, Google Gemini, Groq) to generate personalized outreach content and classify profile data.

- **Data Sent to AI Providers**: Profile descriptions, company names, and campaign objectives are sent via API calls strictly to format personalized messages and evaluate lead fit.
- **Provider Policies**: Data transmitted via commercial APIs is governed by the respective AI provider's privacy policy and terms of service. Providers typically do not use commercial API data for model training.
- **Self-Hosted LLM Support**: Operators seeking complete air-gapped isolation can configure local LLM providers (e.g. Ollama or vLLM) via `LLM_API_BASE`.

---

## 5. Data Subject Rights & Data Erasure

Prospects whose profiles reside in your local Outreach Manager database can be updated or deleted at any time:
- **Deletion**: Remove individual Lead and Deal records via the Django Web Dashboard (`http://localhost:8000/admin/`) or Django management shell.
- **Full System Wipe**: Wiping the `data/db.sqlite3` file completely removes all persistent lead data, chat history, and qualification scores.
