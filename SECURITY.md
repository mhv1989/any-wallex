# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| latest `master` | ✅ |
| tagged releases (e.g. `v1.0.0-portable`) | ✅ |
| older tags | ❌ upgrade |

## Reporting a vulnerability

**Do not open a public issue for security problems.**

**Preferred:** email **`mrdeveloper.outlast170@passinbox.com`** (encryption welcome — ask for a PGP
key and one will be published). **Also available:** GitHub's Private
vulnerability reporting (Repository → Security → Report a vulnerability).

Include:

1. Affected component (`bot/server.py`, `bot/broker.py`, `web/index.html`, …)
2. Reproduction steps and impact assessment
3. Whether it affects **paper mode**, **live mode**, or both

You will get an acknowledgement within **72 hours** and a fix timeline within
**7 days** for confirmed issues affecting live-trading safety.

Contact for all security matters: **`mrdeveloper.outlast170@passinbox.com`** Credit is given
in the release notes unless you prefer anonymity.

## Scope: what counts as a security issue here

This is a trading platform connected to real exchange accounts. Highest-severity
classes, in order:

1. **Unauthorized order placement** — anything that lets a request the trader
   did not author reach `LiveBroker` (CSRF, DNS-rebinding, auth bypass).
2. **Secret extraction** — anything that reads `secrets.enc` / the encrypted
   vault, or leaks API keys into logs, the UI, or error messages.
3. **Strategy poisoning** — injecting instructions through LLM responses
   (wizard research, AI strategy generation) that alter risk parameters or
   trigger orders.
4. **Data integrity** — tampering with `grids.json`/`bot.db` to hide losses or
   forge equity history.

## Built-in defenses (what the code already does)

| Threat | Defense | Where |
|---|---|---|
| Drive-by CSRF from malicious websites | Same-origin guard: non-localhost `Host` and cross-origin `Origin` → 403 | `bot/server.py` middleware |
| Stolen API keys at rest | Fernet (AES-128-CBC + HMAC) encryption, PBKDF2 key from `WALLEX_KEY_PASSWORD`; keys never logged | `bot/crypto_store.py` |
| Accidental live orders | Dry-Run gate on every live order (min notional, price band, fee-gap ≥ 3×fee) + master kill-switch `WALLEX_LIVE_ALLOWED=no` | `bot/broker.py`, `bot/server.py` |
| XSS via exchange data or LLM output | `escHtml()` on all injected strings; no raw innerHTML of API fields | `web/index.html` |
| Malicious/lying LLM responses | Strategy artifacts validated against a schema before activation; wizard probe re-verifies every endpoint against the live exchange | `bot/wizard.py`, `bot/strategy_schema.py` |
| Prompt injection via web research | Research output is treated as data, never as instructions; every setting is probed against the real API before activation | `bot/wizard.py` |
| Ghost positions after paper→live switch | `engine.positions` cleared + snapshotted on broker swap | `bot/server.py` |
| Orders on stale/unknown prices | 60 s dead-zone + staleness headers; no price = no trade | `bot/engine.py`, adapters |
| Tampered state files | Atomic writes (tmp+rename), corrupt files preserved as `.corrupt`, SQLite WAL | storage layer |

## What is intentionally out of scope

- Your machine's security (malware you already have, compromised Python
  install — verify the Python installer checksum).
- Exchange-side breaches (your account security on the exchange).
- Losses from trading itself. The software executes strategies; it does not
  make them profitable. **No profit guarantee.**

## Verifying official downloads

Every release ships a `SHA256SUMS.txt` covering every file. Verify before running:

**Windows (PowerShell):**
```powershell
# after extracting the release zip
Get-ChildItem -Recurse -File | ForEach-Object {
  (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower() + "  " +
  [IO.Path]::GetRelativePath((Get-Location).Path, $_.FullName).Replace('\','/')
} | Out-File myhashes.txt
Compare-Object (Get-Content SHA256SUMS.txt) (Get-Content myhashes.txt)
# no output = every file matches
```

**Linux / macOS:**
```bash
sha256sum -c SHA256SUMS.txt
```

If any line mismatches: **do not run the app**. The file was modified,
corrupted in transit, or is not from us. Report it to **`mrdeveloper.outlast170@passinbox.com`** with the mismatching lines.

Only download releases from this repository's **Releases** page. We never
distribute binaries through third-party mirrors, Telegram channels, or
"cracked/premium" reuploads — those are the primary malware vector for
trading tools.
