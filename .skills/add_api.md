---
name: add_api
description: Add a missing public function, class, endpoint, or small API surface requested by tests or user instructions.
tags: [api, function, implementation, python]
intents: [add_api, implement_missing_function]
tools: [list_files, read_file, write_file, run_tests]
triggers: [新增 API, 添加函数, 实现函数, missing function, import error, cannot import, add API]
---

## Workflow

1. Use `list_files` to find the likely module.
2. Use `read_file` to inspect existing naming and style.
3. Use `write_file` to add the smallest compatible API.
4. Use `run_tests` when tests exist.

## Boundaries

- Match the existing module style.
- Avoid broad refactors while adding the API.
- Preserve existing public behavior.

## Completion Criteria

- The requested API exists at the expected import path.
- Related tests pass or the final answer explains why they could not be run.
