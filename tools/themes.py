"""Lovelace theme management.

Themes live in `<config>/themes/<name>.yaml` and are loaded via the
`frontend:` integration. Listing/active selection is done over WS/services.
Creating/editing goes through `tools.files` (SSH to the HA host — see that
module for why: HA's config dir is not local to this machine) and triggers
`frontend.reload_themes`.
"""
import yaml
from fastmcp import FastMCP

import ha_client as ha
from tools.files import read_config_file, write_config_file, delete_config_file, list_config_files

mcp = FastMCP("themes")


def _theme_path(name: str) -> str:
    if "/" in name or "\\" in name or name.startswith(".") or not name.strip():
        raise ValueError(f"Invalid theme name: {name!r}")
    return f"themes/{name}.yaml"


@mcp.tool()
def list_themes() -> dict:
    """List all themes registered with the frontend, plus the active default theme.

    Returns: {"themes": {name: {...}}, "default_theme": "...", "default_dark_theme": "..."}
    """
    return ha._ws_call("frontend/get_themes")


@mcp.tool()
def get_theme(name: str) -> dict:
    """Get a single theme's variable map (CSS variables) by name."""
    data = ha._ws_call("frontend/get_themes")
    themes = data.get("themes") or {}
    if name not in themes:
        return {"error": f"Theme {name!r} not found", "available": list(themes.keys())}
    return {"name": name, "variables": themes[name]}


@mcp.tool()
def set_active_theme(name: str = "default", mode: str | None = None) -> dict:
    """Set the active frontend theme. `mode` can be 'light' or 'dark'."""
    payload: dict = {"name": name}
    if mode in {"light", "dark"}:
        payload["mode"] = mode
    return ha.call_service("frontend", "set_theme", payload)


@mcp.tool()
def reload_themes() -> dict:
    """Reload themes from `<config>/themes/`. Call after creating or editing theme files."""
    ha.call_service("frontend", "reload_themes")
    return {"status": "reloaded"}


@mcp.tool()
def create_theme(
    name: str,
    variables: dict,
    overwrite: bool = False,
    reload: bool = True,
) -> dict:
    """Create a new theme file at `themes/<name>.yaml` with the given CSS variables.

    The HA frontend stores themes as a dict keyed by theme name. Standard variables
    include `primary-color`, `accent-color`, `text-primary-color`, etc.
    For dark/light variants use modes: `{"name": {"modes": {"light": {...}, "dark": {...}}}}`.

    Set `overwrite=True` to replace an existing theme. By default reload_themes is called.
    """
    rel_path = _theme_path(name)
    if not overwrite:
        try:
            read_config_file(rel_path)
            return {"success": False, "error": f"Theme {name!r} already exists. Pass overwrite=True to replace."}
        except FileNotFoundError:
            pass

    document = {name: variables}
    write_result = write_config_file(rel_path, yaml.safe_dump(document, sort_keys=False, allow_unicode=True))
    if not write_result.get("success"):
        return {"success": False, "error": write_result.get("error")}

    result = {"success": True, "path": write_result["path"]}
    if reload:
        ha.call_service("frontend", "reload_themes")
        result["reloaded"] = True
    return result


@mcp.tool()
def update_theme(name: str, variables: dict, merge: bool = True, reload: bool = True) -> dict:
    """Update an existing theme file.

    `merge=True` (default) merges new variables into existing ones; `merge=False`
    replaces the whole variable set. Calls reload_themes unless reload=False.
    """
    rel_path = _theme_path(name)
    try:
        existing_content = read_config_file(rel_path)
    except FileNotFoundError:
        return {"success": False, "error": f"Theme {name!r} not found at {rel_path}"}

    if merge:
        existing = yaml.safe_load(existing_content) or {}
        current_vars = existing.get(name, {}) or {}
        current_vars.update(variables)
        document = {name: current_vars}
    else:
        document = {name: variables}

    write_result = write_config_file(rel_path, yaml.safe_dump(document, sort_keys=False, allow_unicode=True))
    if not write_result.get("success"):
        return {"success": False, "error": write_result.get("error")}

    result = {"success": True, "path": write_result["path"], "merged": merge}
    if reload:
        ha.call_service("frontend", "reload_themes")
        result["reloaded"] = True
    return result


@mcp.tool()
def delete_theme(name: str, reload: bool = True) -> dict:
    """Delete a theme file from `themes/<name>.yaml`."""
    rel_path = _theme_path(name)
    try:
        delete_result = delete_config_file(rel_path)
    except FileNotFoundError:
        return {"success": False, "error": f"Theme {name!r} not found at {rel_path}"}

    result = {"success": True, "deleted": delete_result["deleted"]}
    if reload:
        ha.call_service("frontend", "reload_themes")
        result["reloaded"] = True
    return result


@mcp.tool()
def list_theme_files() -> list[str]:
    """List YAML files under `themes/`. Useful when a theme exists on disk but isn't loaded yet."""
    return list_config_files("themes")
