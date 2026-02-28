"""
Persistent phone registry backed by a JSON file.

On first load, seeds from the PHONE_REGISTRY / HOSPITAL_REGISTRY env vars.
All mutations are immediately written to disk so the data survives restarts.
"""

from __future__ import annotations
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

from backend.config import settings

logger = logging.getLogger(__name__)

_REGISTRY_PATH = Path(__file__).parent / "registry.json"


def _normalize_key(name: str) -> str:
    """Convert a display name to a normalized registry key."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


class PhoneRegistry:
    """In-memory phone registry that syncs to a JSON file."""

    def __init__(self):
        self._data: dict[str, dict] = {}
        self._load()

    # --------------------------------------------------
    # Persistence
    # --------------------------------------------------

    def _load(self):
        """Load registry from disk, or seed from .env if file doesn't exist."""
        if _REGISTRY_PATH.exists():
            try:
                with open(_REGISTRY_PATH, "r") as f:
                    self._data = json.load(f)
                logger.info(
                    f"[Registry] Loaded {len(self._data)} contacts from {_REGISTRY_PATH}"
                )
                return
            except Exception as e:
                logger.warning(f"[Registry] Failed to read {_REGISTRY_PATH}: {e}")

        # Seed from .env
        env_registry = settings.get_phone_numbers()
        for key, phone in env_registry.items():
            display_name = key.replace("_", " ").title()
            self._data[key] = {
                "name": display_name,
                "phone": phone,
                "category": "hospital",
            }
        self._save()
        logger.info(
            f"[Registry] Seeded {len(self._data)} contacts from .env"
        )

    def _save(self):
        """Write current registry to disk."""
        try:
            with open(_REGISTRY_PATH, "w") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[Registry] Failed to write {_REGISTRY_PATH}: {e}")

    # --------------------------------------------------
    # CRUD
    # --------------------------------------------------

    def list_all(self) -> list[dict]:
        """Return all contacts as a list of dicts (with key included)."""
        return [
            {"key": k, **v}
            for k, v in sorted(self._data.items(), key=lambda x: x[1].get("name", ""))
        ]

    def get(self, key: str) -> Optional[dict]:
        """Get a single contact by key."""
        entry = self._data.get(key)
        if entry:
            return {"key": key, **entry}
        return None

    def add(self, name: str, phone: str, category: str = "other") -> dict:
        """Add or update a contact. Returns the saved entry."""
        key = _normalize_key(name)
        self._data[key] = {
            "name": name.strip(),
            "phone": phone.strip(),
            "category": category.strip().lower(),
        }
        self._save()
        logger.info(f"[Registry] Added/updated: {key} -> {phone}")
        return {"key": key, **self._data[key]}

    def delete(self, key: str) -> bool:
        """Delete a contact by key. Returns True if it existed."""
        if key in self._data:
            del self._data[key]
            self._save()
            logger.info(f"[Registry] Deleted: {key}")
            return True
        return False

    def get_phone_numbers(self) -> dict[str, str]:
        """Return a flat {key: phone} dict for InputAgent lookups."""
        return {k: v["phone"] for k, v in self._data.items()}


# Singleton instance
phone_registry = PhoneRegistry()
