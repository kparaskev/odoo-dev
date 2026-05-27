import ast
import json
from datetime import datetime, timezone
from pathlib import Path

from .config import OdooConfig
from .model_parser import OdooModelIndexVisitor, OdooModelMethodVisitor, OdooModelVisitor
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
_INDEX_FILE = "model_index.json"


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
        self._model_index: dict[str, dict[str, list[str]]] | None = None

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

    def find_model_implementations(
        self,
        model_name: str,
        mro_source_model: str | None = None,
    ) -> list[dict]:
        """Search installed+local modules for classes that define or extend *model_name*.

        When *mro_source_model* is given, get_true_mro is called on that model first
        to restrict the search to only modules in the inheritance chain. Falls back to
        scanning all installed+local modules if the server method is unavailable.

        Returns a list of dicts, one per matching class:
          module     – addon name
          source     – 'odoo' | 'enterprise' | 'custom'
          file_path  – absolute path to the Python file
          class_name – Python class name
          relation   – 'define' (_name set explicitly) or 'extend' (only _inherit set)
        """
        candidates = self.installed_and_local()

        if mro_source_model is not None:
            try:
                mro_list = self._rpc.get_true_mro(mro_source_model, model_name)
                local_map = {m["name"]: m for m in candidates}
                candidates = [local_map[name] for name in mro_list if name in local_map]
                print(f"MRO filter: {len(candidates)} module(s) in chain for '{model_name}'")
            except RuntimeError as exc:
                print(f"[warn] get_true_mro unavailable, falling back to full scan: {exc}")

        print(f"Scanning {len(candidates)} module(s) for '{model_name}'…")

        results: list[dict] = []
        for module in candidates:
            for m in self.parse_module_models(module["name"]):
                if m["model_name"] != model_name:
                    continue
                relation = "define" if m["has_explicit_name"] else "extend"
                results.append({
                    "module": module["name"],
                    "source": module["source"],
                    "file_path": m["file_path"],
                    "class_name": m["class_name"],
                    "relation": relation,
                })

        return results

    def parse_module_models(self, module_name: str) -> list[dict]:
        """Parse all Python files in *module_name* and return discovered Odoo models.

        Looks up the module path from the local-modules cache, walks every .py
        file, runs OdooModelVisitor on each, then prints a human-readable summary.
        """
        module = self.get_local_module(module_name)
        if module is None:
            print(f"Module '{module_name}' not found in local modules cache.")
            return []

        module_path = Path(module["path"])
        visitor = OdooModelVisitor()

        py_files = sorted(module_path.rglob("*.py"))
        for py_file in py_files:
            try:
                source = py_file.read_text(encoding="utf-8")
                tree = ast.parse(source)
                visitor.current_file = str(py_file)
                visitor.visit(tree)
            except SyntaxError as exc:
                print(f"  [warn] syntax error in {py_file}: {exc}")
            except Exception as exc:
                print(f"  [warn] could not parse {py_file}: {exc}")
        
        return visitor.models

    def build_model_index(self, force: bool = False) -> dict[str, dict[str, list[str]]]:
        """Scan all local modules and build a model→module→files index.

        The index is persisted to *data_path/model_index.json* and cached
        in-memory so subsequent calls are instant.  Pass *force=True* to
        rebuild even when the file already exists.

        Structure::

            {
              "sale.order": {
                "sale":        ["/path/sale/models/sale_order.py"],
                "sale_stock":  ["/path/sale_stock/models/sale_order.py"],
              },
              ...
            }
        """
        index_file = self._data_path / _INDEX_FILE
        if not force and index_file.exists():
            self._model_index = _load_json(index_file)
            print(f"Loaded model index from cache ({index_file})")
            return self._model_index

        print(f"Building model index across {len(self._local_modules)} module(s)…")
        index: dict[str, dict[str, list[str]]] = {}

        for module in self._local_modules:
            module_name = module["name"]
            module_path = Path(module["path"])
            visitor = OdooModelIndexVisitor()

            for py_file in sorted(module_path.rglob("*.py")):
                try:
                    source = py_file.read_text(encoding="utf-8")
                    tree = ast.parse(source)
                    visitor.current_file = str(py_file)
                    visitor.visit(tree)
                except SyntaxError:
                    pass
                except Exception:
                    pass

            for model_name, file_path in visitor.entries:
                index.setdefault(model_name, {}).setdefault(module_name, []).append(file_path)

        _save_json(index_file, index)
        self._model_index = index
        print(f"  → {len(index)} model(s) indexed, saved to {index_file}")
        return index

    def load_model_index(self) -> dict[str, dict[str, list[str]]]:
        """Return the in-memory index, loading from disk if needed.

        Raises FileNotFoundError if the index has never been built.
        """
        if self._model_index is not None:
            return self._model_index
        index_file = self._data_path / _INDEX_FILE
        if not index_file.exists():
            raise FileNotFoundError(
                f"Model index not found at {index_file}. Call build_model_index() first."
            )
        self._model_index = _load_json(index_file)
        return self._model_index

    def lookup_model(self, model_name: str) -> dict[str, list[str]]:
        """Return ``{module: [file_paths]}`` for *model_name* from the index.

        Returns an empty dict when the model is not found.
        """
        return self.load_model_index().get(model_name, {})

    def build_model_methods(
        self, model_name: str, force: bool = False
    ) -> dict[str, list[dict]]:
        """Build and persist a method index for *model_name*.

        Uses the model index to find only the files that define or extend
        *model_name*, parses them with the lightweight OdooModelMethodVisitor,
        and saves the result to ``<data_path>/<model_name>.methods.json``.

        Returns a dict keyed by method name; each value is a list of
        occurrences (one per module/file that defines that method)::

            {
              "action_confirm": [
                {
                  "module":     "sale",
                  "file":       "/abs/path/sale_order.py",
                  "class_name": "SaleOrder",
                  "args":       ["self"],
                  "decorators": ["api.multi"],
                }
              ],
              ...
            }

        Pass *force=True* to rebuild even when the cache file already exists.
        """
        methods_file = self._data_path / f"{model_name}.methods.json"
        if not force and methods_file.exists():
            data = _load_json(methods_file)
            print(f"Loaded method index for '{model_name}' from cache ({methods_file})")
            return data

        locations = self.lookup_model(model_name)
        if not locations:
            print(f"Model '{model_name}' not found in index. Run build_model_index() first.")
            return {}

        total_files = sum(len(files) for files in locations.values())
        print(f"Building method index for '{model_name}' ({total_files} file(s) across {len(locations)} module(s))…")

        methods: dict[str, list[dict]] = {}
        for module_name, file_paths in locations.items():
            for file_path in file_paths:
                visitor = OdooModelMethodVisitor(model_name)
                try:
                    source = Path(file_path).read_text(encoding="utf-8")
                    tree = ast.parse(source)
                    visitor.current_file = file_path
                    visitor.visit(tree)
                except Exception:
                    continue
                for m in visitor.methods:
                    methods.setdefault(m["name"], []).append({
                        "module": module_name,
                        "file": file_path,
                        "class_name": m["class_name"],
                        "args": m["args"],
                        "decorators": m["decorators"],
                    })

        _save_json(methods_file, methods)
        print(f"  → {len(methods)} method(s) indexed, saved to {methods_file}")
        return methods

    def load_model_methods(self, model_name: str) -> dict[str, list[dict]]:
        """Load the method index for *model_name* from disk.

        Raises FileNotFoundError if build_model_methods() has not been called yet.
        """
        methods_file = self._data_path / f"{model_name}.methods.json"
        if not methods_file.exists():
            raise FileNotFoundError(
                f"Method index for '{model_name}' not found at {methods_file}."
                " Call build_model_methods() first."
            )
        return _load_json(methods_file)

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


def _print_model_implementations(model_name: str, results: list[dict]) -> None:
    bar = "─" * 60
    print(f"\n{bar}")
    print(f"Model : {model_name}  —  {len(results)} implementation(s) found")
    print(bar)
    if not results:
        print("  (none found among installed+local modules)")
    else:
        defines = [r for r in results if r["relation"] == "define"]
        extends = [r for r in results if r["relation"] == "extend"]
        for label, group in (("define", defines), ("extend", extends)):
            if not group:
                continue
            print(f"\n  {label.upper()} ({len(group)})")
            for r in group:
                print(f"    [{r['source']:>10}]  {r['module']:<40}  {r['class_name']}")
                print(f"               {r['file_path']}")
    print(f"\n{bar}\n")
