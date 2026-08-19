# ClawSphere code review checklist

Use this checklist for the final diff. Report actionable findings first, ordered by severity, with file/line evidence, impact, and a concrete correction. Do not report formatting preferences already enforced mechanically.

## Correctness and compatibility

- Confirm the implementation matches the stated requirement and completion criteria.
- Check failure paths, boundary values, empty or malformed inputs, state transitions, retries, and idempotency.
- Check public API, configuration, persistence, and response-shape compatibility.

## Agent behavior and safety

- Preserve the four-stage planning flow, LLM-call budgets, route-decision observability, and grounding verification.
- Ensure write operations cannot bypass RBAC, risk checks, tenant boundaries, rate limits, or durable HITL approval.
- Ensure fallbacks and exception paths fail safely and do not fabricate successful results.
- Check concurrent session, checkpoint, approval, and memory updates for ordering or race problems.

## Data and operations

- Prevent secrets, credentials, tokens, certificates, sensitive prompts, and raw platform payloads from entering source control or unsafe logs.
- Check audit and application logs for retention, rotation, redaction, bounded size, and failure isolation.
- Check new configuration for secure production defaults and explicit demo-only behavior.

## Dependencies and maintainability

- Confirm new functionality does not duplicate an existing helper or dependency.
- Require a documented `evaluate-dependencies` decision for every new production package.
- Prefer cohesive, testable changes and avoid unnecessary coupling or speculative abstractions.

## Tests and handoff

- Require focused tests for new behavior and regressions, including meaningful error and security paths.
- Verify that reported commands were actually run and that results correspond to the final diff.
- Confirm documentation or examples changed when externally visible behavior changed.
