# Outreach Manager System Architecture

This document provides a reference guide for the technical architecture of **Outreach Manager v1.0**.

For the primary, detailed architectural specification, component diagrams, state machines, and design trade-offs, please see the root [ARCHITECTURE.md](../ARCHITECTURE.md).

---

## High-Level Summary

Outreach Manager operates as a modular, session-driven campaign engine:

1. **Input**: Candidate profiles are discovered on LinkedIn via targeted search queries and enriched via Voyager API data.
2. **Qualification**: Profiles are scored using a Gaussian Process Regressor with fastembed vector embeddings, balancing exploration and exploitation.
3. **Execution Session**: The `SessionExecutor` orchestrates discrete, bounded sessions (`python manage.py rundaemon`).
4. **Workflow Scheduling**: The stateless `WorkflowScheduler` evaluates candidate deals against due timestamps (`Q(next_action_at__lte=now)`) and dispatches workflow tasks (`Connect`, `Reply`, `FollowUp`, `CheckPending`).
5. **Browser Engine**: Centralized browser ownership via `AccountSession`, Playwright automation, stealth fingerprinting scripts (`stealth_profile.py`), and Bezier mouse movement (`human_actions.py`).
6. **State Tracking**: Lead progress is tracked via the Deal finite state machine (`QUALIFIED` → `READY_TO_CONNECT` → `PENDING` → `CONNECTED` → `MESSAGED` → `REPLIED`).
