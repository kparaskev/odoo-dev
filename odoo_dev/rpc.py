import xmlrpc.client
from functools import cached_property
from .config import OdooConfig


class OdooRPC:
    """Thin wrapper around the Odoo XML-RPC external API."""

    def __init__(self, config: OdooConfig) -> None:
        self._config = config
        self._uid: int | None = None

    @cached_property
    def _common(self) -> xmlrpc.client.ServerProxy:
        return xmlrpc.client.ServerProxy(f"{self._config.url}/xmlrpc/2/common")

    @cached_property
    def _object(self) -> xmlrpc.client.ServerProxy:
        return xmlrpc.client.ServerProxy(f"{self._config.url}/xmlrpc/2/object")

    def authenticate(self) -> int:
        """Authenticate and return the user id (uid)."""
        self._uid = self._common.authenticate(
            self._config.db,
            self._config.username,
            self._config.password,
            {},
        )
        if not self._uid:
            raise PermissionError(
                f"Authentication failed for user '{self._config.username}' "
                f"on database '{self._config.db}'"
            )
        return self._uid

    @property
    def uid(self) -> int:
        if self._uid is None:
            self.authenticate()
        return self._uid

    def server_version(self) -> str:
        return self._common.version()["server_version"]

    def execute(self, model: str, method: str, *args, **kwargs) -> object:
        """Call models.execute_kw on the remote server."""
        return self._object.execute_kw(
            self._config.db,
            self.uid,
            self._config.password,
            model,
            method,
            list(args),
            kwargs,
        )

    def search_read(
        self,
        model: str,
        domain: list | None = None,
        fields: list[str] | None = None,
        limit: int = 0,
        offset: int = 0,
        order: str = "",
    ) -> list[dict]:
        kwargs: dict = {}
        if fields:
            kwargs["fields"] = fields
        if limit:
            kwargs["limit"] = limit
        if offset:
            kwargs["offset"] = offset
        if order:
            kwargs["order"] = order
        return self.execute(model, "search_read", domain or [], **kwargs)

    def search(self, model: str, domain: list | None = None, **kwargs) -> list[int]:
        return self.execute(model, "search", domain or [], **kwargs)

    def read(self, model: str, ids: list[int], fields: list[str] | None = None) -> list[dict]:
        kwargs = {}
        if fields:
            kwargs["fields"] = fields
        return self.execute(model, "read", ids, **kwargs)

    def create(self, model: str, values: dict) -> int:
        return self.execute(model, "create", values)

    def write(self, model: str, ids: list[int], values: dict) -> bool:
        return self.execute(model, "write", ids, values)

    def unlink(self, model: str, ids: list[int]) -> bool:
        return self.execute(model, "unlink", ids)

    def fields_get(self, model: str, attributes: list[str] | None = None) -> dict:
        kwargs = {}
        if attributes:
            kwargs["attributes"] = attributes
        return self.execute(model, "fields_get", **kwargs)

    def get_true_mro(self, model: str, target_model: str) -> list[str]:
        """Call get_true_mro(target_model) on *model* and return the ordered list
        of module names in the inheritance chain for *target_model*.

        Raises RuntimeError if the method is not available on the server.
        """
        try:
            return self.execute(model, "get_true_mro", target_model)
        except xmlrpc.client.Fault as exc:
            msg = str(exc)
            if "AttributeError" in msg or "get_true_mro" in msg:
                raise RuntimeError(
                    f"get_true_mro is not available on model '{model}'. "
                    "Ensure the server has this method installed."
                ) from exc
            raise
