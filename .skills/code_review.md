---
name: code_review
description: Review code for bugs, regressions, missing tests, and safety risks.
tags: [review, bugs, risk, tests]
intents: [code_review]
tools: [list_files, read_file, run_tests]
triggers: [review, code review, 审查, 看一下代码, 风险, bug]
---

## Workflow

1. Use `list_files` to understand the repository shape.
2. Use `read_file` to inspect files relevant to the request.
3. Focus on correctness, regressions, safety risks, and missing tests.
4. Use `run_tests` only when it helps confirm a finding.

## Boundaries

- Do not rewrite code during review unless explicitly asked.
- Findings should be specific and tied to files or behavior.
- Prefer high-signal issues over style-only comments.

## Completion Criteria

- Findings are ordered by severity.
- Each finding explains impact and evidence.
