"""One-command run: builds the demo database if missing, then starts the server on the port in PORT (default 8080)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import uvicorn

from app.config import get_settings

ROOT = Path(__file__).resolve().parent

if __name__ == "__main__":
    settings = get_settings()
    if not Path(settings.database_path).exists():
        subprocess.run([sys.executable, str(ROOT / "scripts" / "generate_data.py"), "--reset"], check=True, cwd=ROOT)
    uvicorn.run("app.main:create_app", factory=True, host="0.0.0.0", port=settings.port, app_dir=str(ROOT))
