"""Home Assistant Supervisor API — add-on lifecycle, backups, host/core management.

The internal `supervisor` hostname and SUPERVISOR_TOKEN only exist inside HA's own
add-on containers — this MCP server runs on a separate machine with no route to
either. The HA host (SSH alias `ha-green`) IS on that internal network, and its
SSH session already carries a scoped SUPERVISOR_TOKEN env var, so requests are
made by running curl there instead of locally (same REST paths/payloads as
before — only the transport changed).
"""
import json as json_mod
import os
import shlex
import subprocess

from fastmcp import FastMCP

mcp = FastMCP("supervisor")

_SSH_HOST = os.getenv("HA_SSH_HOST", "ha-green")
_SSH_TIMEOUT = 30
_STATUS_MARKER = "___HTTP_STATUS___"


def _ssh_run(remote_cmd: str, input_data: str | None = None) -> subprocess.CompletedProcess:
    """Run a command on the HA host over SSH."""
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={_SSH_TIMEOUT}", _SSH_HOST, remote_cmd],
        input=input_data,
        capture_output=True,
        text=True,
        timeout=_SSH_TIMEOUT + 15,
    )


def _supervisor_request(method: str, path: str, json: dict | None = None) -> dict:
    """Internal: call Supervisor REST API via curl on the HA host (over SSH)."""
    parts = [
        "curl", "-sS", "--max-time", "30",
        "-X", shlex.quote(method.upper()),
        "-H", '"Authorization: Bearer $SUPERVISOR_TOKEN"',
        "-w", shlex.quote(f"\n{_STATUS_MARKER}%{{http_code}}"),
    ]
    input_data = None
    if json is not None:
        parts += ["-H", shlex.quote("Content-Type: application/json"), "--data-binary", "@-"]
        input_data = json_mod.dumps(json)
    parts += [shlex.quote(f"http://supervisor{path}")]
    try:
        result = _ssh_run(" ".join(parts), input_data=input_data)
    except subprocess.TimeoutExpired:
        return {"error": f"SSH/curl timed out ({_SSH_HOST})"}
    if result.returncode != 0:
        return {"error": f"SSH/curl failed ({_SSH_HOST}): {result.stderr.strip()}"}
    raw = result.stdout
    if _STATUS_MARKER not in raw:
        return {"error": "unexpected curl output", "raw": raw[:500]}
    body_text, status_text = raw.rsplit(_STATUS_MARKER, 1)
    try:
        status_code = int(status_text.strip())
    except ValueError:
        status_code = 0
    try:
        data = json_mod.loads(body_text) if body_text.strip() else {}
    except json_mod.JSONDecodeError:
        return {"error": f"non-JSON response (HTTP {status_code})", "raw": body_text[:500]}
    if status_code >= 400:
        return {"error": f"HTTP {status_code}", "detail": data}
    return data


def _supervisor_get_text(path: str) -> str:
    """Internal: GET a text endpoint (e.g. logs) instead of JSON."""
    parts = [
        "curl", "-sS", "--max-time", "30",
        "-H", '"Authorization: Bearer $SUPERVISOR_TOKEN"',
        "-w", shlex.quote(f"\n{_STATUS_MARKER}%{{http_code}}"),
        shlex.quote(f"http://supervisor{path}"),
    ]
    result = _ssh_run(" ".join(parts))
    if result.returncode != 0:
        raise RuntimeError(f"SSH/curl failed ({_SSH_HOST}): {result.stderr.strip()}")
    raw = result.stdout
    if _STATUS_MARKER not in raw:
        raise RuntimeError(f"unexpected curl output: {raw[:200]}")
    body_text, status_text = raw.rsplit(_STATUS_MARKER, 1)
    status_code = int(status_text.strip()) if status_text.strip().isdigit() else 0
    if status_code >= 400:
        raise RuntimeError(f"HTTP {status_code}: {body_text[:200]}")
    return body_text


# --- Add-on lifecycle ---

@mcp.tool()
def list_addons() -> dict:
    """List all installed add-ons with slug, name, state, version, update_available."""
    resp = _supervisor_request("GET", "/addons")
    if "error" in resp:
        return resp
    data = resp.get("data", {}) if isinstance(resp, dict) else {}
    addons = data.get("addons", []) if isinstance(data, dict) else []
    return {
        "addons": [
            {
                "slug": a.get("slug"),
                "name": a.get("name"),
                "state": a.get("state"),
                "version": a.get("version"),
                "version_latest": a.get("version_latest"),
                "update_available": a.get("update_available", False),
            }
            for a in addons
        ]
    }


@mcp.tool()
def get_addon(slug: str) -> dict:
    """Get full info for a specific add-on by slug."""
    return _supervisor_request("GET", f"/addons/{slug}/info")


