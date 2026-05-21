# Contributing to WPBot

Thanks for taking an interest in this project. WPBot is a personal open-source side project — thoughtful contributions are welcome.

## Before you start

1. **Search existing issues** on [GitHub](https://github.com/dogukannparlak/whatsappbot/issues) to avoid duplicates.
2. For larger changes, open an issue first to discuss approach and scope.
3. Keep PRs **focused and small**. One logical change per pull request is easier to review.

## Development setup

```bash
git clone https://github.com/dogukannparlak/whatsappbot.git
cd whatsappbot
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
cp .env.example .env
# Configure MySQL in .env, then:
python main.py
```

Tables are created automatically on first run (`init_db()` in `db.py`).

## Pull request workflow

1. Fork the repository.
2. Create a branch from `main` (e.g. `fix/docs-typo` or `feat/retry-backoff`).
3. Make your changes with clear commit messages.
4. Test manually against a local MySQL instance and a WhatsApp Web session you own.
5. Open a PR describing **what** changed and **why**.

## Code style

- Match the existing module layout (`main.py`, `api.py`, `whatsapp.py`, `db.py`, …).
- Prefer **minimal diffs** — no drive-by refactors unrelated to the issue.
- Use type hints where the surrounding file already uses them.
- Comments only for non-obvious behavior; let the code speak when possible.

## Security — do not commit

- `.env` or any file with real database passwords
- `Browser/` Chrome profile directories (session / QR login data)
- `logs/`, `*.log`, local `*.db` files

These paths are listed in `.gitignore`; double-check before pushing.

## Behavior

Be respectful in issues and reviews. Harassment or spam will not be tolerated.

## Questions

Open a [GitHub issue](https://github.com/dogukannparlak/whatsappbot/issues) with the `question` label if something is unclear.

Thank you for helping improve WPBot.
