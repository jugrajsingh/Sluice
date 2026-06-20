# Contributing to Sluice

Thank you for your interest in contributing. This document covers how to report issues, submit
pull requests, and what to expect from the process.

---

## Code of Conduct

Be respectful and constructive. Contributions of all kinds are welcome — bug reports, docs,
tests, and code.

---

## Reporting issues

Open a GitHub issue with:

- A clear title and description of the problem.
- Steps to reproduce (minimal reproduction preferred).
- Sluice version, Python version, and relevant deployment context (k8s, queue backend, etc.).
- Error output, logs, or a stack trace if applicable.

For security vulnerabilities, **do not open a public issue**. Email
jugrajskhalsa@gmail.com directly.

---

## Development setup

See `README.md` for the full quickstart and `CLAUDE.md` for the internal project map.

The short version:

```bash
# 1. Install uv (https://docs.astral.sh/uv/getting-started/installation/)
# 2. Sync the workspace venv
uv sync

# 3. Run all tests
uv run pytest packages

# 4. Lint and format
uv run ruff check .
uv run ruff format .

# 5. Type-check
uv run mypy .
```

All CI checks (lint, format, type-check, tests) must pass before a PR is merged. The
pre-commit hooks enforce this locally — run `pre-commit install` once after cloning.

---

## Submitting a pull request

1. Fork the repository and create a feature branch off `main`.
2. Make your changes. New behavior should have tests.
3. Ensure `uv run pytest packages`, `uv run ruff check .`, and `uv run mypy .` all pass.
4. Open a PR against `main` with a clear description of the change and the motivation.
5. **Sign the CLA** (see below). The cla-assistant bot will prompt you on the PR if you have
   not signed yet. The PR cannot be merged until the CLA is on file.

For significant new features or architectural changes, open an issue first so the design can
be discussed before you invest significant time coding.

---

## Contributor License Agreement (CLA)

### Why a CLA is required

Sluice is licensed under **AGPL-3.0-or-later** and is also offered under a **commercial license**
(see `COMMERCIAL-LICENSE.md`). This dual-licensing model is only sustainable if the maintainer
can grant commercial licenses that cover all contributions — not just the original code.

Without a CLA, every contributor implicitly licenses their work inbound under the AGPL only.
That would mean the maintainer could not include your contribution in a commercial build without
violating the AGPL — effectively blocking the commercial license for any product containing your
code. The CLA solves this by granting the maintainer an additional, sublicensable license to use
contributions under any terms, including commercial ones. **You retain full copyright in your
work**; you are simply granting an extra license on top of the AGPL grant.

### How to sign

Sluice uses **[cla-assistant.io](https://cla-assistant.io/)** to collect CLA signatures.

When you open a pull request, the cla-assistant GitHub App will post a comment asking you to
sign. Click the link, review `CLA.md` (in the repository root), and sign with your GitHub
account. The signature is recorded and you will not be asked again on future PRs.

**Entity contributors** (companies, organizations): the CLA must be signed by an authorized
representative. Please coordinate with jugrajskhalsa@gmail.com before submitting your first PR.

### CLA setup note (for maintainer reference)

To activate cla-assistant for this repository:
1. Go to [cla-assistant.io](https://cla-assistant.io/) and sign in with the maintainer GitHub
   account.
2. Link the repository and point the CLA URL to `CLA.md` in the default branch.
3. The GitHub App will automatically comment on new PRs from contributors who have not yet signed.

---

## What happens after you submit

- A maintainer will review your PR, usually within a week.
- You may be asked for changes; please respond within a reasonable time or the PR may be closed.
- Once approved and the CLA is on file, your PR will be merged.

---

## License

By contributing to Sluice you agree that your contributions are licensed under
AGPL-3.0-or-later (the project's open-source license) and additionally under the terms of the
Contributor License Agreement in `CLA.md`.
