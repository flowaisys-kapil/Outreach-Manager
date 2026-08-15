# Outreach Manager Architecture

This document describes the architectural design, system boundaries, component interactions, and execution flow of **Outreach Manager v1.0**.

---

## 1. System Overview

Outreach Manager is structured as a single Python package (`outreach_manager`) containing modular sub-applications for core orchestration, CRM management, LinkedIn automation, AI message generation, and email channels:

```
manage.py
outreach_manager/
├── core/         # Orchestration engine — SessionExecutor, Scheduler, LLM & Logging
├── crm/          # CRM Data Layer — Campaign, Lead, Deal models & funnels
├── linkedin/     # LinkedIn Channel — Browser automation, tasks, ML qualification
├── chat/         # Message History — ChatMessage & multi-turn history
└── emails/       # Email Channel — Email delivery & finder integration
```

### Layering Principles

- **`core`** owns orchestration, session boundaries, LLM infrastructure, logging, and channel-agnostic scheduling.
- **`crm`** owns persistent business domain entities (`Campaign`, `Lead`, `Deal`).
- **`linkedin`** owns Playwright browser interactions, stealth fingerprinting, ML profile ranking, and specific workflow task handlers (`Connect`, `Reply`, `FollowUp`, `CheckPending`, `Extract`).
- **`chat`** owns multi-turn conversation logs and thread summaries.

---

## 2. Component Architecture Diagram

```
+-------------------------------------------------------------------------+
|                              Django Admin /                             |
|                        Dashboard UI (dashboard.html)                    |
+-------------------------------------------------------------------------+
                                     |
                                     v
+-------------------------------------------------------------------------+
|                   rundaemon CLI / SessionExecutor                       |
+-------------------------------------------------------------------------+
                                     |
                                     v
+-------------------------------------------------------------------------+
|                  Workflow Scheduler (scheduler.py)                      |
|            - Weighted Randomization                                     |
|            - State-Aware Candidate Due Filtering                        |
+-------------------------------------------------------------------------+
                                     |
           +-------------------------+-------------------------+
           |                         |                         |
           v                         v                         v
+--------------------+    +--------------------+    +--------------------+
|  Connect Task      |    |  FollowUp / Reply  |    | CheckPending Task  |
|  (handle_connect)  |    |  Tasks             |    | (Phase A & B Sync) |
+--------------------+    +--------------------+    +--------------------+
           |                         |                         |
           +-------------------------+-------------------------+
                                     |
                                     v
+-------------------------------------------------------------------------+
|                    AccountSession & Browser Engine                      |
|            - Stealth Fingerprinting Injection                           |
|            - Bezier Human Movement Simulation                           |
|            - Health Check & Auto-Rebuild Recovery                       |
+-------------------------------------------------------------------------+
```

---

## 3. Session Execution Lifecycle

Outreach Manager runs in discrete, bounded execution sessions managed by `SessionExecutor`:

```
+------------------+     +--------------------+     +-------------------+
| Start Session    | --> | Build Browser      | --> | Execute Workflows |
| (SessionExecutor)|     | (session.ensure_   |     | (BalancedSequence |
|                  |     |  browser())        |     |  Generator)       |
+------------------+     +--------------------+     +-------------------+
                                                              |
                                                              v
+------------------+     +--------------------+     +-------------------+
| Log Session      | <-- | Synthesize Metrics | <-- | Exhaust Batches   |
| Summary          |     | (SessionSummary)   |     | (claim_due_deal)  |
+------------------+     +--------------------+     +-------------------+
```

### Key Responsibilities of `SessionExecutor`

1. **Bounded Run Guarantee**: Sessions execute a single sequence of due workflows and exit cleanly. No runaway loops.
2. **Summary Metric Accounting**: Tracks `processed_actions`, `skipped_actions`, `deal_errors`, `workflow_errors`, and `fatal_errors` separately.
3. **Browser Lifecycle Cleanup**: Ensures Playwright pages and browser contexts are closed gracefully upon completion or failure.

---

## 4. Workflow Scheduler & State-Aware Candidate Filtering

The scheduler (`outreach_manager/linkedin/scheduler.py`) determines which tasks run and which Deals are claimed during a session.

### Workflow Sequence Selection

Workflows run in a randomized sequence governed by `BalancedSequenceGenerator` (weighted by due work volume).

Available workflows:
1. `REPLY_UNREAD`
2. `FOLLOW_UP`
3. `FIRST_MESSAGE`
4. `CHECK_PENDING`
5. `CONNECT`
6. `EXTRACT_LEADS`

### Batch Candidate Filtering (`claim_due_deal`)

When a workflow runs, it claims candidate Deals in a loop until `claim_due_deal()` returns `None`.

Candidate eligibility is **state-aware**:
- For `PENDING` state (`CHECK_PENDING` task):
  ```python
  Q(next_check_pending_at__isnull=True) | Q(next_check_pending_at__lte=now)
  ```
- For `CONNECTED` state (`FOLLOW_UP` task):
  ```python
  Q(next_action_at__isnull=True) | Q(next_action_at__lte=now)
  ```
- For `QUALIFIED` / `READY_TO_CONNECT` state (`CONNECT` task):
  ```python
  Q(next_action_at__isnull=True) | Q(next_action_at__lte=now)
  ```

