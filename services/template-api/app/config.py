from __future__ import annotations

import os
from pathlib import Path


PLATFORM_ROOT = Path(__file__).resolve().parents[3]
DATA_ROOT = PLATFORM_ROOT / "data"
ARTIFACT_ROOT = PLATFORM_ROOT / "artifacts"
ATTACHMENT_ROOT = DATA_ROOT / "attachments"
LOCAL_DATABASE = DATA_ROOT / "platform.db"
MATERIAL_DATABASE = Path(
    os.environ.get("RUIWARE_MATERIAL_DB", PLATFORM_ROOT / "ruiware.db")
).resolve()

DATA_ROOT.mkdir(parents=True, exist_ok=True)
ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
ATTACHMENT_ROOT.mkdir(parents=True, exist_ok=True)
