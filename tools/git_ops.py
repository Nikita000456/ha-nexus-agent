import os
import shlex
import subprocess

from fastmcp import FastMCP
from dotenv import load_dotenv

load_dotenv()

mcp = FastMCP("git_ops")

# Git for the HA config directory has to run ON the HA host itself (over SSH) —
# there is no local /config on this machine, and there is no HA WS/REST command
# that can run arbitrary git operations. See ~/.ssh/config host `ha-green`.
_SSH_HOST = os.getenv("HA_SSH_HOST", "ha-green")
_CONFIG_PATH = "/config"
_SSH_TIMEOUT = 30

_DEFAULT_GITIGNORE = (
    "secrets.yaml\n"
    ".storage/\n"
    "*.db\n"
    "*.db-shm\n"
    "*.db-wal\n"
    "home-assistant.log\n"
    "deps/\n"
    "custom_components/\n"
    "zigbee2mqtt/\n"
    ".cache/\n"
)


def _ssh_run(remote_cmd: str, input_data: str | None = None) -> subprocess.CompletedProcess:
    """Run a command on the HA host over SSH."""
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={_SSH_TIMEOUT}", _SSH_HOST, remote_cmd],
        input=input_data,
        capture_output=True,
        text=True,
        timeout=_SSH_TIMEOUT + 15,
    )


def _git(*args: str) -> subprocess.CompletedProcess:
    """Run `git <args>` inside the HA config dir, on the HA host."""
    quoted_args = " ".join(shlex.quote(a) for a in args)
    return _ssh_run(f"cd {shlex.quote(_CONFIG_PATH)} && git {quoted_args}")


def _require_repo() -> None:
    check = _ssh_run(f"test -d {shlex.quote(_CONFIG_PATH)}/.git")
    if check.returncode != 0:
        raise RuntimeError(f"No git repo at {_CONFIG_PATH} on {_SSH_HOST}. Run git_init_config() first.")


@mcp.tool()
def git_init_config() -> dict:
    """Initialize git repository in HA config directory. Run once before using other git tools."""
    check = _ssh_run(f"test -d {shlex.quote(_CONFIG_PATH)}/.git")
    if check.returncode == 0:
        return {"status": "already_initialized", "path": _CONFIG_PATH}

    write_result = _ssh_run(f"cat > {shlex.quote(_CONFIG_PATH)}/.gitignore", input_data=_DEFAULT_GITIGNORE)
    if write_result.returncode != 0:
        return {"status": "error", "error": f"failed to write .gitignore: {write_result.stderr.strip()}"}

    init_cmd = (
        f"cd {shlex.quote(_CONFIG_PATH)} && "
        "git init -q && "
        "git config user.name 'ha-nexus-agent' && "
        "git config user.email 'ha-nexus-agent@local' && "
        "git add .gitignore && "
        "git commit -q -m 'chore: init HA config repository'"
    )
    result = _ssh_run(init_cmd)
    if result.returncode != 0:
        return {"status": "error", "error": result.stderr.strip()}
    return {"status": "initialized", "path": _CONFIG_PATH}


@mcp.tool()
def git_status() -> dict:
    """Show current git status of HA config directory."""
    _require_repo()
    branch_result = _git("rev-parse", "--abbrev-ref", "HEAD")
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "detached"

    status_result = _git("status", "--porcelain")
    staged: list[str] = []
    modified: list[str] = []
    untracked: list[str] = []
    for line in status_result.stdout.splitlines():
        if not line:
            continue
        code, path = line[:2], line[3:]
        if code == "??":
            untracked.append(path)
            continue
        if code[0] != " ":
            staged.append(path)
        if code[1] != " ":
            modified.append(path)

    return {
        "branch": branch,
        "staged": staged,
        "modified": modified,
        "untracked": untracked,
        "is_dirty": bool(staged or modified or untracked),
    }


@mcp.tool()
def git_commit_all(message: str) -> dict:
    """Stage all changes and commit them with a message. Use before making risky changes as a checkpoint."""
    _require_repo()
    _git("add", "-A")
    status = _git("status", "--porcelain")
    if not status.stdout.strip():
        return {"status": "nothing_to_commit"}
    commit_result = _git("commit", "-m", message)
    if commit_result.returncode != 0:
        return {"status": "error", "error": commit_result.stderr.strip()}
    sha = _git("rev-parse", "--short", "HEAD").stdout.strip()
    return {"status": "committed", "sha": sha, "message": message}


