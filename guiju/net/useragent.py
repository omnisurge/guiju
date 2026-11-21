"""Shared user-agent for body/probe requests."""

from __future__ import annotations

from .._version import __version__

BODY_USER_AGENT = f"guiju/{__version__} (compliance-aware; official code fetcher)"
