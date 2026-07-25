import os
import shlex
import subprocess
import yaml
from pathlib import PurePosixPath
from fastmcp import FastMCP
from dotenv import load_dotenv

load_dotenv()

mcp = FastMCP("files")


class _HALoader(yaml.SafeLoader):
    """SafeLoader that tolerates Home Assistant's custom YAML tags.

    HA configs routinely contain `!include`, `!include_dir_merge_named`,
    `!secret`, `!env_var`, etc. The stock SafeLoader rejects them, so any
    validation of a real HA file fails. We can't resolve them (we don't have
    file/secret access from here), but for syntax checking it's enough to keep
    them as opaque tagged values.
    """


def _ha_tag_passthrough(loader: yaml.Loader, tag_suffix: str, node: yaml.Node):
    if isinstance(node, yaml.ScalarNode):
        return {"__ha_tag__": tag_suffix, "value": loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {"__ha_tag__": tag_suffix, "value": loader.construct_sequence(node, deep=True)}
    if isinstance(node, yaml.MappingNode):
        return {"__ha_tag__": tag_suffix, "value": loader.construct_mapping(node, deep=True)}
    return None


for _tag in (
    "!include",
    "!include_dir_list",
    "!include_dir_merge_list",
    "!include_dir_named",
    "!include_dir_merge_named",
    "!secret",
    "!env_var",
    "!input",
):
    _HALoader.add_constructor(_tag, lambda loader, node, t=_tag: _ha_tag_passthrough(loader, t, node))


def _ha_yaml_load(content: str):
    return yaml.load(content, Loader=_HALoader)

# HA's config directory lives on the remote HA host, not on this machine — there is
# no local /config to read/write. All file access goes over SSH to the host below
# (see ~/.ssh/config), which must have a passwordless key configured.
_CONFIG_PATH = "/config"
_SSH_HOST = os.getenv("HA_SSH_HOST", "ha-green")
_SSH_TIMEOUT = 15

_ALLOWED_EXTENSIONS = {".yaml", ".yml", ".json", ".txt"}
_BLOCKED_PATHS = {"secrets.yaml", ".storage"}


def _ssh(remote_cmd: str, input_data: str | None = None) -> subprocess.CompletedProcess:
    """Run a command on the HA host over SSH."""
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={_SSH_TIMEOUT}", _SSH_HOST, remote_cmd],
        input=input_data,
        capture_output=True,
        text=True,
        timeout=_SSH_TIMEOUT + 10,
    )


def _safe_remote_path(relative_path: str) -> str:
    """Validate relative_path and return the absolute remote path."""
    rel = PurePosixPath(relative_path)
    if rel.is_absolute() or ".." in rel.parts:
        raise PermissionError(f"Path outside config directory: {relative_path}")
    if rel.suffix not in _ALLOWED_EXTENSIONS:
        raise PermissionError(f"File extension not allowed: {rel.suffix}")
    if any(blocked in rel.parts for blocked in _BLOCKED_PATHS):
        raise PermissionError(f"Access to blocked path segment in: {relative_path}")
    return f"{_CONFIG_PATH}/{rel}"


@mcp.tool()
def read_config_file(relative_path: str) -> str:
    """Read a config file relative to HA config dir. E.g. 'automations.yaml' or 'packages/lights.yaml'."""
    remote_path = _safe_remote_path(relative_path)
    result = _ssh(f"cat {shlex.quote(remote_path)}")
    if result.returncode != 0:
        if "No such file" in result.stderr:
            raise FileNotFoundError(f"File not found: {remote_path}")
        raise RuntimeError(f"SSH read failed ({_SSH_HOST}): {result.stderr.strip()}")
    return result.stdout


