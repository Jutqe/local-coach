"""Default paths. No imports from the package: the language module needs them before anything else is loaded."""
import os
from pathlib import Path

DEFAULT_VAULT_CANDIDATES: tuple[Path, ...] = ()
# Database, memory and imports live outside the vault: notes are only read.
DEFAULT_WORKSPACE = Path(os.environ.get("LOCAL_COACH_WORKSPACE", "~/.local-coach")).expanduser()
