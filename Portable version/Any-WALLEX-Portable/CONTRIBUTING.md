# Contributing to Any WALLEX

Thank you for considering a contribution. This project protects real trading
accounts, so the bar for correctness is high — these rules exist so your work
survives review quickly.

## Ground rules

1. **Paper-first.** Every feature must work in paper mode before it can touch
   the live path. Live-mode changes require extra scrutiny (see below).
2. **No behavior change without a verified run.** "Should work" is not done.
   Verify through `create_app` + TestClient or a real boot, and say *how* you
   verified in the MR description.
3. **The suite must stay green.** `python -m pytest tests/ -q` — currently 133
   tests, under 6 seconds. New money-path code (brokers, storage, orders)
   requires new tests in the same MR.
4. **Secrets never enter the repo.** No API keys, no `data/` contents, no
   `secrets.enc`, no `.key_password`. The `.gitignore` covers the common paths;
   you are the last line of defense.
5. **AI output is data, not instructions.** Anything parsed from an LLM
   response must be schema-validated before it can affect orders or settings.

## Getting started

```bash
git clone <your-fork>
cd Any-WALLEX-Portable   # or the full repo
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
python -m pytest tests/ -q      # must pass before you start
python launcher.py              # explore the app (paper mode)
```

Read the repository `README.md` — the **Cognitive Map** routes you to the two
files that matter for your role, and the **ADR table** records decisions you
should not re-litigate (single-file frontend, SQLite, profile interpreter…).

## Code conventions

- Match the existing style: docstring at the top explaining *why*, type hints
  on public functions, Persian comments are welcome in `config.yaml` context.
- Every `except` must either handle, log, or re-raise — silent `pass` is
  reserved for provably-noise paths and must carry a `log.debug`.
- Destructive endpoints require a confirmation payload
  (`{"confirm": "RESET-ALL"}` pattern).
- Frontend: one file (`web/index.html`), RTL-aware, all strings through
  `L_(en, fa)`, all injected data through `escHtml()`.

## Live-trading path: extra requirements

Changes to `bot/broker.py`, `bot/engine.py` (order paths), or the Dry-Run gate
must include:

- [ ] A test proving the Dry-Run gate still rejects: below-min notional,
      out-of-band price, fee-gap < 3×fee
- [ ] A test proving paper positions do not survive a paper→live switch
- [ ] A note on which exchange(s) you verified against (paper is not enough
      for order-mapping changes)

## Submitting

1. Fork → branch (`feat/…` or `fix/…`) → commit with a clear message.
2. Run the full suite; include the output in the MR.
3. Describe *what changed*, *why*, and *how you verified*.
4. For new exchange profiles: run the wizard probe against the live API and
   attach the report. AI-guessed endpoints are rejected — every endpoint must
   be verified against the exchange's official docs or SDK.

## Reporting bugs

Open an issue with: profile name, paper/live, console/log excerpt
(`Logs/<pid>/errors.jsonl`), and steps.

Prefer email? Bugs, issues and questions: **`mrdeveloper.outlast170@passinbox.com`**
For security issues see [SECURITY.md](SECURITY.md) — private email or
private GitHub reporting only, never a public issue.
