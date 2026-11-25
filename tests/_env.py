"""Settings for every API test suite, set before api.py and auth.py read them at import.

Imported first by each suite, so suites run together share one throwaway
user database (with different usernames) instead of each one setting its own
and the second one silently losing.
"""

import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
# Users, sessions, quotas and the audit log live in Postgres (Lampus Phase 1, Step 3).
# Each test run gets its own throwaway schema in the database named by TEST_DATABASE_URL
# (or DATABASE_URL), dropped again when the run ends, so runs never see each other's rows.
import atexit  # noqa: E402
import secrets  # noqa: E402

import psycopg  # noqa: E402
from psycopg.conninfo import make_conninfo  # noqa: E402

_BASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get(
    "DATABASE_URL", "postgresql://lampus:lampus@localhost:5432/lampus")
SCHEMA = f"test_{secrets.token_hex(4)}"
with psycopg.connect(_BASE_URL, autocommit=True) as _conn:
    _conn.execute(f"CREATE SCHEMA {SCHEMA}")
os.environ["DATABASE_URL"] = make_conninfo(_BASE_URL, options=f"-c search_path={SCHEMA}")


@atexit.register
def _drop_schema():
    with psycopg.connect(_BASE_URL, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


os.environ["QUOTA_TURNS_PER_DAY"] = "3"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
