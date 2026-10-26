"""Connections to the local demo database. Every demo login's password is its name (sandbox only)."""
from __future__ import annotations

import os

import psycopg


def connect(user: str, *, dbname: str | None = None, actor: str | None = None, **kwargs) -> psycopg.Connection:
    """Log in as `user`. A service account must pass `actor`: who or what its changes are for (ADR-002)."""
    password = "itdp_admin" if user == "itdp_admin" else user
    options = f"-c app.user_id={actor}" if actor else ""
    return psycopg.connect(
        host=os.environ.get("ITDP_HOST", "localhost"),
        port=int(os.environ.get("ITDP_PORT", "5433")),
        dbname=dbname or os.environ.get("ITDP_DB", "itdp"),
        user=user, password=password, options=options, **kwargs)
