"""Direct MCP adapter: live initialize/tools-list is the only contract source."""

from __future__ import annotations

import asyncio
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Mapping

from core import HouseholdError, cart_summary


ODA_ENDPOINT = "https://oda.com/mcp"
SERVER_NAME = "oda-weekly"
REQUIRED_TOOLS = frozenset({
    "product_search", "recipe_search", "likely_to_buy", "get_cart",
    "manipulate_cart", "get_delivery_addresses", "get_delivery_slots",
    "select_delivery_slot", "get_orders", "get_order", "order_tracking",
})


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return _json_value(dump(mode="json", by_alias=True, exclude_none=True))
    raise HouseholdError("Oda returned an unsupported value")


class OdaClient:
    def __init__(self, token_directory: Path | str):
        self.token_directory = Path(token_directory)

    def probe(self) -> dict[str, Any]:
        return self._run(None, {}, 90.0)

    def call(self, tool: str, arguments: Mapping[str, Any], *, deadline: float | None = None) -> dict[str, Any]:
        if not isinstance(tool, str) or not tool:
            raise HouseholdError("Oda tool is missing")
        timeout = 90.0 if deadline is None else deadline - time.monotonic()
        if timeout <= 0:
            raise HouseholdError("Oda operation deadline reached")
        return self._run(tool, dict(arguments), min(timeout, 90.0))

    def _run(self, tool: str | None, arguments: dict[str, Any], timeout: float) -> dict[str, Any]:
        if not self.token_directory.is_dir():
            raise HouseholdError("Oda login is required")
        with self._lock():
            try:
                return asyncio.run(self._run_async(tool, arguments, timeout))
            except HouseholdError:
                raise
            except Exception as exc:
                raise HouseholdError("Oda MCP is unavailable") from exc

    async def _run_async(self, tool: str | None, arguments: dict[str, Any], timeout: float) -> dict[str, Any]:
        try:
            import httpx2 as httpx
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client
            from mcp.types import LATEST_PROTOCOL_VERSION
            from tools.mcp_oauth import (
                HermesTokenStorage,
                _build_client_metadata,
                _configure_callback_port,
                _make_callback_waiter,
                _make_redirect_handler,
            )
            from tools.mcp_oauth_manager import _HERMES_PROVIDER_CLS
        except ImportError as exc:
            raise HouseholdError("Hermes MCP runtime is unavailable") from exc

        token_directory = self.token_directory

        class ExactStorage(HermesTokenStorage):
            def __init__(self) -> None:
                super().__init__(SERVER_NAME, hermes_home=token_directory.parent)

            def _tokens_path(self) -> Path:
                return token_directory / f"{SERVER_NAME}.json"

            def _client_info_path(self) -> Path:
                return token_directory / f"{SERVER_NAME}.client.json"

            def _meta_path(self) -> Path:
                return token_directory / f"{SERVER_NAME}.meta.json"

        storage = ExactStorage()
        if not storage.has_cached_tokens() or _HERMES_PROVIDER_CLS is None:
            raise HouseholdError("Oda login is required")
        oauth_config: dict[str, Any] = {}
        _configure_callback_port(oauth_config, storage)
        port = oauth_config.get("_resolved_port", 0)
        provider = _HERMES_PROVIDER_CLS(
            server_name=SERVER_NAME,
            preregistered=False,
            server_url=ODA_ENDPOINT,
            client_metadata=_build_client_metadata(oauth_config),
            storage=storage,
            redirect_handler=_make_redirect_handler(port),
            callback_handler=_make_callback_waiter(port, timeout=min(30.0, timeout)),
        )
        provider._hermes_home = str(token_directory.parent.resolve())
        original = httpx.URL(ODA_ENDPOINT)

        async def same_origin(response: Any) -> None:
            if response.is_redirect and response.next_request is not None:
                target = response.next_request.url
                if (target.scheme, target.host, target.port) != (original.scheme, original.host, original.port):
                    raise HouseholdError("Oda MCP redirected outside Oda")

        async with httpx.AsyncClient(
            auth=provider,
            follow_redirects=True,
            headers={"mcp-protocol-version": LATEST_PROTOCOL_VERSION},
            timeout=httpx.Timeout(timeout, read=min(60.0, timeout)),
            event_hooks={"response": [same_origin]},
        ) as client:
            async with streamable_http_client(ODA_ENDPOINT, http_client=client, terminate_on_close=False) as streams:
                async with ClientSession(streams[0], streams[1], read_timeout_seconds=timeout) as session:
                    async with asyncio.timeout(timeout):
                        initialized = await session.initialize()
                        tools: list[Any] = []
                        cursor: str | None = None
                        for _ in range(10):
                            page = await (session.list_tools(cursor=cursor) if cursor else session.list_tools())
                            tools.extend(page.tools)
                            cursor = getattr(page, "next_cursor", getattr(page, "nextCursor", None))
                            if not cursor:
                                break
                        names = {str(getattr(item, "name", "")) for item in tools}
                        missing = sorted(REQUIRED_TOOLS - names)
                        if missing:
                            raise HouseholdError("Oda MCP lacks required operations: " + ", ".join(missing))
                        server_info = _json_value(getattr(initialized, "server_info", None))
                        protocol = str(getattr(initialized, "protocol_version", ""))
                        status = {
                            "status": "ready",
                            "protocol_version": protocol,
                            "server": server_info,
                            "tool_count": len(names),
                            "tools": sorted(names),
                        }
                        if tool is None:
                            return status
                        if tool not in names:
                            raise HouseholdError(f"Oda does not expose {tool}")
                        result = await session.call_tool(tool, arguments)
        if bool(getattr(result, "is_error", getattr(result, "isError", False))):
            raise HouseholdError("Oda rejected the operation")
        structured = getattr(result, "structured_content", getattr(result, "structuredContent", None))
        value = _json_value(structured)
        if not isinstance(value, dict):
            raise HouseholdError("Oda returned no structured result")
        return value

    @contextmanager
    def _lock(self):
        path = self.token_directory / ".oda-household.lock"
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise HouseholdError("another Oda operation is active") from exc
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Oda MCP startup/preflight check")
    parser.add_argument("--tokens", type=Path, required=True)
    parser.add_argument("--state", action="store_true", help="also read a secret-free cart/order baseline")
    args = parser.parse_args()
    client = OdaClient(args.tokens)
    probe = client.probe()
    output: dict[str, Any] = {
        "status": probe["status"],
        "protocol_version": probe["protocol_version"],
        "server": probe["server"],
        "tool_count": probe["tool_count"],
    }
    if args.state:
        cart = cart_summary(client.call("get_cart", {}))
        orders = client.call("get_orders", {"page": 1, "size": 20})
        encoded = json.dumps({"cart": cart, "orders": orders}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        values = orders.get("orders") if isinstance(orders.get("orders"), list) else []
        latest_status = None
        if values:
            latest = values[0]
            order_id = str(latest.get("orderNumber") or latest.get("order_number") or "") if isinstance(latest, Mapping) else ""
            if order_id:
                latest_status = client.call("order_tracking", {"order_number": order_id}).get("status")
        output["baseline"] = {
            "digest": hashlib.sha256(encoded).hexdigest(),
            "cart_lines": len(cart["items"]),
            "cart_count": cart["count"],
            "cart_total": cart["total"],
            "delivery_selected": bool((cart.get("delivery") or {}).get("display")),
            "orders_returned": len(values),
            "latest_order_status": latest_status,
        }
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