@mcp.tool()
def write_config_file(relative_path: str, content: str, validate_yaml: bool = True) -> dict:
    """Write content to a config file. Validates YAML syntax before saving (set validate_yaml=False to skip).
    Creates parent directories as needed.
    """
    remote_path = _safe_remote_path(relative_path)
    suffix = PurePosixPath(remote_path).suffix

    if validate_yaml and suffix in {".yaml", ".yml"}:
        try:
            _ha_yaml_load(content)
        except yaml.YAMLError as e:
            return {"success": False, "error": f"YAML validation failed: {e}"}

    remote_dir = str(PurePosixPath(remote_path).parent)
    cmd = f"mkdir -p {shlex.quote(remote_dir)} && cat > {shlex.quote(remote_path)}"
    result = _ssh(cmd, input_data=content)
    if result.returncode != 0:
        return {"success": False, "error": f"SSH write failed ({_SSH_HOST}): {result.stderr.strip()}"}
    return {"success": True, "path": remote_path}


@mcp.tool()
def list_config_files(subdirectory: str = "") -> list[str]:
    """List files in the HA config directory (or a subdirectory)."""
    rel = PurePosixPath(subdirectory) if subdirectory else PurePosixPath(".")
    if rel.is_absolute() or ".." in rel.parts:
        raise PermissionError(f"Path outside config directory: {subdirectory}")
    remote_dir = f"{_CONFIG_PATH}/{rel}" if subdirectory else _CONFIG_PATH
    result = _ssh(f"find {shlex.quote(remote_dir)} -type f 2>/dev/null")
    if result.returncode != 0:
        return []
    files = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        p = PurePosixPath(line)
        if p.suffix not in _ALLOWED_EXTENSIONS or any(b in p.parts for b in _BLOCKED_PATHS):
            continue
        try:
            files.append(str(p.relative_to(_CONFIG_PATH)))
        except ValueError:
            continue
    return files


@mcp.tool()
def validate_yaml_content(content: str) -> dict:
    """Validate YAML content without saving. Returns parsed result or error.

    Tolerates Home Assistant custom tags (`!include`, `!secret`, `!env_var`, …)
    so real HA config files validate cleanly.
    """
    try:
        parsed = _ha_yaml_load(content)
        return {"valid": True, "type": type(parsed).__name__}
    except yaml.YAMLError as e:
        return {"valid": False, "error": str(e)}


@mcp.tool()
def delete_config_file(relative_path: str) -> dict:
    """Delete a config file. Will NOT delete if it has no known safe extension."""
    remote_path = _safe_remote_path(relative_path)
    check = _ssh(f"test -f {shlex.quote(remote_path)}")
    if check.returncode != 0:
        raise FileNotFoundError(f"File not found: {remote_path}")
    result = _ssh(f"rm {shlex.quote(remote_path)}")
    if result.returncode != 0:
        raise RuntimeError(f"SSH delete failed ({_SSH_HOST}): {result.stderr.strip()}")
    return {"success": True, "deleted": remote_path}


@mcp.tool()
def append_to_config_file(relative_path: str, content: str, validate_yaml: bool = False) -> dict:
    """Append content to an existing config file (e.g. adding an automation entry to automations.yaml)."""
    remote_path = _safe_remote_path(relative_path)
    check = _ssh(f"test -f {shlex.quote(remote_path)}")
    if check.returncode != 0:
        raise FileNotFoundError(f"File not found: {remote_path}")

    read_result = _ssh(f"cat {shlex.quote(remote_path)}")
    if read_result.returncode != 0:
        raise RuntimeError(f"SSH read failed ({_SSH_HOST}): {read_result.stderr.strip()}")
    combined = read_result.stdout + "\n" + content

    suffix = PurePosixPath(remote_path).suffix
    if validate_yaml and suffix in {".yaml", ".yml"}:
        try:
            _ha_yaml_load(combined)
        except yaml.YAMLError as e:
            return {"success": False, "error": f"YAML validation failed after append: {e}"}

    write_result = _ssh(f"cat > {shlex.quote(remote_path)}", input_data=combined)
    if write_result.returncode != 0:
        return {"success": False, "error": f"SSH write failed ({_SSH_HOST}): {write_result.stderr.strip()}"}
    return {"success": True, "path": remote_path}
