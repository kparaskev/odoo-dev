import ast
import json
from datetime import datetime, timezone
from pathlib import Path

from .config import OdooConfig
from .rpc import OdooRPC

# Fields fetched from ir.module.module for installed remote modules
_REMOTE_MODULE_FIELDS = [
    "name",
    "display_name",
    "state",
    "latest_version",
    "installed_version",
    "summary",
    "author",
    "website",
    "category_id",
]

_REMOTE_FILE = "remote_modules.json"
_LOCAL_FILE = "local_modules.json"


class OdooAnalyzer:
    """
    Combines local source-code inspection with remote XML-RPC queries.

    remote_modules  – modules installed on the connected Odoo server
    local_modules   – modules found in odoo source / enterprise / custom paths

    Both lists are persisted to DATA_PATH as JSON and are only refreshed when
    `force=True` is passed or the cache file is absent.
    """

    def __init__(self, config: OdooConfig, rpc: OdooRPC, force: bool = False) -> None:
        self._config = config
        self._rpc = rpc
        self._data_path = config.data_path
        self._data_path.mkdir(parents=True, exist_ok=True)

        self._remote_modules: list[dict] = []
        self._local_modules: list[dict] = []

        self._load_or_build(force)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def remote_modules(self) -> list[dict]:
        return self._remote_modules

    @property
    def local_modules(self) -> list[dict]:
        return self._local_modules

    def get_local_module(self, name: str) -> dict | None:
        """Return the first local module matching *name*, or None."""
        return next((m for m in self._local_modules if m["name"] == name), None)

    def get_remote_module(self, name: str) -> dict | None:
        """Return the remote module record matching *name*, or None."""
        return next((m for m in self._remote_modules if m["name"] == name), None)

    def remote_module_names(self) -> set[str]:
        return {m["name"] for m in self._remote_modules}

    def local_module_names(self) -> set[str]:
        return {m["name"] for m in self._local_modules}

    def installed_and_local(self) -> list[dict]:
        """Local modules that are also installed on the remote server."""
        installed = self.remote_module_names()
        return [m for m in self._local_modules if m["name"] in installed]

    def installed_but_not_local(self) -> list[str]:
        """Names of remote-installed modules with no local source found."""
        local = self.local_module_names()
        return sorted(n for n in self.remote_module_names() if n not in local)

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Force a full rebuild and persist the results."""
        self._load_or_build(force=True)

    def _load_or_build(self, force: bool) -> None:
        remote_file = self._data_path / _REMOTE_FILE
        local_file = self._data_path / _LOCAL_FILE

        if force or not remote_file.exists():
            print("Fetching remote modules from server…")
            self._remote_modules = self._fetch_remote_modules()
            _save_json(remote_file, self._remote_modules)
            print(f"  → {len(self._remote_modules)} installed modules cached to {remote_file}")
        else:
            self._remote_modules = _load_json(remote_file)
            print(f"Loaded {len(self._remote_modules)} remote modules from cache ({remote_file})")

        if force or not local_file.exists():
            print("Scanning local source paths…")
            self._local_modules = self._scan_local_modules()
            _save_json(local_file, self._local_modules)
            print(f"  → {len(self._local_modules)} local modules cached to {local_file}")
        else:
            self._local_modules = _load_json(local_file)
            print(f"Loaded {len(self._local_modules)} local modules from cache ({local_file})")

    # ------------------------------------------------------------------
    # Remote data collection
    # ------------------------------------------------------------------

    def _fetch_remote_modules(self) -> list[dict]:
        return self._rpc.search_read(
            "ir.module.module",
            [["state", "=", "installed"]],
            fields=_REMOTE_MODULE_FIELDS,
        )

    # ------------------------------------------------------------------
    # Local data collection
    # ------------------------------------------------------------------

    def _scan_local_modules(self) -> list[dict]:
        """Walk each configured addon directory and collect manifest info."""
        sources: list[tuple[str, Path]] = []

        if self._config.odoo_source_path:
            # Community addons live in <odoo>/addons and <odoo>/odoo/addons
            for sub in ("addons", "odoo/addons"):
                p = self._config.odoo_source_path / sub
                if p.exists():
                    sources.append(("odoo", p))

        if self._config.enterprise_path:
            sources.append(("enterprise", self._config.enterprise_path))

        for custom_path in self._config.custom_addon_paths:
            sources.append(("custom", custom_path))

        modules: list[dict] = []
        seen: set[str] = set()

        for source_label, base_path in sources:
            if not base_path.exists():
                print(f"  [warn] path not found, skipping: {base_path}")
                continue
            for manifest_path in sorted(base_path.glob("*/__manifest__.py")):
                module_dir = manifest_path.parent
                name = module_dir.name
                if name in seen:
                    continue
                seen.add(name)
                try:
                    manifest = _parse_manifest(manifest_path)
                except Exception as exc:
                    print(f"  [warn] could not parse {manifest_path}: {exc}")
                    manifest = {}
                modules.append({
                    "name": name,
                    "path": str(module_dir),
                    "source": source_label,
                    "manifest": manifest,
                })

        return modules


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _parse_manifest(path: Path) -> dict:
    return ast.literal_eval(path.read_text(encoding="utf-8"))


def _save_json(path: Path, data: list) -> None:
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)


def _load_json(path: Path) -> list:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)["data"]
