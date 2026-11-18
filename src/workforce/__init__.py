"""AI Workforce: CrewAI child workers managed by Claude Code (the parent)."""

import os

# Privacy: turn off CrewAI/OpenTelemetry anonymous telemetry before crewai is
# ever imported. Values in the environment or .env still take precedence.
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

__version__ = "0.1.0"
