# MiniCode

MiniCode is a minimal Claude Code style coding agent scaffold. The first milestone is intentionally small:

- LLM: local Ollama HTTP API
- Sandbox: Docker CLI
- Agent loop: JSON actions with structured tools and final answer
- Future extension points: context, harness, memory, skills, self-evolution

## Requirements

- Python 3.11+
- Docker
- Ollama running locally
- A local Ollama model, for example:

```powershell
ollama pull qwen2.5:7b
```

## Quick Start

```powershell
python -m pip install -e .
python -m minicode --check
python -m minicode "inspect the workspace and create a hello.txt file"
```

Or after installing the editable package:

```powershell
minicode "inspect the workspace and create a hello.txt file"
```

## Configuration

Environment variables:

- `MINICODE_MODEL`: Ollama model name, default `qwen2.5:7b`
- `MINICODE_OLLAMA_URL`: Ollama base URL, default `http://127.0.0.1:11434`
- `MINICODE_WORKSPACE`: workspace mounted into Docker, default current directory
- `MINICODE_DOCKER_IMAGE`: sandbox image, default `python:3.12-slim`
- `MINICODE_MAX_STEPS`: agent loop limit, default `8`
- `MINICODE_APPROVAL`: approval mode for risky commands, default `never`

Example:

```powershell
$env:MINICODE_MODEL = "codellama:7b"
python -m minicode "list files"
```

Approval modes:

- `never`: block commands that require approval
- `ask`: prompt in the console before running approval-required commands
- `always`: allow approval-required commands without prompting

Example:

```powershell
python -m minicode --approval ask "run tests and fix failures"
```

## Current Tool Protocol

The model must return one JSON object per turn:

```json
{"thought":"short reasoning","action":"list_files","args":{"path":".","max_depth":2}}
```

or:

```json
{"thought":"done","action":"finish","args":{"answer":"summary for the user"}}
```

Available tools:

- `list_files`: list workspace files with `path`, `max_depth`, and `limit`
- `read_file`: read a bounded line range with `path`, `start_line`, and `limit`
- `write_file`: write a workspace file with `path`, `content`, and `overwrite`
- `run_tests`: run a test command in Docker, default `python -m pytest`
- `run_shell`: fallback shell command in Docker
- `finish`: return the final answer

This will evolve into richer context, harness, memory, skill, and self-improvement modules later.
