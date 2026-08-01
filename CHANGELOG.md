# Changelog

All notable changes to **Outreach Manager** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.0.0] - 2026-08-01

### Added
- **Session-Based Execution Engine**: Introduced `SessionExecutor` to orchestrate outreach in discrete, bounded execution runs with explicit exit guarantees and runtime metric summaries.
- **Stateless Workflow Scheduler**: Built a state-aware `WorkflowScheduler` using Django ORM candidate due filters (`Q(next_action_at__lte=now)`) to dynamically select and execute due tasks.
- **Batch Execution & Pool Exhaustion**: Implemented natural batch workflow loops that process eligible leads until `claim_due_deal()` returns `None`, preventing runaway background loops.
- **Centralized Browser Ownership**: Established `AccountSession` as the single owner of Playwright browser instances, context states, and page pointers.
- **Stealth & Human Movement Simulation**: Integrated stealth fingerprinting injection (`stealth_profile.py`) and Bezier-curve mouse paths with human typing cadence (`human_actions.py`).
- **Robust Browser Crash Recovery**: Added automated browser health checks and single-attempt deal resumption, allowing active deals to recover from browser crashes without losing progress.
- **UI Pre-Validation & Token Protection**: Added `verify_ui_ready` checks before invoking AI generation to prevent wasted LLM calls when browser target elements are unready.
- **Structured AI Generation**: Integrated `pydantic-ai` supporting multi-provider models (`openai`, `anthropic`, `google`, `groq`, `ollama`) with standardized `provider:model` formatting.
- **Infrastructure Quota Classification**: Added classification for HTTP 429 quota errors, treating provider rate limits as operational infrastructure events rather than application bugs.
- **3-Tier Error Accounting & Reporting**: Introduced `WorkflowResult` to categorize execution into processed actions, deal-level errors, workflow exceptions, and fatal errors.
- **Core Workflow Suite**:
  - `CONNECT`: Discovers and sends targeted connection requests.
  - `FIRST_MESSAGE`: Generates personalized opener messages for newly connected leads.
  - `FOLLOW_UP`: Executes multi-turn nudge sequences for non-responsive leads.
  - `REPLY_UNREAD`: Reconciles incoming replies and updates deal outcomes.
  - `CHECK_PENDING`: Synchronizes pending invitations with LinkedIn and withdraws requests older than 7 days.
  - `EXTRACT_LEADS`: Discovers candidate profiles using AI-generated search queries.
- **Production Logging**: Implemented a 3-tier logging architecture providing concise operational timelines, recoverable warnings, and detailed error tracebacks.
- **CRM Control Center**: Built an integrated Django web dashboard to monitor lead funnels, active campaigns, deal states, and live execution logs.

### Changed
- Replaced continuous background daemon loops with bounded execution sessions.
- Centralized browser management under `AccountSession` to eliminate orphaned browser processes and stale context pointers.
- Separated first-message opener generation from follow-up nudge sequences to improve message relevance and conversion tracking.
- Upgraded the AI execution pipeline to `pydantic-ai` for structured outputs and multi-provider compatibility.
- Streamlined configuration to use standardized environment variables (`.env`) and Django model settings.

### Fixed
- Fixed infinite batch workflow loops on deals in cooldown by tracking claimed and processed candidates within the active session.
- Fixed browser recovery failures during active deals by caching generated AI content and reusing it upon browser rebuild.
- Fixed `CHECK_PENDING` synchronization to inspect LinkedIn as the source of truth rather than exiting early based on stale local database counts.
- Fixed candidate due filtering to prevent duplicate deal claims across parallel workflow runs.
- Fixed coroutine execution warnings during synchronous ORM calls in Playwright event loops.

### Notes
- Initial stable public release for Outreach Manager v1.0.
- Future roadmap features (Continuous Automatic Mode, Routine Cadence Planner, Multi-Turn Conversation Strategy Engine) are scheduled for upcoming v1.1 releases.
