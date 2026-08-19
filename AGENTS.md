# ClawSphere engineering guidance

## Scope and context

- Read the relevant implementation, neighboring code, tests, and documentation before editing.
- Preserve unrelated user changes in a dirty worktree. Do not discard or rewrite them.
- Prefer the smallest change that fully satisfies the requested behavior and follows existing patterns.

## Requirements and clarification

- Before coding, identify the goal, affected behavior, constraints, non-goals, and observable completion criteria.
- Ask the user a concise clarifying question when an unresolved choice would materially change the public API, data model, security posture, destructive behavior, user experience, compatibility, dependency set, or acceptance criteria.
- Do not ask about facts that can be discovered safely from the repository, tests, configuration, or authoritative documentation.
- For low-risk ambiguity, proceed with the most conservative reasonable assumption and state it in the work update or final handoff.

## Dependency policy

- Reuse existing project code, Python standard-library modules, browser/platform APIs, and already-installed dependencies before adding code or packages.
- Invoke the project `evaluate-dependencies` skill before adding or replacing a production dependency, or before implementing a common capability that likely has a mature maintained library.
- Base third-party decisions on current official documentation, package metadata, release activity, compatibility, license, security advisories, and transitive cost. Do not rely only on model memory or popularity.
- Explain the recommendation and obtain user confirmation before adding a new production dependency unless the user explicitly authorized that dependency in the request.
- Prefer a thin adapter around a stable dependency so project code is not unnecessarily coupled to a vendor API.

## Implementation

- Keep security decisions deterministic in code. Do not weaken RBAC, HITL approval, tenant isolation, rate limits, grounding verification, or credential handling for convenience.
- Maintain backward compatibility unless the request explicitly allows a breaking change.
- Do not leave placeholders, silent exception handling, dead branches, or speculative abstractions.
- Never commit secrets, `.env` contents, private certificates, runtime logs, generated data, or sensitive payloads.

## Verification

- Add or update tests for every behavior change when a meaningful automated test is feasible. Cover the success path, relevant failure paths, edge cases, and the reported regression.
- Run the narrowest relevant tests first, then the broader applicable checks.
- Backend default: `python -m pytest -q`.
- Frontend default: run `pnpm build` from `frontend/`. Add focused frontend tests when a test framework exists or when introducing one has been explicitly approved.
- Do not claim a check passed unless it was actually run. Report skipped or blocked checks and the exact reason.

## Self-review and completion

- After implementation and tests, inspect the final diff using `docs/code_review.md` before handing work back.
- Fix findings caused by the change and rerun affected checks. Keep review-only observations about unrelated pre-existing code separate.
- Work is complete only when the requested behavior is implemented, relevant tests are added or updated, applicable checks pass, the final diff has been reviewed, and remaining risks or assumptions are disclosed.
