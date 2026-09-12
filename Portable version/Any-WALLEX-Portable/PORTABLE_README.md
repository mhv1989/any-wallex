# Any WALLEX — Portable Edition

A ready-to-run, **clean-data** distribution of the Any WALLEX multi-exchange
trading platform. Exchange connection profiles (35 exchanges) are pre-built so
you get the full experience immediately — but there are **no user accounts, no
API keys, no trade history, no cached personal data**. Everything user-specific
is created fresh on this machine the first time you run it.

> ⚠️ **Risk disclosure.** This software places real orders only after you
> explicitly enable live mode. It does not guarantee profit. Crypto trading is
> high-risk; you can lose your entire capital. Default mode is **Paper Trading**.

---

## What's inside

```
Any-WALLEX-Portable/
├── launcher.py            interactive boot: pick a profile, it opens the dashboard
├── run.bat                double-click entry point (Windows)
├── stop.bat               stops running profiles (warns about active workflows)
├── stop_all.py            cross-platform stopper
├── config.yaml            every strategy threshold (documented in Persian)
├── requirements.txt       pinned Python dependencies
├── LICENSE                Apache License 2.0
├── NOTICE                 attribution + trademark + risk disclaimer
├── README.md              this file (full platform docs in the repo README)
├── runtime/python/        bundled Python 3.11 + all dependencies (zero install)
├── bot/                   the whole backend (FastAPI, engine, brokers, grids…)
├── web/                   the single-file Persian/English dashboard
├── scripts/               probe & batch tools
├── tests/                 133 tests — verify your install: python -m pytest tests/ -q
└── data/
    ├── exchange_catalog.json          the 36-exchange catalog (public info)
    ├── profiles/<exchange>/profile.json   pre-verified endpoint configs
    ├── profiles/registry.json         35 profiles, ports 8787–8821
    ├── profiles/exchange_knowledge.json   per-exchange API knowledge + history
    └── strategies/                    the built-in Legacy strategy + an example
```

**Deliberately NOT included** (your clean start):
`*.db` (trade history), `secrets.enc` / `salt.bin` / `.key_password` (previous
owner's encrypted API keys), `markets_cache.json`, all logs, per-user AI
provider configs. The first boot generates a brand-new encrypted vault with a
key password **you** choose.

---

## Quick start (Windows) — ZERO INSTALL

**Python is bundled.** The `runtime\python\` folder ships a complete
Python 3.11 runtime with every dependency pre-installed. You do **not**
need to install Python or pip.

1. Extract the zip anywhere (e.g. `D:\Any-WALLEX-Portable`)
2. Double-click **`run.bat`**
3. The onboarding wizard appears: keep the default **Wallex paper profile**
   or pick any of the 35 pre-configured exchanges.
4. The dashboard opens at `http://127.0.0.1:8787/` — you are in **Paper
   Trading**, safe to explore.

Requirements: Windows 10/11 x64 only (the bundled runtime is Windows x64).

### Linux / macOS

The bundled runtime is Windows-only. On Linux/macOS use your system Python:

```bash
cd Any-WALLEX-Portable
python3.11 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python launcher.py
```

## Connecting your exchange

Each shipped profile already contains **live-verified endpoint configuration**
(base URLs, candle formats, auth scheme). To trade on an exchange you only add
**your own** API key:

1. Dashboard → ⚙️ Settings → paste your API key (and secret if the exchange
   requires one). It is encrypted with Fernet (AES-128-CBC + HMAC) using a key
   derived from `WALLEX_KEY_PASSWORD` before it ever touches disk.
2. The profile's 7-point probe verifies reachability. Some exchanges
   geo-block certain regions (documented per profile in the knowledge
   library) — the app tells you honestly instead of failing silently.
3. Live trading additionally requires the explicit live-enable flow; the
   `WALLEX_LIVE_ALLOWED=no` environment variable is a master kill-switch.

## First-run data ownership

Everything this app generates stays inside this folder:

| Path | Contents |
|---|---|
| `data/profiles/<exchange>/bot.db` | your trades, equity, events (SQLite, WAL) |
| `data/profiles/<exchange>/secrets.enc` | your encrypted API keys |
| `Logs/<pid>/` | 13 categories of JSONL logs, 10 MB rotated |

Delete the folder = delete your data. Nothing is sent to any server except the
exchange APIs you configure (and your chosen LLM provider, if you opt in).

## Profiles included

Wallex (default, native adapter) · Nobitex · MEXC · KuCoin · Bybit · Kraken ·
Binance · OKX · Bitunix · CoinEx · DigiFinex · Hyperliquid · EdgeX · Apex Omni ·
AsterDEX · dYdX · BitMEX · Exir · Bitpin · Ramzinex · BloFin · KCEX · BYDFi ·
WEEX · Lighter · Ethereal · Godex · Quickex · 0x Swap · Houdini Swap ·
Eterna MCP · Southxchange · VDEX · OK Exchange · OKX Demo

Some are marked limited in the knowledge library (geo-blocked in some regions
or missing public candle endpoints) — the app records these limitations
honestly rather than pretending they work.

## Support & contact

- **Bugs & issues:** email **`mrdeveloper.outlast170@passinbox.com`** or open a GitHub issue
- **Security reports:** **`mrdeveloper.outlast170@passinbox.com`** (private — see `SECURITY.md`)
- Please include: your OS, Python version, the exchange profile, and
  the relevant lines from `Logs/<pid>/errors.jsonl` if available.

## License

Apache License 2.0 — see `LICENSE` and `NOTICE`. Exchange names are trademarks
of their owners; this project is not affiliated with any exchange.

## Verify before you trust

```bash
python -m pytest tests/ -q        # 133 tests must pass
python launcher.py --list         # show all profiles + ports
```
