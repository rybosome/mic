"""Optional sinks for already-computed evaluation results."""

from mic.reporters.console import format_summary
from mic.reporters.html import render_report, write_report

__all__ = ["format_summary", "render_report", "write_report"]
