# Contributing to AllDebrid-Client

Thank you for considering contributing! Here's how to get involved.

---

## Getting Started

1. **Fork** the repository and clone your fork:
   ```bash
   git clone https://github.com/your-username/alldebrid-client.git
   cd alldebrid-client
   ```

2. **Set up the development environment:**
   ```bash
   cd backend
   pip install -r requirements.txt
   ```

3. **Run locally:**
   ```bash
   uvicorn main:app --reload --host 0.0.0.0 --port 8080
   ```

4. **Or with Docker:**
   ```bash
   docker compose up --build
   ```

---

## How to Contribute

### Reporting Bugs

- Search existing issues before opening a new one.
- Include steps to reproduce, expected behavior, and actual behavior.
- Include your OS, Python version, and Docker version if relevant.

### Suggesting Features

- Open an issue with the label `enhancement`.
- Describe the use case clearly — what problem does it solve?

### Submitting a Pull Request

1. Create a branch from `main`:
   ```bash
   git checkout -b feat/my-feature
   # or
   git checkout -b fix/my-bugfix
   ```

2. Make your changes. Keep commits focused and atomic.

3. Write a clear commit message (English):
   ```
   feat: add RSS feed support
   fix: resolve SQLite constraint error on duplicate hash
   docs: update README with aria2 config example
   ```

4. Push and open a Pull Request against `main`.

5. Fill in the PR template — what changed, why, and how to test it.

---

## Branch Naming

| Prefix | Purpose |
|--------|---------|
| `feat/` | New feature |
| `fix/` | Bug fix |
| `docs/` | Documentation only |
| `chore/` | Maintenance, deps, CI |
| `refactor/` | Code restructure without behavior change |

---

## Code Style

- Python: follow PEP 8, use `async`/`await` consistently.
- Keep functions small and single-purpose.
- All commit messages and code comments in **English**.
- No unnecessary dependencies — keep the footprint small.

---

## Versioning

This project uses [Semantic Versioning](https://semver.org/):

- `MAJOR` — breaking changes
- `MINOR` — new backwards-compatible features
- `PATCH` — bug fixes

Update `CHANGELOG.md` with your changes under `[Unreleased]` when submitting a PR.

---

## Questions?

Open an issue or start a discussion. We're happy to help.