@mcp.tool()
def install_addon(slug: str) -> dict:
    """Install an add-on from its slug (must already be in a registered repository)."""
    return _supervisor_request("POST", f"/addons/{slug}/install")


@mcp.tool()
def uninstall_addon(slug: str, confirm: bool = False) -> dict:
    """Uninstall an add-on. DANGEROUS — requires confirm=True."""
    if not confirm:
        return {"error": "set confirm=True to proceed"}
    return _supervisor_request("POST", f"/addons/{slug}/uninstall")


@mcp.tool()
def start_addon(slug: str) -> dict:
    """Start an installed add-on."""
    return _supervisor_request("POST", f"/addons/{slug}/start")


@mcp.tool()
def stop_addon(slug: str) -> dict:
    """Stop a running add-on."""
    return _supervisor_request("POST", f"/addons/{slug}/stop")


@mcp.tool()
def restart_addon(slug: str) -> dict:
    """Restart an add-on (stop + start)."""
    return _supervisor_request("POST", f"/addons/{slug}/restart")


@mcp.tool()
def update_addon(slug: str) -> dict:
    """Update an add-on to the latest available version."""
    return _supervisor_request("POST", f"/addons/{slug}/update")


@mcp.tool()
def get_addon_logs(slug: str, lines: int = 100) -> dict:
    """Get the last N log lines from an add-on (returns text wrapped in {logs: ...})."""
    try:
        text = _supervisor_get_text(f"/addons/{slug}/logs")
    except Exception as e:
        return {"error": str(e)}
    if not text:
        return {"error": "empty response"}
    log_lines = text.splitlines()
    if lines > 0:
        log_lines = log_lines[-lines:]
    return {"logs": "\n".join(log_lines)}


@mcp.tool()
def set_addon_options(slug: str, options: dict) -> dict:
    """Set configuration options for an add-on (sent as {"options": options})."""
    return _supervisor_request("POST", f"/addons/{slug}/options", json={"options": options})


@mcp.tool()
def get_addon_stats(slug: str) -> dict:
    """Get runtime resource stats (CPU, memory, network, IO) for an add-on."""
    return _supervisor_request("GET", f"/addons/{slug}/stats")


# --- Supervisor self / Core / Host ---

@mcp.tool()
def get_supervisor_info() -> dict:
    """Get info about the Supervisor itself (version, channel, healthy)."""
    return _supervisor_request("GET", "/supervisor/info")


@mcp.tool()
def get_core_info() -> dict:
    """Get info about Home Assistant Core (version, arch, machine)."""
    return _supervisor_request("GET", "/core/info")


@mcp.tool()
def get_host_info() -> dict:
    """Get info about the host OS (HAOS version, hostname, kernel, etc.)."""
    return _supervisor_request("GET", "/host/info")


@mcp.tool()
def restart_core(confirm: bool = False) -> dict:
    """Restart Home Assistant Core. DANGEROUS — requires confirm=True."""
    if not confirm:
        return {"error": "set confirm=True to proceed"}
    return _supervisor_request("POST", "/core/restart")


@mcp.tool()
def restart_host(confirm: bool = False) -> dict:
    """Reboot the host machine. VERY DANGEROUS — requires confirm=True."""
    if not confirm:
        return {"error": "set confirm=True to proceed"}
    return _supervisor_request("POST", "/host/reboot")


# --- Backups ---

@mcp.tool()
def list_backups() -> dict:
    """List all backups managed by Supervisor."""
    return _supervisor_request("GET", "/backups")


@mcp.tool()
def create_backup(
    name: str,
    addons: list[str] | None = None,
    folders: list[str] | None = None,
    password: str | None = None,
) -> dict:
    """Create a full backup, or partial when addons/folders are provided."""
    payload: dict = {"name": name}
    if password:
        payload["password"] = password
    if addons is not None or folders is not None:
        if addons is not None:
            payload["addons"] = addons
        if folders is not None:
            payload["folders"] = folders
        return _supervisor_request("POST", "/backups/new/partial", json=payload)
    return _supervisor_request("POST", "/backups/new/full", json=payload)


@mcp.tool()
def restore_backup(slug: str, password: str | None = None, confirm: bool = False) -> dict:
    """Restore a full backup by slug. DANGEROUS — requires confirm=True."""
    if not confirm:
        return {"error": "set confirm=True to proceed"}
    payload: dict = {}
    if password:
        payload["password"] = password
    return _supervisor_request("POST", f"/backups/{slug}/restore/full", json=payload)


@mcp.tool()
def delete_backup(slug: str) -> dict:
    """Delete a backup by slug."""
    return _supervisor_request("DELETE", f"/backups/{slug}")
