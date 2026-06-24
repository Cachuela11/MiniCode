---
name: input_validation
description: Add validation for user input, config values, function arguments, or boundary ranges.
tags: [validation, input, errors, robustness]
intents: [add_input_validation]
tools: [read_file, write_file, run_tests]
triggers: [输入校验, 参数校验, validate, validation, invalid input, ValueError, boundary]
---

## Workflow

1. Use `read_file` to inspect the current input handling.
2. Identify valid and invalid cases from the task or tests.
3. Use `write_file` to add explicit validation and clear errors.
4. Use `run_tests` to verify valid and invalid paths.

## Boundaries

- Do not silently coerce invalid values unless requested.
- Preserve accepted valid inputs.
- Prefer clear exceptions for invalid data.

## Completion Criteria

- Valid inputs still work.
- Invalid inputs fail predictably.
- Related tests pass.
