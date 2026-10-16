import os
from pathlib import Path

ROOT = Path(os.environ.get("VIX_HOME", Path(__file__).resolve().parent.parent))
DATA_DIR = ROOT / "data"
REPORT_DIR = DATA_DIR / "reports"
DB_PATH = DATA_DIR / "vix.db"
STATIC_DIR = ROOT / "static"

WEB_PORT = int(os.environ.get("VIX_WEB_PORT", "47530"))
MCP_PORT = int(os.environ.get("VIX_MCP_PORT", "47531"))
SEED = int(os.environ.get("VIX_SEED", "42"))

for d in (DATA_DIR, REPORT_DIR):
    d.mkdir(parents=True, exist_ok=True)
