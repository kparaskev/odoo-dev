import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


@dataclass
class OdooConfig:
    url: str
    db: str
    username: str
    password: str
    odoo_source_path: Path | None
    enterprise_path: Path | None
    data_path: Path
    custom_addon_paths: list[Path] = field(default_factory=list)


def load_config() -> OdooConfig:
    url = os.environ["ODOO_URL"].rstrip("/")
    db = os.environ["ODOO_DB"]
    username = os.environ["ODOO_USERNAME"]
    password = os.environ["ODOO_PASSWORD"]

    odoo_source = os.getenv("ODOO_SOURCE_PATH")
    enterprise = os.getenv("ODOO_ENTERPRISE_PATH")
    custom_raw = os.getenv("ODOO_CUSTOM_ADDON_PATHS", "")
    data_path = os.getenv("DATA_PATH", "data")

    odoo_source_path = Path(odoo_source) if odoo_source else None
    enterprise_path = Path(enterprise) if enterprise else None
    custom_addon_paths = [
        Path(p.strip()) for p in custom_raw.split(",") if p.strip()
    ]

    return OdooConfig(
        url=url,
        db=db,
        username=username,
        password=password,
        odoo_source_path=odoo_source_path,
        enterprise_path=enterprise_path,
        data_path=Path(data_path),
        custom_addon_paths=custom_addon_paths,
    )


def inject_odoo_paths(config: OdooConfig) -> None:
    """Add local Odoo source paths to sys.path so local modules are importable."""
    paths_to_add = []

    if config.odoo_source_path and config.odoo_source_path.exists():
        paths_to_add.append(str(config.odoo_source_path))
    if config.enterprise_path and config.enterprise_path.exists():
        paths_to_add.append(str(config.enterprise_path))
    for p in config.custom_addon_paths:
        if p.exists():
            paths_to_add.append(str(p))

    for p in reversed(paths_to_add):
        if p not in sys.path:
            sys.path.insert(0, p)
