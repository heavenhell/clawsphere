"""Command-line entry point for exporting sanitized session audit logs."""

from backend.agent.audit_log import main


if __name__ == "__main__":
    raise SystemExit(main())
