# Contributing

## Local Development Setup

**Prerequisites:** Python 3.13, FFmpeg (with `ffprobe`) installed and on `PATH`.

```bash
# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

Verify FFmpeg is available:

```bash
ffmpeg -version
ffprobe -version
```

## Running the Service

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

The service is available at `http://localhost:8000`. The interactive API docs are at `http://localhost:8000/docs`.

Environment variables can be set in a `.env` file or exported in your shell before starting. See `.env.example` for all options.

## Code Style

No linter is configured yet. Keep these conventions consistent with the existing code:

- Follow PEP 8 naming and spacing
- Keep functions small and single-purpose; prefer `async def` for I/O-bound work
- Use descriptive names; avoid abbreviations unless they are standard (`ext`, `mb`, etc.)
- Add a docstring to new public functions
- Keep side effects (filesystem, subprocess) at the edges; keep validation logic pure

## Commit Messages

Format: `type(scope): summary`

| Type | Use for |
|------|---------|
| `feat` | new feature |
| `fix` | bug fix |
| `refactor` | code restructure without behavior change |
| `test` | adding or updating tests |
| `docs` | documentation only |
| `chore` | build, deps, tooling |
| `perf` | performance improvement |

Examples:

```
feat(split): add support for opus output format
fix(upload): handle empty file upload gracefully
docs(readme): clarify memory_mode parameter
```

## Pull Request Process

1. Fork the repository
2. Create a feature branch off `main`: `git checkout -b feat/my-feature`
3. Make your changes with clear, focused commits
4. Verify the service starts and the `/health` and `/ready` endpoints respond
5. Open a pull request against `main` with a description of what changed and why

Keep PRs focused. One logical change per PR makes review faster.

## Bug Reports

Open a [GitHub Issue](https://github.com/blackaxgit/audio-splitter/issues) with:

- What you did (request, parameters, file format)
- What you expected
- What actually happened (include the `Reference:` ID from the error response if present)

## License

This project is licensed under the [Mozilla Public License 2.0](LICENSE).

Modifications to existing MPL-licensed files must be released under MPL-2.0. You may combine this code with files under other licenses in a larger work without those other files being affected.
