#!/usr/bin/env python3
"""Run Ella's isolated Photon transport bridge.

The launcher must set ``HERMES_IMPORT_ROOT`` to the reviewed immutable Hermes
checkout and ``HERMES_HOME`` to the isolated transport-only home before this
entrypoint starts. Secrets are loaded by the service manager from protected
files; no command accepts secret values.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import stat
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import httpx

HERMES_IMPORT_ROOT = Path(os.getenv("HERMES_IMPORT_ROOT", ""))
if not HERMES_IMPORT_ROOT.is_absolute():
    raise SystemExit("HERMES_IMPORT_ROOT must name the immutable Hermes checkout")
HERMES_HOME = Path(os.getenv("HERMES_HOME", ""))
if not HERMES_HOME.is_absolute() or HERMES_HOME.resolve() == (Path.home() / ".hermes").resolve():
    raise SystemExit("HERMES_HOME must name the isolated Ella transport home")
sys.path.insert(0, str(HERMES_IMPORT_ROOT))

from gateway.config import PlatformConfig  # noqa: E402
from plugins.platforms.photon import auth as photon_auth  # noqa: E402
from plugins.platforms.photon.adapter import PhotonAdapter  # noqa: E402

from bridge import (  # noqa: E402
    BridgeConfig,
    BridgeError,
    BridgeHttpServer,
    BridgeJournal,
    HttpEllaBackend,
    ImessagePhotonBridge,
    SingletonLease,
    _is_provider_stream_healthy,
)


class HermesPhotonProvider:
    """Pinned Hermes adapter plus its Spectrum user-management client."""

    def __init__(self) -> None:
        self.adapter: Optional[PhotonAdapter] = None

    async def connect(self, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> bool:
        adapter = PhotonAdapter(PlatformConfig(enabled=True, token="", extra={}))

        async def handle(message_event: Any) -> None:
            raw = getattr(message_event, "raw_message", None)
            if isinstance(raw, dict):
                await handler(raw)

        adapter.handle_message = handle
        connected = await adapter.connect()
        if connected:
            self.adapter = adapter
        return connected

    async def disconnect(self) -> None:
        if self.adapter is not None:
            await self.adapter.disconnect()
            self.adapter = None

    async def healthy(self) -> bool:
        adapter = self.adapter
        if adapter is None:
            return False
        try:
            body = await adapter._sidecar_call("/healthz", {})
        except Exception:
            return False
        return _is_provider_stream_healthy(body)

    async def list_users(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            photon_auth.list_users,
            os.environ["PHOTON_PROJECT_ID"],
            os.environ["PHOTON_PROJECT_SECRET"],
        )

    async def register_user(self, handset_e164: str) -> dict[str, Any]:
        user, _created = await asyncio.to_thread(
            photon_auth.register_user_if_absent,
            os.environ["PHOTON_PROJECT_ID"],
            os.environ["PHOTON_PROJECT_SECRET"],
            phone_number=handset_e164,
        )
        return user

    async def send_text(self, handset_e164: str, text: str) -> Optional[str]:
        if self.adapter is None:
            raise BridgeError("bridge_provider_not_connected")
        result = await self.adapter.send(handset_e164, text)
        if not result.success:
            raise BridgeError("bridge_provider_send_failed")
        return str(result.message_id) if result.message_id else None


async def _serve(config: BridgeConfig, *, reconcile_only: bool = False) -> int:
    _prepare_state_directory(config.state_directory)
    lease = SingletonLease(config.state_directory / "bridge.lock")
    lease.acquire()
    journal = BridgeJournal(config.state_directory, config.project_id)
    provider = HermesPhotonProvider()
    backend = HttpEllaBackend(config)
    bridge = ImessagePhotonBridge(config=config, journal=journal, provider=provider, backend=backend)
    server = BridgeHttpServer(bridge)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass
    try:
        await bridge.start()
        if reconcile_only:
            await bridge.queue.join()
            return 0
        await server.start()
        await stop_event.wait()
        return 0
    finally:
        await server.stop()
        await bridge.stop()
        journal.close()
        lease.close()


async def _deregister(config: BridgeConfig) -> int:
    _prepare_state_directory(config.state_directory)
    lease = SingletonLease(config.state_directory / "bridge.lock")
    lease.acquire()
    journal = BridgeJournal(config.state_directory, config.project_id)
    backend = HttpEllaBackend(config)
    bridge = ImessagePhotonBridge(
        config=config,
        journal=journal,
        provider=HermesPhotonProvider(),
        backend=backend,
    )
    try:
        await bridge.deregister_all()
        return 0
    finally:
        journal.close()
        lease.close()


async def _health(config: BridgeConfig) -> int:
    url = f"http://{config.registrar_bind}:{config.registrar_port}/healthz"
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=False, trust_env=False) as client:
            response = await client.get(url)
    except httpx.HTTPError:
        return 1
    return 0 if response.status_code == 200 and response.json().get("status") == "ready" else 1


def _prepare_state_directory(path: Path) -> None:
    if path.exists() and path.is_symlink():
        raise BridgeError("bridge_state_directory_symlink_refused")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.stat()
    if stat.S_IMODE(metadata.st_mode) != 0o700 or metadata.st_uid != os.getuid():
        raise BridgeError("bridge_state_directory_insecure")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("serve", "health", "reconcile", "deregister"), nargs="?", default="serve")
    args = parser.parse_args()
    try:
        config = BridgeConfig.from_environment()
        if args.command == "serve":
            return asyncio.run(_serve(config))
        if args.command == "reconcile":
            return asyncio.run(_serve(config, reconcile_only=True))
        if args.command == "deregister":
            return asyncio.run(_deregister(config))
        return asyncio.run(_health(config))
    except BridgeError as exc:
        print(exc.code, file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
