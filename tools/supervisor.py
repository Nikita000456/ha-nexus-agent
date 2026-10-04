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
import re
import shlex
import subprocess

from fastmcp import FastMCP

mcp = FastMCP("supervisor")

_SSH_HOST = os.getenv("HA_SSH_HOST", "ha-green")
_SSH_TIMEOUT = 30
_STATUS_MARKER = "___HTTP_STATUS___"
_REDACTED = "**REDACTED**"
_SECRET_TOKENS = {"password", "passwd", "pass", "pwd", "secret", "token", "credential", "private"}
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_CREDENTIALS_IN_URL = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s/@]+:[^\s/@]+@")


def _secret_key(key: str) -> bool:
    normalized = _NON_ALNUM.sub("_", _CAMEL_BOUNDARY.sub("_", key).lower()).strip("_")
    for part in normalized.split("_"):
        singular = part.rstrip("s") or part
        if part in _SECRET_TOKENS or singular in _SECRET_TOKENS:
            return True
        if part in {"key", "keys", "apikey", "apikeys", "accesskey", "accesskeys"}:
            return True
    return False


def _schema_nodes(schema: object) -> dict[str, dict]:
    if not isinstance(schema, list):
        return {}
    return {node["name"]: node for node in schema
            if isinstance(node, dict) and isinstance(node.get("name"), str)}


def _redact_options(value: object, schema: dict[str, dict], path: str = "",
                    key: str | None = None, node: dict | None = None) -> tuple[object, list[dict]]:
    """Return a redacted copy and paths; never mutate Supervisor's response."""
    if isinstance(value, dict):
        result, fields = {}, []
        for child_key, child in value.items():
            child_node = schema.get(child_key, {})
            child_schema = _schema_nodes(child_node.get("schema")) if child_node.get("type") == "schema" else {}
            child_path = f"{path}.{child_key}" if path else str(child_key)
            result[child_key], found = _redact_options(child, child_schema, child_path, child_key, child_node)
            fields.extend(found)
        return result, fields
    if isinstance(value, list):
        result, fields = [], []
        for index, child in enumerate(value):
            new_child, found = _redact_options(child, schema, f"{path}[{index}]", key, node)
            result.append(new_child)
            fields.extend(found)
        return result, fields
    reason = None
    if (node or {}).get("format") == "password":
        reason = "schema_password"
    elif key is not None and _secret_key(key):
        reason = "key_name_heuristic"
    elif isinstance(value, str) and _CREDENTIALS_IN_URL.search(value):
        reason = "credentials_in_url"
    if reason and value not in (None, ""):
        return _REDACTED, [{"path": path, "reason": reason}]
    return value, []


def _contains_marker(value: object) -> bool:
    if value == _REDACTED:
        return True
    if isinstance(value, dict):
        return any(_contains_marker(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_marker(item) for item in value)
    return False


class RedactionMergeError(ValueError):
    pass


def _restore_markers(new: object, old: object, path: str = "") -> object:
    """Restore unchanged secrets from stored options, rejecting ambiguous lists."""
    if new == _REDACTED:
        if old is None:
            raise RedactionMergeError(f"No stored value for {path or '<root>'}")
        return old
    if isinstance(new, dict):
        old_dict = old if isinstance(old, dict) else {}
        return {key: _restore_markers(value, old_dict.get(key), f"{path}.{key}" if path else key)
                for key, value in new.items()}
    if isinstance(new, list):
        old_list = old if isinstance(old, list) else []
        if _contains_marker(new) and len(new) != len(old_list):
            raise RedactionMergeError(f"List length changed for {path or '<root>'}; re-read options")
        return [_restore_markers(value, old_list[index] if index < len(old_list) else None,
                                 f"{path}[{index}]") for index, value in enumerate(new)]
    return new


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
    """Get add-on info with secret-looking options replaced by placeholders."""
    response = _supervisor_request("GET", f"/addons/{slug}/info")
    if not isinstance(response, dict) or "error" in response:
        return response
    data = response.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("options"), dict):
        return response
    options, fields = _redact_options(data["options"], _schema_nodes(data.get("schema")))
    return {**response, "data": {**data, "options": options}, "redacted_fields": fields}


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
    """Set options, restoring unchanged values represented by redaction markers."""
    restored = _contains_marker(options)
    if restored:
        current = _supervisor_request("GET", f"/addons/{slug}/info")
        if not isinstance(current, dict) or "error" in current:
            return current
        data = current.get("data")
        stored = data.get("options") if isinstance(data, dict) else None
        if not isinstance(stored, dict):
            return {"error": "stored_options_unavailable"}
        try:
            options = _restore_markers(options, stored)
        except RedactionMergeError as exc:
            return {"error": "redaction_marker_unresolvable", "message": str(exc)}
    result = _supervisor_request("POST", f"/addons/{slug}/options", json={"options": options})
    if isinstance(result, dict) and "error" in result:
        # Supervisor errors can echo submitted values, including new secrets.
        return {"error": "options_update_failed", "message": "Supervisor rejected the options update"}
    return result


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
