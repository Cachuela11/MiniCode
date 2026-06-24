---
name: python_test_repair
description: Fix failing Python unit tests, assertion errors, import errors, and simple implementation bugs.
tags: [python, test, unittest, pytest]
intents: [fix_failing_test, debug_test_failure]
tools: [list_files, read_file, write_file, run_tests]
triggers: [测试失败, 单元测试, pytest, unittest, AssertionError, failing test, test failure]
---

## Workflow

1. Use `run_tests` to reproduce or inspect the failure when no failure output is provided.
2. Use `read_file` to inspect the failing test.
3. Use `read_file` to inspect the implementation under test.
4. Use `write_file` for the smallest targeted fix.
5. Use `run_tests` again to verify the fix.

## Boundaries

- Do not delete or weaken tests just to pass.
- Prefer fixing implementation code over changing tests.
- Keep changes focused on the failing behavior.

## Completion Criteria

- The relevant test command passes.
- The final answer names the changed file and verification result.
