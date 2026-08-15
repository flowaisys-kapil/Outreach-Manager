# outreach_manager/core/session_summary.py
"""Authoritative structured summary of an outreach session."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from termcolor import colored

logger = logging.getLogger(__name__)


@dataclass
class SessionSummary:
    """Authoritative structured summary of an outreach session."""
    start_time: datetime
    finish_time: datetime | None = None
    duration_seconds: float = 0.0
    workflows_executed: list[str] = field(default_factory=list)
    workflows_skipped: list[str] = field(default_factory=list)
    workflows_disabled: list[str] = field(default_factory=list)
    workflows_limit_reached: list[str] = field(default_factory=list)
    workflows_no_work: list[str] = field(default_factory=list)
    actions_performed: int = 0
    deal_errors: int = 0
    workflow_errors: int = 0
    fatal_errors: int = 0
    browser_recoveries: int = 0      # successful browser recoveries during this session
    llm_deferrals: int = 0           # clean LLM quota deferrals during this session
    diagnostics_generated: int = 0    # count of diagnostic packages saved
    ai_calls: int = 0
    primary_provider: str = ""
    fallback_used: bool = False
    provider_failures: int = 0
    errors: list[str] = field(default_factory=list)
    aggregated_metrics: dict[str, int] = field(default_factory=dict)

    color_output: bool = True
    execution_mode: str = "manual"

    @property
    def total_errors(self) -> int:
        return self.deal_errors + self.workflow_errors + self.fatal_errors

    def log_summary(self, logger_obj=None, colored_func=None) -> None:
        """Output structured operational session summary to console/log."""
        if logger_obj is None:
            try:
                import outreach_manager.core.session_executor as se
                logger_obj = se.logger
            except Exception:
                logger_obj = logger
        if colored_func is None:
            try:
                import outreach_manager.core.session_executor as se
                colored_func = se.colored
            except Exception:
                colored_func = colored

        def _c(text: str, color: str, bold: bool = False) -> str:
            if not self.color_output:
                return text
            attrs = ["bold"] if bold else []
            return colored_func(text, color, attrs=attrs)

        dur_str = f"{int(self.duration_seconds // 60)}m {int(self.duration_seconds % 60)}s"
        W = 60
        DIV = "-" * W
        SEP = "=" * W

        if self.fatal_errors > 0 or (self.workflow_errors > 0 and self.actions_performed == 0):
            status_line = _c("✖  Session Failed", "red", bold=True)
            status_color = "red"
        elif self.total_errors > 0 or self.browser_recoveries > 0 or self.fallback_used:
            status_line = _c("⚠  Completed with Issues", "yellow", bold=True)
            status_color = "yellow"
        elif self.actions_performed == 0:
            status_line = _c("ℹ  No Work Available", "cyan", bold=True)
            status_color = "cyan"
        else:
            status_line = _c("✔  Completed Successfully", "green", bold=True)
            status_color = "green"

        exec_mode_str = self.execution_mode.capitalize()
        start_str  = self.start_time.strftime("%H:%M:%S") if self.start_time else "N/A"
        finish_str = self.finish_time.strftime("%H:%M:%S") if self.finish_time else "N/A"

        lines: list[str] = []
        lines.append(_c(SEP, status_color, bold=True))
        lines.append(_c(f"{'OUTREACH SESSION SUMMARY':^{W}}", status_color, bold=True))
        lines.append(_c(SEP, status_color, bold=True))

        lines.append("")
        lines.append(_c("Status", "white", bold=True))
        lines.append(DIV)
        lines.append(status_line)
        lines.append("")
        lines.append(f"  {'Execution Mode':<22}{exec_mode_str}")
        lines.append(f"  {'Start Time':<22}{start_str}")
        lines.append(f"  {'Finish Time':<22}{finish_str}")
        lines.append(f"  {'Duration':<22}{dur_str}")

        metric_label_map: dict[str, str] = {
            "connection_requests_sent": "Connection Requests Sent",
            "first_messages_sent":      "First Messages Sent",
            "replies_sent":             "Replies Sent",
            "follow_ups_sent":          "Follow-Ups Sent",
            "pending_requests_checked": "Pending Requests Checked",
            "accepted_connections":     "Accepted Connections",
            "withdrawn_requests":       "Withdrawn Requests",
            "leads_extracted":          "Leads Extracted",
            "emails_sent":              "Emails Sent",
        }
        ordered_keys = list(metric_label_map.keys())
        extra_keys = [k for k in self.aggregated_metrics if k not in ordered_keys]
        display_metrics = [
            (metric_label_map.get(k, k.replace("_", " ").title()), v)
            for k in ordered_keys + extra_keys
            if self.aggregated_metrics.get(k, 0) > 0
            for v in [self.aggregated_metrics[k]]
        ]

        if display_metrics:
            lines.append("")
            lines.append(_c("Work Completed", "white", bold=True))
            lines.append(DIV)
            for label, value in display_metrics:
                lines.append(f"  {label:<30}{value:>6}")

        lines.append("")
        lines.append(_c("Workflow Results", "white", bold=True))
        lines.append(DIV)

        def _fmt_wf(name: str) -> str:
            return name.replace("_", " ").title()

        if self.workflows_executed:
            lines.append("  Executed")
            for w in self.workflows_executed:
                lines.append(f"    {_c('✓', 'green')} {_fmt_wf(w)}")
        if self.workflows_disabled:
            lines.append("  Disabled")
            for w in self.workflows_disabled:
                lines.append(f"    {_c('•', 'yellow')} {_fmt_wf(w)}")
        if self.workflows_no_work:
            lines.append("  No Work")
            for w in self.workflows_no_work:
                lines.append(f"    {_c('•', 'cyan')} {_fmt_wf(w)}")
        if self.workflows_limit_reached:
            lines.append("  Daily Limit Reached")
            for w in self.workflows_limit_reached:
                lines.append(f"    {_c('•', 'magenta')} {_fmt_wf(w)}")
        if not (self.workflows_executed or self.workflows_disabled
                or self.workflows_no_work or self.workflows_limit_reached):
            lines.append("  No workflows ran.")

        lines.append("")
        lines.append(_c("AI Usage", "white", bold=True))
        lines.append(DIV)
        lines.append(f"  {'Primary Provider':<22}{self.primary_provider or 'N/A'}")
        lines.append(f"  {'Fallback Provider':<22}{'N/A' if not self.fallback_used else 'Yes'}")
        lines.append(f"  {'AI Calls':<22}{self.ai_calls}")
        lines.append(f"  {'Fallback Used':<22}{'Yes' if self.fallback_used else 'No'}")
        lines.append(f"  {'Provider Failures':<22}{self.provider_failures}")

        lines.append("")
        lines.append(_c("Errors", "white", bold=True))
        lines.append(DIV)
        lines.append(f"  {'Deal Errors':<22}{self.deal_errors}")
        lines.append(f"  {'Workflow Errors':<22}{self.workflow_errors}")
        lines.append(f"  {'Fatal Errors':<22}{self.fatal_errors}")
        lines.append(f"  {'Browser Recoveries':<22}{self.browser_recoveries}")
        lines.append(f"  {'LLM Deferrals':<22}{self.llm_deferrals}")
        lines.append(f"  {'Diagnostics Generated':<22}{self.diagnostics_generated}")
        lines.append(f"  {'Total Errors':<22}{self.total_errors}")

        lines.append("")
        lines.append(_c("Recommendation", "white", bold=True))
        lines.append(DIV)
        recommendation = self._derive_recommendation()
        for sentence in recommendation:
            lines.append(f"  {sentence}")

        lines.append("")
        lines.append(_c(SEP, status_color, bold=True))

        logger_obj.info("\n%s", "\n".join(lines))

    def _derive_recommendation(self) -> list[str]:
        """Derive operational recommendation based on session summary."""
        if self.fatal_errors > 0:
            return [
                "Session encountered a fatal error.",
                "Review Session History and address the issue before the next session.",
            ]
        if self.workflow_errors > 0 and self.actions_performed == 0:
            return [
                "All workflows failed without completing any work.",
                "Review Session History for failed workflows.",
            ]
        if self.workflow_errors > 0:
            return [
                "Some workflows encountered errors.",
                "Review Session History for details.",
            ]
        if self.browser_recoveries > 0 and self.fallback_used:
            return [
                "Browser recovered and fallback provider was used.",
                "Review provider health if this becomes frequent.",
            ]
        if self.browser_recoveries > 0:
            return [
                "Browser recovered successfully.",
                "No further action required.",
            ]
        if self.fallback_used:
            return [
                "Fallback provider was used.",
                "Review provider health if this becomes frequent.",
            ]
        if self.deal_errors > 0:
            return [
                "Some deals encountered errors but the session completed.",
                "Review Session History for details.",
            ]
        if self.actions_performed == 0:
            return ["No eligible outreach work was available."]
        return [
            "No action required.",
            "Session completed successfully.",
        ]
