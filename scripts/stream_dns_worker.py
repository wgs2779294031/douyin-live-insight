"""Resolve a name using Windows' configured DNS on connected hardware uplinks."""

from __future__ import annotations

import json
from pathlib import Path
import sys

if __name__ == "__main__":
    # The bundled interpreter uses a fixed ._pth and omits the script directory.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.stream_dns_query import query_dns
from scripts.stream_dns_topology import configured_uplink_dns
from scripts.stream_network import StreamNetworkError, _host_name


def emit_dns(hostname):
    hostname = _host_name(hostname)
    servers = configured_uplink_dns()
    if not servers:
        raise ValueError("no_uplink_dns_servers")
    for server in servers:
        for record_type in (1, 28):  # DNS A and AAAA.
            answers = query_dns(hostname, server, record_type)
            if answers:
                records = [{"address": address, "ttl": ttl} for address, ttl in answers]
                print(json.dumps({"records": records}, separators=(",", ":")), flush=True)
    print('{"records":[]}', flush=True)


def main():
    if len(sys.argv) != 2:
        return 2
    try:
        emit_dns(sys.argv[1])
        return 0
    except (OSError, ValueError, StreamNetworkError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
