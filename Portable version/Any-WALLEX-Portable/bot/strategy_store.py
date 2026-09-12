"""Strategy store — persistence for legacy + external AI strategies.

All strategies the engine can use are represented as normalized JSON
files on disk under `data/strategies/`. The engine, backtest, and
frontend all read from this single source of truth.

Naming
------
- Legacy built-in strategy: `legacy.json`
- External AI strategies: `<strategy_id>.json`
- Each file contains one JSON artifact matching the AI strategy schema.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("strategy_store")


class StrategyStore:
    """Load/save/list strategy artifacts from disk."""

    def __init__(self, data_dir: str):
        self.base = Path(data_dir) / "strategies"
        self.base.mkdir(parents=True, exist_ok=True)
        self._legacy_path = self.base / "legacy.json"
        self._ensure_legacy()

    def _ensure_legacy(self) -> None:
        if not self._legacy_path.exists():
            self._allow_legacy_write = True   # only this path may write legacy.json
            try:
                from .strategy_schema import new_artifact
                self.save(
                    new_artifact(
                        vibe="Built-in strategy shipped with the app.",
                        provider="builtin",
                        model="none",
                    )
                    | {
                        "strategy_id": "legacy",
                        "name": "Legacy Strategy",
                        "source": "legacy",
                        "timeframe": "60",
                        "risk": {
                            "max_positions": 4,
                            "risk_per_trade_pct": 1.0,
                            "stop_atr_mult": 1.5,
                            "target_atr_mult": 3.0,
                            "dollar_tp": 0.0,
                            "dollar_stop": 0.0,
                            "grid_mode": "none",
                            "grid_step_pct": 1.0,
                            "grid_max_steps": 5,
                        },
                    }
                )
            finally:
                self._allow_legacy_write = False

    def _path_for(self, strategy_id: str) -> Path:
        safe = strategy_id.replace("/", "_").replace("\\", "_")
        return self.base / f"{safe}.json"

    def save(self, artifact: Dict[str, Any]) -> None:
        sid = str(artifact.get("strategy_id", "unknown"))
        # FIX(#12): legacy is the locked fallback — an AI-returned or
        # client-supplied strategy_id of 'legacy' must never overwrite
        # legacy.json. Only _ensure_legacy may create it.
        if sid == "legacy" and not getattr(self, "_allow_legacy_write", False):
            raise ValueError("strategy_id 'legacy' is reserved and cannot be overwritten")
        path = self._path_for(sid)
        artifact.setdefault("created_ts", int(time.time()))
        artifact["updated_ts"] = int(time.time())
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        log.info("strategy saved: %s", path)

    def load(self, strategy_id: str) -> Optional[Dict[str, Any]]:
        path = self._path_for(strategy_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("strategy load failed %s: %s", path, exc)
            return None

    def delete(self, strategy_id: str) -> bool:
        path = self._path_for(strategy_id)
        if path.exists():
            path.unlink()
            return True
        return False

    def list_all(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for path in self.base.glob("*.json"):
            if path.name == "legacy.json":
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                out.append(data)
            except Exception as exc:
                log.warning("strategy list failed %s: %s", path, exc)
        out.sort(key=lambda x: x.get("updated_ts", 0), reverse=True)
        return out

    def list_ids(self) -> List[str]:
        return [s.get("strategy_id") for s in self.list_all()]

    def legacy(self) -> Dict[str, Any]:
        data = self.load("legacy")
        if data is None:
            # regenerate if missing
            self._ensure_legacy()
            data = self.load("legacy")
        return data or {}
