import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "Bingasys Core")
    database_path: Path = Path(os.getenv("DATABASE_PATH", "bingasys.db"))


settings = Settings()
