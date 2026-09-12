# Release Guide — Publishing the Portable Edition

This is the maintainer's runbook for shipping a release to GitHub.
Complete each step in order; do not skip the integrity steps.

## 0. Pre-flight

```bash
python -m pytest tests/ -q          # full suite green
```

- Confirm `bot/server.py` version constant matches the release tag
  (`app_version = "1.0.0"`).
- Confirm the repo has no user data staged: `git status` must show no `data/`,
  `Logs/`, `*.db`, `secrets.enc` files.
- Replace `YOUR-ORG/any-wallex` in `bot/server.py` (`source:` field) and this
  file with the real GitHub repository URL.

## 1. Refresh the portable tree

The portable folder is a *build artifact* — regenerate it from the repo after
any code change, never edit it directly:

- Copy: `bot/ web/ scripts/ tests/ launcher.py stop_all.py run.bat stop.bat
  config.yaml requirements.txt README.md .env.example`
- Data: `data/exchange_catalog.json`,
  `data/profiles/<id>/profile.json` for every clean profile,
  `data/profiles/registry.json` (reset `active`/`last_launched` to `wallex`),
  `data/profiles/exchange_knowledge.json`,
  `data/strategies/legacy.json` + example strategy
- Exclude everything else: `*.db *.db-* *.log secrets.enc salt.bin
  .key_password markets_cache.json ai_providers.json cmc_cache.json history/
  grids/ Logs/`

Final audit (must return nothing):

```
Portable version/Any-WALLEX-Portable> dir /s /b *.db *.enc salt.bin .key_password *.log
```

## 2. Integrity manifest

From inside `Any-WALLEX-Portable/`:

**Windows (PowerShell):**
```powershell
Get-ChildItem -Recurse -File |
  Where-Object { $_.Name -ne 'SHA256SUMS.txt' } |
  ForEach-Object {
    "{0}  {1}" -f (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower(),
    [IO.Path]::GetRelativePath((Get-Location).Path, $_.FullName).Replace('\','/')
  } | Sort-Object | Out-File -Encoding ascii SHA256SUMS.txt
```

**Linux / macOS:**
```bash
find . -type f ! -name SHA256SUMS.txt -print0 |
  sed 's|^\./||' | xargs -0 sha256sum | sort -k2 > SHA256SUMS.txt
```

## 3. Package

```bash
cd "Portable version"
zip -r Any-WALLEX-Portable-v1.0.0.zip Any-WALLEX-Portable
sha256sum Any-WALLEX-Portable-v1.0.0.zip > Any-WALLEX-Portable-v1.0.0.zip.sha256
```

Windows equivalent:
```powershell
Compress-Archive -Path Any-WALLEX-Portable -DestinationPath Any-WALLEX-Portable-v1.0.0.zip
Get-FileHash Any-WALLEX-Portable-v1.0.0.zip -Algorithm SHA256 |
  ForEach-Object { $_.Hash.ToLower() } | Out-File Any-WALLEX-Portable-v1.0.0.zip.sha256
```

## 4. GitHub release

1. Tag: `git tag -a v1.0.0-portable -m "First portable release"` → `git push --tags`
2. GitHub → Releases → **Draft a new release** → pick the tag.
3. Title: `Any WALLEX Portable v1.0.0`
4. Body: paste `PORTABLE_README.md` highlights + the zip's SHA-256 (from step 3)
   + the minimum Python version + the contact line:
   `Bugs & security: mrdeveloper.outlast170@passinbox.com`
5. Attach: `Any-WALLEX-Portable-v1.0.0.zip` **and**
   `Any-WALLEX-Portable-v1.0.0.zip.sha256`.
6. Publish. Then verify: download the zip fresh, extract to a clean folder, run
   `sha256sum -c SHA256SUMS.txt` inside it, and boot once with a throwaway
   Python env.

## 5. Repository settings (anti-theft & hygiene)

- **License file** detected automatically (Apache-2.0 badge).
- **Enable private vulnerability reporting** (Settings → Code security).
- **Branch protection** on `master`: require PR review for direct pushes if
  collaborators join; require the test suite via Actions (add a workflow that
  runs `python -m pytest tests/ -q` on every PR).
- **Do NOT enable "sponsors" merchants for trading advice**; keep the risk
  disclaimer prominent in the README.

## 6. Anti-theft posture (what the license does and does not do)

Apache-2.0 *allows* forks — that is the point. What it gives you:

- **Attribution is mandatory.** Forks must keep `LICENSE` and `NOTICE` and must
  state changes. The `/api/status` provenance block (`app`, `version`,
  `license`, `source`) and the dashboard footer are embedded evidence: a
  rebranded copy that strips them is a license violation you can act on
  (GitHub DMCA / repository report).
- **Patent grant is mutual** — a malicious fork cannot sue you for patents it
  uses from this code, and if they litigate, their license terminates (§3).
- **Malware reuploads**: Apache-2.0 §7 means the software is provided as-is; a
  trojanized reupload is (a) a license violation (modified files must carry
  notices, attribution removed = violation), and (b) reportable via GitHub
  abuse. The `SHA256SUMS.txt` + zip hash in release notes give users a
  definitive way to detect it.

What Apache-2.0 does **not** do: prevent closed-source forks that comply with
the license. If you ever want copyleft protection, that is a licensing decision
to make *before* more third-party contributions arrive (switching later requires
agreement of all contributors).

## 7. Post-release

- Watch the first issues for install problems (Python version is the usual one).
- Update `exchange_knowledge.json` when an exchange changes endpoints; ship it
  in the next release so users inherit fixes.
- Keep `SHA256SUMS.txt` regeneration in step 2 of every future release —
  no exceptions.
