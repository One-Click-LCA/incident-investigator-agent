"""External dependency connectivity checks (DNS + TCP)."""

import socket
from langchain_core.tools import tool

from src.utils import safe_call, tool_result


@tool
def check_external_connectivity(hostnames: list, timeout_seconds: int = 4) -> str:
    """
    Check DNS resolution and TCP port 443 reachability for a list of hostnames.
    Use to verify external dependencies (Keycloak, user-management, Typesense, Gotenberg, etc.)
    are reachable from the local network. Pass the hostname only (no protocol prefix).
    Example: ["keycloak.example.com", "typesense.example.com"]
    Returns JSON string with reachability status per hostname.
    """
    def _run():
        results = []
        for hostname in hostnames:
            # Strip protocol if accidentally included
            host = hostname.replace("https://", "").replace("http://", "").split("/")[0].split(":")[0]
            entry = {"hostname": host, "dns": "unknown", "tcp_443": "unknown", "status": "unknown"}

            try:
                ip = socket.gethostbyname(host)
                entry["dns"] = "resolved"
                entry["ip"] = ip
            except socket.gaierror as e:
                entry["dns"] = "failed"
                entry["dns_error"] = str(e)
                entry["status"] = "dns_failed"
                results.append(entry)
                continue

            try:
                sock = socket.create_connection((host, 443), timeout=timeout_seconds)
                sock.close()
                entry["tcp_443"] = "reachable"
                entry["status"] = "reachable"
            except socket.timeout:
                entry["tcp_443"] = "timeout"
                entry["status"] = "timeout"
            except OSError as e:
                entry["tcp_443"] = "unreachable"
                entry["tcp_error"] = str(e)
                entry["status"] = "unreachable"

            results.append(entry)

        unreachable = [r for r in results if r["status"] != "reachable"]
        return {
            "checked_count": len(results),
            "unreachable_count": len(unreachable),
            "results": results,
        }

    return tool_result(safe_call("check_external_connectivity", _run))
