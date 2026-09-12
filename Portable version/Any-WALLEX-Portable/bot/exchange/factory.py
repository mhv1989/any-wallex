"""Adapter factory — build the right exchange adapter for a profile.

Phase 1: 'wallex' → hand-coded WallexAdapter (zero regression).
Phase 2: any profile with data/profiles/<id>/profile.json → GenericRESTAdapter.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .base import ExchangeAdapter
from .profile import load_profile


def build_adapter(profile_id: str, api_key: str = "", min_gap_sec: float = 12.0,
                  max_retries: int = 3, retry_pause_sec: float = 30.0,
                  redact_fn=None, cfg: Optional[dict] = None,
                  api_secret: str = "", passphrase: str = "") -> ExchangeAdapter:
    if profile_id == "wallex":
        from .wallex import WallexAdapter
        return WallexAdapter(
            api_key,
            min_gap_sec=min_gap_sec,
            max_retries=max_retries,
            retry_pause_sec=retry_pause_sec,
            redact_fn=redact_fn,
        )
    # Phase 2: profile.json → GenericRESTAdapter
    root = Path(__file__).resolve().parent.parent.parent / "data" / "profiles"
    profile = load_profile(profile_id, root)
    if profile is None:
        raise NotImplementedError(
            f"no adapter implementation for profile '{profile_id}' (no profile.json; "
            f"run the setup wizard or create data/profiles/{profile_id}/profile.json)")
    from .generic import GenericRESTAdapter
    return GenericRESTAdapter(profile, api_key=api_key, api_secret=api_secret,
                              passphrase=passphrase, redact_fn=redact_fn)
