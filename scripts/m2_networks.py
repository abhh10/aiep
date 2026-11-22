#!/usr/bin/env python3
"""Create, inspect or remove the three AIEP Docker networks (agent, proxy, egress) (approval-gated Docker step).

    PYTHONPATH=src .venv/bin/python scripts/m2_networks.py create|inspect|remove

``create`` creates only the missing AIEP networks and verifies all of them against ``docker inspect`` (it never modifies an existing
one). ``remove`` removes only AIEP-labelled, empty networks of the exact AIEP names. The Docker client is closed in a ``finally``.
Exit codes: 0 ok; 1 a network differs from policy or could not be removed safely.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aiep.networks import ALL_NETWORKS, NetworkError, ensure_networks, remove_networks, verify_network  # noqa: E402


def _default_client() -> Any:
    import docker

    return docker.from_env(timeout=60)


def main(argv: list[str] | None = None, *, client_factory: Callable[[], Any] = _default_client) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in ("create", "inspect", "remove"):
        print("usage: m2_networks.py create|inspect|remove", file=sys.stderr)
        return 2
    client = client_factory()
    try:
        if args[0] == "create":
            info = ensure_networks(client)
        elif args[0] == "inspect":
            info = {}
            for name in ALL_NETWORKS:
                net = client.networks.get(name)
                net.reload()
                problems = verify_network(net.attrs, name)
                if problems:
                    raise NetworkError(f"{name}: {'; '.join(problems)}")
                info[name] = net.attrs
        else:
            print(json.dumps({"removed": remove_networks(client)}))
            return 0
        for name, attrs in info.items():
            print(json.dumps({"name": name, "id": attrs["Id"], "driver": attrs["Driver"], "internal": attrs["Internal"],
                              "labels": attrs["Labels"], "options": attrs["Options"], "ipam": attrs.get("IPAM"),
                              "containers": sorted((attrs.get("Containers") or {}))}, sort_keys=True))
        return 0
    except NetworkError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    finally:
        try:
            client.close()
        except Exception as e:
            print(f"WARNING: closing the Docker client failed: {type(e).__name__}: {e}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
