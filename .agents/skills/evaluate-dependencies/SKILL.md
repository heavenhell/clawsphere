---
name: evaluate-dependencies
description: Evaluate whether ClawSphere should reuse existing code or dependencies, adopt a maintained Python or JavaScript package, wrap a third-party API, or implement a capability locally. Use when a request would add or replace a production dependency, reimplement common functionality, choose between libraries, or change requirements or package lock files. Do not trigger for routine use of an already-approved dependency whose suitability is not in question.
---

# Evaluate Dependencies

Produce an evidence-backed reuse-versus-build decision before changing production dependencies or implementing common infrastructure from scratch.

## Workflow

1. Define the required capability, runtime constraints, compatibility needs, security sensitivity, and acceptance criteria. Ask only when a missing answer would materially change the decision.
2. Inspect repository manifests, lock files, imports, helpers, adapters, and neighboring implementations. Determine whether the capability already exists directly or transitively.
3. Prefer candidates in this order: existing project abstraction, standard or platform API, installed dependency, mature third-party package behind a thin adapter, then local implementation.
4. For any external candidate, verify current information from authoritative sources. Check the official documentation, package registry, source repository and releases, security advisories, and license. Do not rely only on model memory.
5. Compare the smallest credible candidate set on:
   - supported Python, Node, browser, and operating-system versions;
   - maintenance and release activity;
   - API stability, typing, tests, and documentation;
   - known vulnerabilities and security model;
   - license and commercial constraints;
   - transitive dependencies, install or bundle size, performance, and operational cost;
   - migration difficulty, vendor coupling, and exit strategy.
6. Reject abandoned, incompatible, unnecessarily broad, insecure, or license-incompatible packages even when they are popular.
7. Recommend one of: reuse existing capability, adopt a dependency, wrap an external API, or implement locally. Explain why rejected alternatives are inferior for this repository.
8. Obtain user confirmation before adding a new production dependency unless the request already names and authorizes it.
9. When authorized to implement, update the appropriate manifest and lock file together, add focused tests, and document operational or licensing implications.

## Decision output

Return a concise record containing:

- required capability and constraints;
- existing reusable options found in the repository;
- candidates and current evidence sources;
- compatibility, security, license, maintenance, and cost comparison;
- recommendation with confidence and tradeoffs;
- approval needed and the exact files that would change.

If current source access is unavailable, say so and avoid presenting package stability or security as verified.
