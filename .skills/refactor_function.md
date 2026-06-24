---
name: refactor_function
description: Refactor a function for clarity while preserving behavior.
tags: [refactor, cleanup, maintainability]
intents: [refactor_function]
tools: [list_files, read_file, write_file, run_tests]
triggers: [重构, refactor, cleanup, simplify, improve readability]
---

## Workflow

1. Use `read_file` to inspect the target function and nearby tests.
2. Identify behavior that must stay unchanged.
3. Use `write_file` for a focused refactor.
4. Use `run_tests` or a targeted command to verify behavior.

## Boundaries

- Do not change behavior unless explicitly requested.
- Keep the public interface stable.
- Avoid unrelated formatting churn.

## Completion Criteria

- The code is clearer and existing behavior is preserved.
- Verification passes or the final answer explains verification limits.
