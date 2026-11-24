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
os.environ["AUTH_DB"] = str(TMP / "auth.sqlite")
os.environ["QUOTA_TURNS_PER_DAY"] = "3"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