@mcp.tool()
def git_log(limit: int = 20) -> list[dict]:
    """Show recent git commits in the HA config directory."""
    _require_repo()
    result = _git("log", f"-n{limit}", "--pretty=format:%h%x1f%s%x1f%an%x1f%aI")
    if result.returncode != 0:
        return []
    commits = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        parts = line.split("\x1f")
        if len(parts) != 4:
            continue
        sha, message, author, date = parts
        commits.append({"sha": sha, "message": message, "author": author, "date": date})
    return commits


@mcp.tool()
def git_diff(sha: str | None = None) -> str:
    """Show diff of uncommitted changes, or diff of a specific commit (by SHA)."""
    _require_repo()
    result = _git("show", sha, "--stat") if sha else _git("diff")
    if result.returncode != 0:
        raise RuntimeError(f"git diff failed: {result.stderr.strip()}")
    return result.stdout


@mcp.tool()
def git_rollback_file(relative_path: str, sha: str = "HEAD", confirm: bool = False) -> dict:
    """Restore a single file to its state at a specific commit (default: HEAD = undo uncommitted changes).

    Set confirm=True to proceed; without it returns a safety prompt.
    """
    if not confirm:
        return {
            "error": "confirmation_required",
            "message": f"This will restore '{relative_path}' to {sha}. Uncommitted changes to this file will be lost.",
            "action": f"git_rollback_file(relative_path='{relative_path}', sha='{sha}', confirm=True)",
        }
    _require_repo()
    result = _git("checkout", sha, "--", relative_path)
    if result.returncode != 0:
        return {"status": "error", "error": result.stderr.strip()}
    return {"status": "restored", "file": relative_path, "to": sha}


@mcp.tool()
def git_rollback_to_commit(sha: str, confirm: bool = False) -> dict:
    """Hard-reset config to a previous commit. WARNING: ALL changes after that commit are permanently lost.

    Set confirm=True to proceed; without it returns a safety prompt.
    """
    if not confirm:
        try:
            _require_repo()
            current = _git("rev-parse", "--short", "HEAD").stdout.strip()
        except Exception:
            current = "unknown"
        return {
            "error": "confirmation_required",
            "message": (
                f"This will hard-reset ALL config files to commit {sha}. "
                f"Current HEAD is {current}. Every change after {sha} will be PERMANENTLY LOST."
            ),
            "action": f"git_rollback_to_commit(sha='{sha}', confirm=True)",
        }
    _require_repo()
    result = _git("reset", "--hard", sha)
    if result.returncode != 0:
        return {"status": "error", "error": result.stderr.strip()}
    return {"status": "reset", "to": sha}


@mcp.tool()
def git_create_branch(branch_name: str) -> dict:
    """Create a new branch (useful before experimental changes)."""
    _require_repo()
    result = _git("checkout", "-b", branch_name)
    if result.returncode != 0:
        return {"status": "error", "error": result.stderr.strip()}
    return {"status": "created_and_checked_out", "branch": branch_name}


@mcp.tool()
def git_checkout_branch(branch_name: str) -> dict:
    """Switch to an existing branch."""
    _require_repo()
    result = _git("checkout", branch_name)
    if result.returncode != 0:
        return {"status": "error", "error": result.stderr.strip()}
    return {"status": "switched", "branch": branch_name}


@mcp.tool()
def git_list_branches() -> list[str]:
    """List all local branches in HA config repo."""
    _require_repo()
    result = _git("branch", "--format=%(refname:short)")
    return [b for b in result.stdout.splitlines() if b.strip()]


@mcp.tool()
def safe_write_with_checkpoint(relative_path: str, content: str, commit_message: str | None = None) -> dict:
    """Write a config file AND automatically git-commit the current state before writing.
    This is the safest way to modify config files — you always have a rollback point.
    """
    from tools.files import write_config_file

    _require_repo()

    # checkpoint current state
    _git("add", "-A")
    if _git("status", "--porcelain").stdout.strip():
        _git("commit", "-m", f"checkpoint: before modifying {relative_path}")

    result = write_config_file(relative_path, content)
    if not result.get("success"):
        return result

    # commit the new change
    msg = commit_message or f"chore: update {relative_path}"
    _git("add", "-A")
    if _git("status", "--porcelain").stdout.strip():
        commit_result = _git("commit", "-m", msg)
        if commit_result.returncode == 0:
            result["git_sha"] = _git("rev-parse", "--short", "HEAD").stdout.strip()
            result["git_message"] = msg

    return result