Once a Deal is claimed, `claimed_at` is stamped. Upon completion or cooldown, `schedule_next_action()` updates state timestamps and clears `claimed_at`.

---

## 5. Centralized Browser Ownership & Recovery

Browser state is centrally owned by `AccountSession` (`outreach_manager/linkedin/browser/registry.py`).

```
+------------------+     +--------------------+     +-------------------+
| Workflow Failure | --> | Health Inspection  | --> | Browser Rebuild   |
| Detected         |     | (is_browser_      |     | (ensure_browser() |
|                  |     |  healthy())        |     |  reconnects CDP)  |
+------------------+     +--------------------+     +-------------------+
                                                              |
                                                              v
+------------------+                                +-------------------+
| Resume Batch     | <----------------------------- | Retry Active Deal |
| Execution        |                                | (Single Attempt)  |
+------------------+                                +-------------------+
```

### Stealth & Human Action Layer

1. **Stealth Fingerprinting** (`stealth_profile.py`): Injects anti-bot evasion scripts into every page load (WebGL noise, navigator overrides, permissions mocks).
2. **Human Simulation** (`human_actions.py`): Simulates human interactions via Bezier-curve mouse movements, variable typing delays, and organic page scrolling.

### Auto-Rebuild & Recovery

If Playwright crashes or a context disconnects during a Deal:
1. `is_browser_healthy()` detects page death.
2. `session.ensure_browser()` automatically rebuilds the Playwright instance and reconnects to Chrome.
3. The workflow retries the active Deal once (`deal_retry_count <= 1`) using cached LLM message content to avoid duplicate AI generation.

---

## 6. Deal Lifecycle & State Machine

Every lead progresses through a strict Finite State Machine (FSM):

```
                       +-------------------+
                       |     QUALIFIED     |
                       +-------------------+
                                 |
                                 v
                       +-------------------+
                       | READY_TO_CONNECT  |
                       +-------------------+
                                 | (CONNECT Task)
                                 v
                       +-------------------+
                       |      PENDING      | <---+ (Backoff / 7d check)
                       +-------------------+ ----+
                                 | (Acceptance)
                                 v
                       +-------------------+
                       |     CONNECTED     |
                       +-------------------+
                                 | (FIRST_MESSAGE Task)
                                 v
                       +-------------------+
                       |     MESSAGED      | <---+ (FOLLOW_UP Nudges)
                       +-------------------+ ----+
                                 | (Lead Replies)
                                 v
                       +-------------------+
                       |      REPLIED      |
                       +-------------------+
```

### State Definitions

- `QUALIFIED`: Profile discovered and scored eligible by Bayesian ML model.
- `READY_TO_CONNECT`: Passed confidence thresholds, ready for connection request.
- `PENDING`: Connection request sent; awaiting acceptance on LinkedIn.
- `CONNECTED`: Connection accepted; eligible for first message.
- `MESSAGED`: First message sent; eligible for multi-turn follow-up nudges.
- `REPLIED`: Prospect replied; conversation active.
- `FAILED`: Permanently dropped (e.g. withdrawn 3 times or hard error).

---

## 7. AI Layer & Prompt Pipeline

Outreach Manager uses `pydantic-ai` for structured LLM interactions across providers (OpenAI, Anthropic, Gemini, Groq, Ollama).

### UI Pre-Validation & Token Efficiency

To prevent wasted API calls:
1. **UI Pre-Validation** (`verify_ui_ready`): Workflows verify that the browser page is live and the target conversation UI is open **BEFORE** calling the LLM.
2. **Content Caching**: If message generation succeeds but browser sending fails, the generated text is cached in local memory. Upon browser recovery, the cached message is reused without re-querying the LLM.

### Quota Exhaustion Handling (HTTP 429)

Provider 429 quota exhaustion is treated as an operational infrastructure event:
- Classified via `is_quota_error(exc)`.
- Suppresses long Python tracebacks in logs.
- Defers the active Deal cleanly via `schedule_next_action(deal, "quota_exhausted")`.
- Continues execution without marking the session as a developer bug failure.

---

## 8. 3-Tier Production Logging

Outreach Manager separates operational metrics from developer diagnostics:

1. **Operational Timeline (INFO)**: Clean, human-readable status updates (`[INF] ▶ connect john-doe`, `[INF] Still Pending (Backoff 24.0h)`).
2. **Recoverable Operational Warnings (WARNING)**: Non-fatal infrastructure events like browser restarts or quota exhaustion.
3. **Unexpected Developer Bugs (ERROR / EXCEPTION)**: Emits full Python tracebacks only for unexpected code failures, missing attributes, or syntax errors.

---

## 9. Architectural Rationale

| Design Choice | Rationale |
|---|---|
| **SessionExecutor** | Encapsulates execution in discrete, audit-friendly runs. Prevents memory leaks and runaway processes. |
| **Batch Workflow Loops** | Workflows process eligible deals in natural batches, enforcing rate limits and pool exhaustion. |
| **Centralized Browser Ownership** | Single point of recovery (`AccountSession`). Prevents orphaned browser processes and stale context pointers. |
| **Lightweight Scheduler** | Stateless candidate filters executed directly against Django ORM models. Zero daemon lock-in. |
