"""
QAD Environment MCP Server.

Provides tools for managing QAD Adaptive ERP environments via SSH.
Supports configuration viewing/editing, log tailing, yab operations,
and general environment inspection.

Usage:
    qad-env-mcp                              # uses defaults (mfg/qad)
    QAD_SSH_PASSWORD=secret qad-env-mcp      # override password via env var
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import os
import re
import sys

from mcp.server.fastmcp import FastMCP

from .paths import (
    BACKUP_DIR,
    CATALINA_LOG,
    LIB_ABS,
    LIB_DIR,
    MAIN_CONFIG,
    QAD_JAR_PREFIX,
    SYSTEST_ROOT,
    TOMCAT_SERVICES,
    YAB_LOG_DIR,
    resolve_config_path,
    resolve_hostname,
    resolve_log_path,
)
from .registry import EnvironmentRegistry
from .ssh_manager import SSHManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stderr,  # MCP uses stdout for protocol — logs go to stderr
)
logger = logging.getLogger("qad-env-mcp")

# ---------------------------------------------------------------------------
# Server & SSH setup
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "qad-env",
    instructions="""You are connected to QAD Adaptive ERP environments.
Each environment is a Linux VM accessible via SSH at {env_id}.environments.qad.com.
When the user mentions an environment by name or ID, use that as the env_id parameter.
Always confirm destructive operations (restart, update, backup) with the user before executing.

Tool selection guide:
- yab_help: ALWAYS use this for any question about yab itself — version, capabilities,
  available commands, what a command does, or how to use it. Examples:
  query="version" for yab version, query="" for general help, query="update" for
  update command help. This is the ONLY tool for yab version info.
- yab_run: Execute quick yab commands (status, start, stop, restart).
  Do NOT use for version or help — use yab_help instead.
- yab_start / yab_status: For long-running yab operations (update, backup,
  install, restore, deploy). yab_start kicks off the command in the background;
  yab_status polls progress and output.
- read_config / update_config: View or modify QAD property files.
- tail_log: Read recent log entries from Tomcat or other services.
- list_jars: List deployed QAD module JARs (WAR/lib). NOT for yab version.
- run_command: Escape hatch for allowlisted shell commands (df, ps, etc.).
- health_check / service_status / db_status: Diagnostics and monitoring.""",
)

ssh = SSHManager(
    username=os.environ.get("QAD_SSH_USERNAME", "mfg"),
    password=os.environ.get("QAD_SSH_PASSWORD", "qad"),
    port=int(os.environ.get("QAD_SSH_PORT", "22")),
)

registry = EnvironmentRegistry()

# ---------------------------------------------------------------------------
# Read-only commands allowed via run_command
# ---------------------------------------------------------------------------
ALLOWED_COMMANDS = {
    "df", "du", "free", "uptime", "whoami", "hostname", "date",
    "ps", "top", "cat", "head", "tail", "grep", "find", "ls", "wc",
    "java", "systemctl",
}

# yab subcommands that are considered safe (read-only / non-destructive)
YAB_SAFE_COMMANDS = {"status", "info"}

# yab subcommands that require explicit confirmation
YAB_DANGEROUS_COMMANDS = {
    "tomcat-webui-restart", "stop", "start", "update", "install",
    "backup", "restore", "deploy",
}


def _validate_env_id(env_id: str) -> str:
    """Validate and normalize an environment ID, resolving aliases."""
    clean = env_id.replace(".environments.qad.com", "").strip().lower()
    # Try resolving as a registry alias first
    resolved = registry.resolve(clean)
    if resolved:
        return resolved
    if not re.match(r"^[a-z0-9][a-z0-9\-]{2,30}$", clean):
        raise ValueError(
            f"Invalid environment ID: '{env_id}'. "
            "Expected an alphanumeric identifier like 'als2moherp5wcy'."
        )
    return clean


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_config(
    env_id: str,
    config_name: str = "main",
    filter_key: str | None = None,
) -> str:
    """Read configuration properties from a QAD environment.

    Args:
        env_id: Environment identifier (e.g. 'als2moherp5wcy')
        config_name: Config file alias ('main', 'qracore') or a path relative
                     to the systest root. Defaults to the main qracore properties.
        filter_key: Optional substring to filter property keys (e.g. 'ssm' to
                    show only SSM-related properties)
    """
    env_id = _validate_env_id(env_id)
    config_path = resolve_config_path(config_name)
    hostname = resolve_hostname(env_id)

    result = await ssh.run_checked(env_id, f"cat {SYSTEST_ROOT}/{config_path}")

    output = result.stdout
    if filter_key:
        lines = [
            line for line in output.splitlines()
            if filter_key.lower() in line.lower()
        ]
        if not lines:
            return f"No properties matching '{filter_key}' found in {config_path} on {hostname}"
        output = "\n".join(lines)

    return f"# Config: {config_path} on {hostname}\n\n{output}"


@mcp.tool()
async def update_config(
    env_id: str,
    property_key: str,
    new_value: str,
    config_name: str = "main",
) -> str:
    """Update a single property in a QAD configuration file.

    Uses sed to perform an in-place replacement. Only updates existing keys —
    will not add new properties.

    Args:
        env_id: Environment identifier (e.g. 'als2moherp5wcy')
        property_key: The property key to update (e.g. 'qad-qracore.featureFlags.ssm.enabled')
        new_value: The new value to set (e.g. 'true')
        config_name: Config file alias or relative path. Defaults to main config.
    """
    env_id = _validate_env_id(env_id)
    config_path = resolve_config_path(config_name)
    full_path = f"{SYSTEST_ROOT}/{config_path}"
    hostname = resolve_hostname(env_id)

    # Verify the key exists before modifying
    check = await ssh.run(env_id, f"grep -c '^{re.escape(property_key)}=' {full_path}")
    if check.stdout.strip() == "0" or not check.ok:
        return (
            f"Property '{property_key}' not found in {config_path} on {hostname}. "
            "Use get_config to inspect available properties."
        )

    # Read current value for the response
    current = await ssh.run(env_id, f"grep '^{re.escape(property_key)}=' {full_path}")
    current_line = current.stdout.strip()

    # Escape sed special characters in the value
    escaped_value = new_value.replace("/", "\\/").replace("&", "\\&")
    escaped_key = re.escape(property_key).replace("/", "\\/")

    sed_cmd = (
        f"sed -i 's/^{escaped_key}=.*/{escaped_key}={escaped_value}/' {full_path}"
    )
    result = await ssh.run_checked(env_id, sed_cmd)

    # Verify the change
    verify = await ssh.run(env_id, f"grep '^{re.escape(property_key)}=' {full_path}")

    return (
        f"Updated on {hostname}:\n"
        f"  Before: {current_line}\n"
        f"  After:  {verify.stdout.strip()}\n\n"
        f"Note: You may need to restart Tomcat for changes to take effect."
    )


@mcp.tool()
async def get_logs(
    env_id: str,
    lines: int = 50,
    log_name: str = "catalina",
    grep_pattern: str | None = None,
) -> str:
    """Read recent log entries from a QAD environment.

    Args:
        env_id: Environment identifier (e.g. 'als2moherp5wcy')
        lines: Number of lines to retrieve (default 50, max 500)
        log_name: Log file alias ('catalina') or a path relative to systest root
        grep_pattern: Optional pattern to filter log lines (e.g. 'ERROR', 'OutOfMemory')
    """
    env_id = _validate_env_id(env_id)
    log_path = resolve_log_path(log_name)
    hostname = resolve_hostname(env_id)
    lines = min(max(lines, 1), 500)

    if grep_pattern:
        # Get more lines then filter, so we're likely to get enough matches
        cmd = f"tail -n {lines * 5} {SYSTEST_ROOT}/{log_path} | grep -i '{grep_pattern}' | tail -n {lines}"
    else:
        cmd = f"tail -n {lines} {SYSTEST_ROOT}/{log_path}"

    result = await ssh.run(env_id, cmd)

    if not result.ok:
        return f"Failed to read logs from {hostname}: {result.stderr}"

    if not result.stdout.strip():
        qualifier = f" matching '{grep_pattern}'" if grep_pattern else ""
        return f"No log entries{qualifier} found in {log_path} on {hostname}"

    header = f"# Last {lines} lines from {log_path} on {hostname}"
    if grep_pattern:
        header += f" (filtered: '{grep_pattern}')"
    return f"{header}\n\n{result.stdout}"


@mcp.tool()
async def yab_run(
    env_id: str,
    command: str,
    options: str = "",
) -> str:
    """Execute a yab (Your Application Builder) command on a QAD environment.

    Syntax: yab [options] COMMAND
    Options are placed BEFORE the command.

    Do NOT use this tool for version or help queries — use yab_help() instead.

    Common commands:
        status                - Show environment status
        tomcat-webui-restart  - Restart Tomcat (requires confirmation)
        stop                  - Stop Tomcat (requires confirmation)
        start                 - Start Tomcat (requires confirmation)
        update                - Update the environment (use yab_start for this)
        backup                - Backup the database (use yab_start for this)

    Useful options (placed BEFORE the command):
        -v                    - Verbose: write log messages to console
        -clean                - Force certain updates
        -log-copy             - Record all log messages to a file
        -log-level:LEVEL      - Set logging threshold (TRACE|DEBUG|INFO|WARN|ERROR|OFF)

    Args:
        env_id: Environment identifier (e.g. 'als2moherp5wcy')
        command: The yab command to execute (e.g. 'status', 'stop', 'start')
        options: Yab options placed before the command (e.g. '-v', '-clean', '-v -log-copy')
    """
    env_id = _validate_env_id(env_id)
    hostname = resolve_hostname(env_id)
    command = command.strip().lower()

    # Block obviously dangerous patterns
    for val, label in [(command, "command"), (options, "options")]:
        if any(c in val for c in [";", "|", "&", "`", "$", "(", ")"]):
            return f"Error: Invalid characters in {label}."

    # Warn about destructive commands (the LLM should confirm with user)
    warning = ""
    if command in YAB_DANGEROUS_COMMANDS:
        warning = (
            f"⚠️  '{command}' is a potentially destructive operation on {hostname}.\n"
            f"Proceeding as instructed.\n\n"
        )

    # yab syntax: yab [options] COMMAND
    cmd = f"yab {options} {command}".strip() if options else f"yab {command}"

    # yab commands can be slow (especially update/backup)
    timeout = 300.0 if command in {"update", "backup", "restore", "deploy"} else 120.0

    result = await ssh.run(env_id, cmd, timeout=timeout)

    output = result.stdout
    if result.stderr:
        output += f"\n--- stderr ---\n{result.stderr}"

    status = "✓" if result.ok else "✗"
    return f"{warning}{status} {cmd} on {hostname} (exit {result.exit_code}):\n\n{output}"


# ---------------------------------------------------------------------------
# Long-running yab operations (background execution)
# ---------------------------------------------------------------------------

YAB_LOG_PREFIX = "/tmp/yab_job"


@mcp.tool()
async def yab_start(
    env_id: str,
    command: str,
    options: str = "-v",
) -> str:
    """Start a long-running yab command in the background (update, backup, install, etc.).

    The command runs via nohup so it survives disconnection. Use yab_status()
    to monitor progress and retrieve output.

    Syntax: yab [options] COMMAND
    Options are placed BEFORE the command.

    Useful options:
        -v                    - Verbose: write log messages to console (default, captured to log file)
        -clean                - Force certain updates
        -log-level:LEVEL      - Set logging threshold (TRACE|DEBUG|INFO|WARN|ERROR|OFF)

    Args:
        env_id: Environment identifier (e.g. 'als2moherp5wcy')
        command: The yab command to execute (e.g. 'update', 'backup')
        options: Yab options placed before the command (default: '-v' for verbose output)
    """
    env_id = _validate_env_id(env_id)
    hostname = resolve_hostname(env_id)
    command = command.strip().lower()

    for val, label in [(command, "command"), (options, "options")]:
        if any(c in val for c in [";", "|", "&", "`", "$", "(", ")"]):
            return f"Error: Invalid characters in {label}."

    # yab syntax: yab [options] COMMAND
    yab_cmd = f"yab {options} {command}".strip() if options else f"yab {command}"

    # Use a log/pid file keyed by host and command
    log_file = f"{YAB_LOG_PREFIX}_{hostname}_{command}.log"
    pid_file = f"{YAB_LOG_PREFIX}_{hostname}_{command}.pid"

    # Clean up any previous log, start command in background.
    # nohup runs in a subshell, so we must cd to SYSTEST_ROOT explicitly
    # (the cd prepended by ssh.run applies to the outer shell only).
    launch_cmd = (
        f"rm -f {log_file} {pid_file} && "
        f"nohup bash -lc 'cd {SYSTEST_ROOT} && {yab_cmd}' > {log_file} 2>&1 & "
        f"echo $! | tee {pid_file}"
    )

    result = await ssh.run(env_id, launch_cmd, timeout=15.0)
    if not result.ok:
        return f"Failed to start yab {command} on {hostname}: {result.stderr}"

    pid = result.stdout.strip()
    return (
        f"Started `{yab_cmd}` on {hostname} (PID {pid}).\n"
        f"Log file: {log_file}\n\n"
        f"Use yab_status(env_id=\"{env_id}\", command=\"{command}\") to check progress."
    )


@mcp.tool()
async def yab_status(
    env_id: str,
    command: str,
    tail_lines: int = 50,
) -> str:
    """Check the status of a background yab command started with yab_start().

    Returns whether the process is still running and the latest output.

    Args:
        env_id: Environment identifier (e.g. 'als2moherp5wcy')
        command: The yab command that was started (e.g. 'update', 'backup')
        tail_lines: Number of output lines to return from the end (default 50)
    """
    env_id = _validate_env_id(env_id)
    hostname = resolve_hostname(env_id)
    command = command.strip().lower()

    log_file = f"{YAB_LOG_PREFIX}_{hostname}_{command}.log"
    pid_file = f"{YAB_LOG_PREFIX}_{hostname}_{command}.pid"

    # Read PID and check if process is alive
    pid_result = await ssh.run(env_id, f"cat {pid_file} 2>/dev/null", timeout=10.0)
    pid = pid_result.stdout.strip()

    if not pid:
        return f"No background yab {command} job found on {hostname}. Start one with yab_start()."

    # Check if still running
    alive_result = await ssh.run(env_id, f"kill -0 {pid} 2>/dev/null && echo RUNNING || echo DONE", timeout=10.0)
    running = alive_result.stdout.strip() == "RUNNING"

    # Get exit code if finished
    exit_info = ""
    if not running:
        wait_result = await ssh.run(env_id, f"wait {pid} 2>/dev/null; echo $?", timeout=10.0)
        code = wait_result.stdout.strip()
        exit_info = f"Exit code: {code}\n"

    # Tail the log
    log_result = await ssh.run(
        env_id, f"tail -n {tail_lines} {log_file} 2>/dev/null || echo '(log file not found)'", timeout=10.0
    )

    status = "⏳ RUNNING" if running else "✅ COMPLETED"
    return (
        f"# yab {command} on {hostname}\n\n"
        f"Status: {status} (PID {pid})\n"
        f"{exit_info}\n"
        f"## Output (last {tail_lines} lines)\n\n{log_result.stdout}"
    )


@mcp.tool()
async def yab_help(
    env_id: str,
    query: str = "",
) -> str:
    """Get yab version, capabilities, available commands, or help on a specific command.

    Use this tool to answer ANY question about yab itself: version, what commands
    are available, how a specific command works, what options it supports, etc.

    Examples:
        yab_help(env_id, "")              - Show yab help and list of options
        yab_help(env_id, "version")       - Show yab version
        yab_help(env_id, "extended")      - Show extended help with additional options
        yab_help(env_id, "update")        - Show help for the 'update' command
        yab_help(env_id, "backup")        - Show help for the 'backup' command

    Args:
        env_id: Environment identifier (e.g. 'als2moherp5wcy')
        query: What to look up — 'version', 'extended', or a command name (empty = general help)
    """
    env_id = _validate_env_id(env_id)
    hostname = resolve_hostname(env_id)

    if query:
        query = query.strip().lower()
        if any(c in query for c in [";", "|", "&", "`", "$", "(", ")"]):
            return "Error: Invalid characters in query."

    if query == "version":
        # Running yab with no arguments prints the version banner
        result = await ssh.run(env_id, "yab", timeout=30.0)
        label = "yab version"
    elif query == "":
        # General help with options and usage
        result = await ssh.run(env_id, "yab -?", timeout=30.0)
        label = "yab help"
    elif query == "extended":
        result = await ssh.run(env_id, "yab -??", timeout=30.0)
        label = "yab extended help"
    else:
        # Help for a specific command
        result = await ssh.run(env_id, f"yab {query} -?", timeout=30.0)
        label = f"yab {query} help"

    output = result.stdout
    if result.stderr:
        output += f"\n--- stderr ---\n{result.stderr}"

    return f"# {label} on {hostname}\n\n{output}"


@mcp.tool()
async def run_command(
    env_id: str,
    command: str,
) -> str:
    """Execute an allowlisted shell command on a QAD environment.

    This is a read-only escape hatch for commands not covered by other tools.
    Only specific commands are allowed: df, du, free, uptime, whoami, hostname,
    date, ps, top, cat, head, tail, grep, find, ls, wc, java, systemctl.

    Args:
        env_id: Environment identifier (e.g. 'als2moherp5wcy')
        command: Shell command to execute (must start with an allowed command)
    """
    env_id = _validate_env_id(env_id)
    hostname = resolve_hostname(env_id)

    # Extract the base command (first word)
    parts = command.strip().split()
    if not parts:
        return "Error: Empty command."

    base_cmd = parts[0].split("/")[-1]  # handle /usr/bin/df etc.
    if base_cmd not in ALLOWED_COMMANDS:
        return (
            f"Command '{base_cmd}' is not in the allowlist.\n"
            f"Allowed commands: {', '.join(sorted(ALLOWED_COMMANDS))}"
        )

    # Block shell injection patterns
    if any(c in command for c in [";", "&&", "||", "`", "$(", ">"]):
        return "Error: Shell operators are not allowed. Use a single command."

    result = await ssh.run(env_id, command)

    output = result.stdout
    if result.stderr:
        output += f"\n--- stderr ---\n{result.stderr}"

    status = "✓" if result.ok else "✗"
    return f"{status} {command} on {hostname} (exit {result.exit_code}):\n\n{output}"


@mcp.tool()
async def get_version(
    env_id: str,
    module: str | None = None,
) -> str:
    """Get installed software versions from a QAD environment.

    Detects versions by inspecting JAR filenames in the deployed WAR's
    WEB-INF/lib/ directory.  The naming convention is:
        {module-name}-webui-{version}.jar
    For example: qad-webshell-webui-2.39.0.187-SNAPSHOT.jar -> version 2.39.0.187-SNAPSHOT

    Args:
        env_id: Environment identifier (e.g. 'als2moherp5wcy')
        module: Optional module name filter (e.g. 'webshell', 'qracore').
                When omitted, returns all detected module versions.
    """
    env_id = _validate_env_id(env_id)
    hostname = resolve_hostname(env_id)

    cmd = f"ls {LIB_ABS}/"
    result = await ssh.run(env_id, cmd)

    if not result.ok:
        return f"Failed to list JARs on {hostname}: {result.stderr}"

    # Parse QAD application JARs (qad-*) — extract module name and version
    pattern = re.compile(r"^(.+)-webui-(.+)\.jar$")
    versions: list[tuple[str, str]] = []
    for filename in result.stdout.splitlines():
        name = filename.strip()
        if not name.startswith(QAD_JAR_PREFIX):
            continue
        m = pattern.match(name)
        if m:
            versions.append((m.group(1), m.group(2)))
        elif name.endswith(".jar"):
            # QAD jar without -webui- convention — show full filename
            versions.append((name.removesuffix(".jar"), ""))

    if module:
        module_lower = module.lower()
        versions = [
            (name, ver) for name, ver in versions
            if module_lower in name.lower()
        ]

    if not versions:
        qualifier = f" matching '{module}'" if module else ""
        return f"No module versions{qualifier} found on {hostname}"

    versions.sort(key=lambda x: x[0])
    lines = [f"  {name}: {ver}" if ver else f"  {name}" for name, ver in versions]
    header = f"Installed versions on {hostname}"
    if module:
        header += f" (filter: '{module}')"
    return f"{header}:\n\n" + "\n".join(lines)


@mcp.tool()
async def check_connectivity(
    env_id: str,
) -> str:
    """Test SSH connectivity to a QAD environment.

    Useful for verifying VPN is connected and the environment is reachable.

    Args:
        env_id: Environment identifier (e.g. 'als2moherp5wcy')
    """
    env_id = _validate_env_id(env_id)
    hostname = resolve_hostname(env_id)

    try:
        result = await ssh.run(env_id, "echo ok && hostname && uptime")
        if result.ok:
            return f"✓ Connected to {hostname}\n\n{result.stdout}"
        else:
            return f"✗ Connection issue with {hostname}: {result.stderr}"
    except Exception as e:
        return (
            f"✗ Cannot reach {hostname}: {e}\n\n"
            "Check that:\n"
            "  1. You are connected to the corporate VPN\n"
            "  2. The environment ID is correct\n"
            "  3. The environment is running"
        )


# ---------------------------------------------------------------------------
# Registry tools
# ---------------------------------------------------------------------------


def _format_entry(entry) -> str:
    """Format a registry entry for display."""
    lines = [f"  env_id: {entry.env_id}"]
    if entry.aliases:
        lines.append(f"  aliases: {', '.join(entry.aliases)}")
    if entry.description:
        lines.append(f"  description: {entry.description}")
    if entry.tags:
        lines.append(f"  tags: {', '.join(entry.tags)}")
    if entry.owner:
        lines.append(f"  owner: {entry.owner}")
    return "\n".join(lines)


@mcp.tool()
async def register_environment(
    env_id: str,
    alias: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    owner: str | None = None,
) -> str:
    """Register a new QAD environment or update an existing registration.

    Verifies SSH connectivity before registering. If the env_id is already
    registered, merges new aliases and tags with existing ones.

    Args:
        env_id: The actual environment ID (e.g. 'als2moherp5wcy')
        alias: Optional friendly name (e.g. 'staging-abc')
        description: Optional human-readable description
        tags: Optional tags for searching (e.g. ['staging', 'customer-abc'])
        owner: Optional owner name
    """
    clean_id = env_id.replace(".environments.qad.com", "").strip().lower()
    if not re.match(r"^[a-z0-9][a-z0-9\-]{2,30}$", clean_id):
        return f"Invalid environment ID: '{env_id}'."

    # Verify connectivity
    hostname = resolve_hostname(clean_id)
    try:
        result = await ssh.run(clean_id, "echo ok", timeout=15.0)
        if not result.ok:
            return f"Cannot reach {hostname} -- registration aborted."
    except Exception as e:
        return f"Cannot reach {hostname}: {e} -- registration aborted."

    entry = registry.add(
        env_id=clean_id,
        alias=alias,
        description=description,
        tags=tags,
        owner=owner,
    )
    return f"Registered {hostname}:\n{_format_entry(entry)}"


@mcp.tool()
async def unregister_environment(name: str) -> str:
    """Remove an environment from the local registry.

    Args:
        name: Environment ID or alias to remove
    """
    removed = registry.remove(name)
    if removed is None:
        return f"No registered environment matching '{name}'."
    return f"Unregistered {removed.env_id} (aliases: {', '.join(removed.aliases) or 'none'})"


@mcp.tool()
async def add_alias(env_id: str, alias: str) -> str:
    """Add a friendly alias to an already registered environment.

    Args:
        env_id: Existing environment ID or alias
        alias: New alias to add
    """
    entry = registry.add_alias(env_id, alias)
    if entry is None:
        return f"No registered environment matching '{env_id}'. Register it first."
    return f"Added alias '{alias}' to {entry.env_id}:\n{_format_entry(entry)}"


@mcp.tool()
async def list_environments(tag: str | None = None) -> str:
    """List all registered QAD environments, optionally filtered by tag.

    Args:
        tag: Optional tag to filter by (e.g. 'staging')
    """
    entries = registry.list_all()
    if tag:
        tag_lower = tag.lower()
        entries = [e for e in entries if tag_lower in (t.lower() for t in e.tags)]

    if not entries:
        qualifier = f" with tag '{tag}'" if tag else ""
        return f"No registered environments{qualifier}."

    blocks = [_format_entry(e) for e in entries]
    header = f"Registered environments ({len(entries)})"
    if tag:
        header += f" [tag: {tag}]"
    return f"{header}:\n\n" + "\n\n".join(blocks)


@mcp.tool()
async def search_environments(query: str) -> str:
    """Search registered environments by keyword.

    Matches against env_id, aliases, tags, description, and owner.

    Args:
        query: Search term (e.g. 'staging', 'maxim', 'customer-abc')
    """
    results = registry.search(query)
    if not results:
        return f"No environments matching '{query}'."
    blocks = [_format_entry(e) for e in results]
    return f"Found {len(results)} environment(s) matching '{query}':\n\n" + "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Tool 1: health_check
# ---------------------------------------------------------------------------


@mcp.tool()
async def health_check(env_id: str) -> str:
    """Composite health check for a QAD environment.

    Checks disk usage, memory/swap, all Tomcat processes, database processes,
    and recent OOM-killer activity in a single call. Returns a traffic-light
    summary (OK / WARN / CRIT) per subsystem.

    Args:
        env_id: Environment identifier (e.g. 'als2moherp5wcy')
    """
    env_id = _validate_env_id(env_id)
    hostname = resolve_hostname(env_id)

    checks = {
        "disk":     "df -h /dr01 2>/dev/null || df -h / 2>/dev/null",
        "memory":   "free -h",
        "tomcats":  "ps -eo pid,rss,comm,args --no-headers | grep -E 'java|tomcat' | grep -v grep || true",
        "databases":"ps -eo pid,rss,comm,args --no-headers | grep '_mprosrv\\|_broker\\|proadsv' | grep -v grep || true",
        "oom":      "dmesg 2>/dev/null | grep -iE 'oom.killer|killed process|out of memory' | tail -5 || echo '(no dmesg access)'",
        "uptime":   "uptime",
    }

    results = await asyncio.gather(
        *[ssh.run(env_id, cmd) for cmd in checks.values()],
        return_exceptions=True,
    )

    sections: list[str] = [f"# Health Check: {hostname}\n"]

    for (label, _), result in zip(checks.items(), results):
        if isinstance(result, Exception):
            sections.append(f"## {label.upper()}: ERROR\n{result}\n")
            continue

        out = result.stdout.strip()

        # Simple heuristics for traffic-light status
        status = "OK"
        if label == "disk":
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[4].endswith("%"):
                    pct = int(parts[4].rstrip("%"))
                    if pct >= 90:
                        status = "CRIT"
                    elif pct >= 75:
                        status = "WARN"
        elif label == "memory":
            for line in out.splitlines():
                if line.startswith("Swap:"):
                    parts = line.split()
                    if len(parts) >= 3 and parts[1] not in ("0B", "0"):
                        status = "WARN"
        elif label == "oom":
            if out and "(no dmesg access)" not in out:
                status = "WARN"
        elif label in ("tomcats", "databases"):
            if not out:
                status = "WARN"

        sections.append(f"## {label.upper()}: {status}\n{out or '(no output)'}\n")

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# Tool 2: compare_configs
# ---------------------------------------------------------------------------


@mcp.tool()
async def compare_configs(
    env_id_1: str,
    env_id_2: str,
    config_name: str = "main",
) -> str:
    """Diff configuration files between two QAD environments.

    Fetches the same config file from both environments and produces a unified
    diff. Useful for catching configuration drift after a yab update or
    diagnosing 'works on my env' issues.

    Args:
        env_id_1: First environment ID (e.g. 'als2moherp5wcy')
        env_id_2: Second environment ID (e.g. 'xyz789')
        config_name: Config file alias ('main', 'qracore') or relative path.
                     Defaults to the main qracore properties.
    """
    env_id_1 = _validate_env_id(env_id_1)
    env_id_2 = _validate_env_id(env_id_2)
    config_path = resolve_config_path(config_name)

    r1, r2 = await asyncio.gather(
        ssh.run(env_id_1, f"cat {SYSTEST_ROOT}/{config_path}"),
        ssh.run(env_id_2, f"cat {SYSTEST_ROOT}/{config_path}"),
    )

    h1 = resolve_hostname(env_id_1)
    h2 = resolve_hostname(env_id_2)

    if not r1.ok:
        return f"Failed to read config from {h1}: {r1.stderr}"
    if not r2.ok:
        return f"Failed to read config from {h2}: {r2.stderr}"

    lines1 = r1.stdout.splitlines(keepends=True)
    lines2 = r2.stdout.splitlines(keepends=True)

    diff = list(difflib.unified_diff(
        lines1, lines2,
        fromfile=f"{h1}/{config_path}",
        tofile=f"{h2}/{config_path}",
        lineterm="",
    ))

    if not diff:
        return f"No differences found in {config_path} between {h1} and {h2}."

    return f"# Config diff ({config_path})\n\n" + "\n".join(diff)


# ---------------------------------------------------------------------------
# Tool 3: disk_cleanup
# ---------------------------------------------------------------------------


@mcp.tool()
async def disk_cleanup(env_id: str) -> str:
    """Identify disk usage hotspots on a QAD environment.

    Reports sizes of known bloat locations: log files, yab logs, database
    backups, and temp directories. Does NOT delete anything — use yab
    filereduce commands to clean up after reviewing.

    Args:
        env_id: Environment identifier (e.g. 'als2moherp5wcy')
    """
    env_id = _validate_env_id(env_id)
    hostname = resolve_hostname(env_id)

    checks: list[tuple[str, str]] = [
        ("Overall disk (/dr01)",   f"df -h /dr01 2>/dev/null || df -h /"),
        ("Systest root total",     f"du -sh {SYSTEST_ROOT} 2>/dev/null"),
        ("Log files (all Tomcats)",
            f"find {SYSTEST_ROOT}/servers -name 'catalina.out' -exec du -sh {{}} \\; 2>/dev/null"),
        ("YAB logs",               f"du -sh {SYSTEST_ROOT}/{YAB_LOG_DIR} 2>/dev/null || echo '(not found)'"),
        ("Database backups",       f"du -sh {SYSTEST_ROOT}/{BACKUP_DIR} 2>/dev/null || echo '(not found)'"),
        ("WAR backup/temp dirs",
            f"find {SYSTEST_ROOT}/servers -maxdepth 3 -name '*.war.bak' -o -name 'work' -type d 2>/dev/null | "
            f"xargs du -sh 2>/dev/null | sort -rh | head -10 || echo '(none found)'"),
        ("/tmp usage",             "du -sh /tmp 2>/dev/null"),
        ("Maven cache",            "du -sh ~/.m2/repository 2>/dev/null || echo '(not found)'"),
        ("Largest files (top 10)",
            f"find {SYSTEST_ROOT} -type f -size +100M -exec du -sh {{}} \\; 2>/dev/null | sort -rh | head -10 || echo '(none >100M)'"),
    ]

    results = await asyncio.gather(
        *[ssh.run(env_id, cmd) for _, cmd in checks],
        return_exceptions=True,
    )

    sections = [f"# Disk Usage Report: {hostname}\n"]
    for (label, _), result in zip(checks, results):
        if isinstance(result, Exception):
            sections.append(f"## {label}\nERROR: {result}\n")
        else:
            out = result.stdout.strip() or "(no output)"
            sections.append(f"## {label}\n{out}\n")

    sections.append(
        "---\nTo reclaim space: `yab filereduce-yab-log-archive-update` "
        "or truncate old catalina.out logs manually."
    )
    return "\n".join(sections)


# ---------------------------------------------------------------------------
# Tool 4: database_status
# ---------------------------------------------------------------------------


@mcp.tool()
async def database_status(env_id: str) -> str:
    """Check Progress OpenEdge database health on a QAD environment.

    Reports running database broker/server processes, connection counts,
    and helps identify the root cause of Hikari pool timeout failures or
    issues caused by misuse of discon.sh.

    Args:
        env_id: Environment identifier (e.g. 'als2moherp5wcy')
    """
    env_id = _validate_env_id(env_id)
    hostname = resolve_hostname(env_id)

    checks: list[tuple[str, str]] = [
        ("Database broker processes (_broker)",
            "ps -eo pid,rss,stat,args --no-headers | grep '_broker' | grep -v grep || echo '(none running)'"),
        ("Database server processes (_mprosrv)",
            "ps -eo pid,rss,stat,args --no-headers | grep '_mprosrv' | grep -v grep | wc -l"),
        ("AppServer broker (proadsv)",
            "ps -eo pid,rss,stat,args --no-headers | grep 'proadsv' | grep -v grep || echo '(none running)'"),
        ("PAS agent processes",
            "ps -eo pid,rss,stat,args --no-headers | grep 'pasoe\\|_progres' | grep -v grep || echo '(none running)'"),
        ("Java (Tomcat) processes",
            "ps -eo pid,rss,args --no-headers | grep 'java' | grep -v grep | awk '{printf \"%s  RSS=%sMB  %s\\n\", $1, int($2/1024), substr($0, index($0,$3))}' || echo '(none running)'"),
        ("Active DB connections (via promon if available)",
            "which promon >/dev/null 2>&1 && echo 'promon available' || "
            "ss -tn 2>/dev/null | grep ':20931\\|:20932\\|:20933\\|:20934' | wc -l || echo '(cannot check via ss)'"),
        ("Open files / socket count",
            "ls /proc/$(pgrep -f _broker | head -1)/fd 2>/dev/null | wc -l || echo '(broker PID not found)'"),
    ]

    results = await asyncio.gather(
        *[ssh.run(env_id, cmd) for _, cmd in checks],
        return_exceptions=True,
    )

    sections = [f"# Database Status: {hostname}\n"]
    for (label, _), result in zip(checks, results):
        if isinstance(result, Exception):
            sections.append(f"## {label}\nERROR: {result}\n")
        else:
            out = result.stdout.strip() or "(no output)"
            sections.append(f"## {label}\n{out}\n")

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# Tool 5: thread_dump
# ---------------------------------------------------------------------------


@mcp.tool()
async def thread_dump(
    env_id: str,
    service: str = "tomcat-webui",
) -> str:
    """Capture a JVM thread dump from a Tomcat service.

    Sends SIGQUIT (kill -3) to the Tomcat JVM, which prints a full thread
    dump to catalina.out without stopping the process. Useful for diagnosing
    hung requests, thread pool exhaustion, and deadlocks.

    Args:
        env_id: Environment identifier (e.g. 'als2moherp5wcy')
        service: Tomcat service name (default: 'tomcat-webui').
                 Options: tomcat-webui, tomcat-qxtend, tomcat-eventservice, tomcat-default
    """
    env_id = _validate_env_id(env_id)
    hostname = resolve_hostname(env_id)

    service = service.strip().lower()
    if service not in TOMCAT_SERVICES:
        valid = ", ".join(TOMCAT_SERVICES)
        return f"Unknown service '{service}'. Valid options: {valid}"

    svc = TOMCAT_SERVICES[service]
    log_path = f"{SYSTEST_ROOT}/{svc['log']}"
    grep_str = svc["grep"]

    # Find the JVM PID for this service
    pid_result = await ssh.run(
        env_id,
        f"ps -eo pid,args --no-headers | grep 'java' | grep '{grep_str}' | grep -v grep | awk '{{print $1}}' | head -1",
        cwd="/",
    )
    pid = pid_result.stdout.strip()
    if not pid or not pid.isdigit():
        return (
            f"No running JVM found for '{service}' on {hostname}.\n"
            f"Check with: ps aux | grep java | grep {grep_str}"
        )

    # Record current log file size so we can tail only the new output
    size_result = await ssh.run(env_id, f"wc -c < {log_path}", cwd="/")
    offset = size_result.stdout.strip() if size_result.ok else "0"

    # Send SIGQUIT to trigger thread dump
    kill_result = await ssh.run(env_id, f"kill -3 {pid}", cwd="/")
    if not kill_result.ok:
        return f"Failed to send SIGQUIT to PID {pid} on {hostname}: {kill_result.stderr}"

    # Give the JVM a moment to write the dump
    await asyncio.sleep(2)

    # Read only the new content written after the signal
    dump_result = await ssh.run(
        env_id,
        f"tail -c +{offset} {log_path} | head -500",
        cwd="/",
    )

    if not dump_result.stdout.strip():
        return (
            f"Sent SIGQUIT to PID {pid} ({service}) on {hostname}, "
            f"but no thread dump output found in {svc['log']}.\n"
            "The JVM may not have written to the expected log location."
        )

    return (
        f"# Thread Dump: {service} (PID {pid}) on {hostname}\n\n"
        + dump_result.stdout
    )


# ---------------------------------------------------------------------------
# Tool 6: compare_versions
# ---------------------------------------------------------------------------


@mcp.tool()
async def compare_versions(
    env_ids: list[str],
    module: str | None = None,
) -> str:
    """Compare installed module versions across multiple QAD environments.

    Produces a matrix showing where versions agree or diverge. Useful for
    verifying yab update deployments and diagnosing cross-environment
    test failures caused by version mismatches.

    Args:
        env_ids: List of environment IDs to compare (2 or more)
        module: Optional module name filter (e.g. 'webshell', 'qracore')
    """
    if len(env_ids) < 2:
        return "Provide at least 2 environment IDs to compare."

    validated = [_validate_env_id(e) for e in env_ids]
    hostnames = [resolve_hostname(e) for e in validated]

    pattern = re.compile(r"^(.+)-webui-(.+)\.jar$")

    async def get_versions(env_id: str) -> dict[str, str]:
        result = await ssh.run(env_id, f"ls {LIB_ABS}/")
        versions: dict[str, str] = {}
        if not result.ok:
            return versions
        for filename in result.stdout.splitlines():
            name = filename.strip()
            if not name.startswith(QAD_JAR_PREFIX):
                continue
            m = pattern.match(name)
            if m:
                versions[m.group(1)] = m.group(2)
        return versions

    all_versions = await asyncio.gather(*[get_versions(e) for e in validated])

    # Build union of all module names
    all_modules: set[str] = set()
    for vmap in all_versions:
        all_modules.update(vmap.keys())

    if module:
        module_lower = module.lower()
        all_modules = {m for m in all_modules if module_lower in m.lower()}

    if not all_modules:
        qualifier = f" matching '{module}'" if module else ""
        return f"No modules{qualifier} found across the given environments."

    sorted_modules = sorted(all_modules)
    col_w = max(len(h) for h in hostnames) + 2

    # Header
    label_w = max(len(m) for m in sorted_modules) + 2
    header = f"{'Module':<{label_w}}" + "".join(f"{h:<{col_w}}" for h in hostnames)
    separator = "-" * len(header)
    rows = [header, separator]

    mismatches = 0
    for mod in sorted_modules:
        vers = [vmap.get(mod, "(missing)") for vmap in all_versions]
        marker = "  *** MISMATCH" if len(set(vers)) > 1 else ""
        if marker:
            mismatches += 1
        row = f"{mod:<{label_w}}" + "".join(f"{v:<{col_w}}" for v in vers) + marker
        rows.append(row)

    summary = f"\n{mismatches} module(s) with version mismatches." if mismatches else "\nAll modules match."
    qualifier = f" (filter: '{module}')" if module else ""
    return f"# Version Comparison{qualifier}\n\n```\n" + "\n".join(rows) + "\n```" + summary


# ---------------------------------------------------------------------------
# Tool 7: tail_live_errors
# ---------------------------------------------------------------------------


@mcp.tool()
async def tail_live_errors(
    env_id: str,
    lines: int = 50,
) -> str:
    """Search for recent errors across all Tomcat log files on a QAD environment.

    Greps for ERROR, FATAL, Exception, and OutOfMemory across catalina.out
    for all Tomcat services (webui, qxtend, eventservice, default) in a
    single call. Returns a unified, timestamped error feed.

    Args:
        env_id: Environment identifier (e.g. 'als2moherp5wcy')
        lines: Number of matching lines to return per log (default 50, max 200)
    """
    env_id = _validate_env_id(env_id)
    hostname = resolve_hostname(env_id)
    lines = min(max(lines, 1), 200)

    error_pattern = "ERROR\\|FATAL\\|Exception\\|OutOfMemory\\|SEVERE"

    async def grep_log(service: str, log_rel: str) -> tuple[str, str]:
        log_path = f"{SYSTEST_ROOT}/{log_rel}"
        result = await ssh.run(
            env_id,
            f"tail -n {lines * 10} {log_path} 2>/dev/null | grep -E '{error_pattern}' | tail -n {lines}",
        )
        return service, result.stdout.strip()

    tasks = [grep_log(svc, info["log"]) for svc, info in TOMCAT_SERVICES.items()]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    sections = [f"# Recent Errors on {hostname}\n"]
    total_errors = 0
    for item in results:
        if isinstance(item, Exception):
            sections.append(f"ERROR gathering logs: {item}\n")
            continue
        service, output = item
        if output:
            count = len(output.splitlines())
            total_errors += count
            sections.append(f"## {service} ({count} lines)\n{output}\n")
        else:
            sections.append(f"## {service}\n(no errors found)\n")

    if total_errors == 0:
        sections.append("No errors found across any log files.")

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# Tool 8: service_status
# ---------------------------------------------------------------------------


@mcp.tool()
async def service_status(env_id: str) -> str:
    """Detailed status of each individual service on a QAD environment.

    Goes beyond 'yab status' by checking each service's PID, memory usage,
    port bindings, and uptime. Covers all Tomcats, Elasticsearch, Progress
    databases, and PAS appserver. Also detects if DR-only services are
    accidentally running.

    Args:
        env_id: Environment identifier (e.g. 'als2moherp5wcy')
    """
    env_id = _validate_env_id(env_id)
    hostname = resolve_hostname(env_id)

    checks: list[tuple[str, str]] = [
        # Each Tomcat service
        ("tomcat-webui",
            "ps -eo pid,rss,etimes,args --no-headers | grep 'java' | grep 'tomcat-webui' | grep -v grep | "
            "awk '{printf \"PID=%-8s RSS=%sMB  uptime=%ss\\n\", $1, int($2/1024), $3}' || echo 'NOT RUNNING'"),
        ("tomcat-qxtend",
            "ps -eo pid,rss,etimes,args --no-headers | grep 'java' | grep 'tomcat-qxtend' | grep -v grep | "
            "awk '{printf \"PID=%-8s RSS=%sMB  uptime=%ss\\n\", $1, int($2/1024), $3}' || echo 'NOT RUNNING'"),
        ("tomcat-eventservice",
            "ps -eo pid,rss,etimes,args --no-headers | grep 'java' | grep 'tomcat-eventservice' | grep -v grep | "
            "awk '{printf \"PID=%-8s RSS=%sMB  uptime=%ss\\n\", $1, int($2/1024), $3}' || echo 'NOT RUNNING'"),
        ("tomcat-default",
            "ps -eo pid,rss,etimes,args --no-headers | grep 'java' | grep 'tomcat-default' | grep -v grep | "
            "awk '{printf \"PID=%-8s RSS=%sMB  uptime=%ss\\n\", $1, int($2/1024), $3}' || echo 'NOT RUNNING'"),
        # Elasticsearch
        ("elasticsearch",
            "ps -eo pid,rss,etimes,args --no-headers | grep 'elasticsearch' | grep -v grep | "
            "awk '{printf \"PID=%-8s RSS=%sMB  uptime=%ss\\n\", $1, int($2/1024), $3}' || echo 'NOT RUNNING'"),
        # Progress databases
        ("progress-databases (_broker)",
            "ps -eo pid,rss,args --no-headers | grep '_broker' | grep -v grep | "
            "awk '{printf \"PID=%-8s RSS=%sMB  %s\\n\", $1, int($2/1024), $3}' || echo 'NOT RUNNING'"),
        # PAS AppServer
        ("pas-appserver",
            "ps -eo pid,rss,etimes,args --no-headers | grep 'proadsv\\|pasoe' | grep -v grep | "
            "awk '{printf \"PID=%-8s RSS=%sMB  uptime=%ss\\n\", $1, int($2/1024), $3}' || echo 'NOT RUNNING'"),
        # Port listeners
        ("listening ports (8080/9200/20931)",
            "ss -tlnp 2>/dev/null | grep -E ':8080|:8180|:9200|:20931|:3090' || echo '(none of the expected ports open)'"),
        # System resources
        ("system memory",
            "free -h | grep -E 'Mem:|Swap:'"),
    ]

    results = await asyncio.gather(
        *[ssh.run(env_id, cmd) for _, cmd in checks],
        return_exceptions=True,
    )

    sections = [f"# Service Status: {hostname}\n"]
    for (label, _), result in zip(checks, results):
        if isinstance(result, Exception):
            sections.append(f"## {label}\nERROR: {result}\n")
        else:
            out = result.stdout.strip() or "(no output)"
            sections.append(f"## {label}\n{out}\n")

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# Tool 9: backup_info
# ---------------------------------------------------------------------------


@mcp.tool()
async def backup_info(env_id: str) -> str:
    """List available database backups on a QAD environment.

    Shows backup files with timestamps and sizes so you know what restore
    points exist and how recent they are before running a database restore.

    Args:
        env_id: Environment identifier (e.g. 'als2moherp5wcy')
    """
    env_id = _validate_env_id(env_id)
    hostname = resolve_hostname(env_id)

    backup_root = f"{SYSTEST_ROOT}/{BACKUP_DIR}"

    checks: list[tuple[str, str]] = [
        ("Backup directory exists",
            f"test -d {backup_root} && echo 'YES' || echo 'NO'"),
        ("Backup directory size",
            f"du -sh {backup_root} 2>/dev/null || echo '(not found)'"),
        ("Backup files (sorted by date, newest first)",
            f"find {backup_root} -maxdepth 3 -type f \\( -name '*.bak' -o -name '*.db' -o -name '*.bi' -o -name '*.lg' -o -name '*.tar*' -o -name '*.zip' \\) "
            f"-exec ls -lh --time-style='+%Y-%m-%d %H:%M' {{}} \\; 2>/dev/null | sort -k6,7 -r | head -30 || echo '(no backup files found)'"),
        ("Recent backup activity (yab log)",
            f"grep -i 'backup\\|restore' {SYSTEST_ROOT}/{YAB_LOG_DIR}/*.log 2>/dev/null | tail -20 || echo '(no yab backup logs found)'"),
        ("Last database backup age",
            f"find {backup_root} -maxdepth 3 -type f -name '*.bak' -newer /proc/1 2>/dev/null | "
            f"xargs ls -lt --time-style='+%Y-%m-%d %H:%M' 2>/dev/null | head -5 || echo '(cannot determine)'"),
    ]

    results = await asyncio.gather(
        *[ssh.run(env_id, cmd) for _, cmd in checks],
        return_exceptions=True,
    )

    sections = [f"# Backup Information: {hostname}\n"]
    for (label, _), result in zip(checks, results):
        if isinstance(result, Exception):
            sections.append(f"## {label}\nERROR: {result}\n")
        else:
            out = result.stdout.strip() or "(no output)"
            sections.append(f"## {label}\n{out}\n")

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# Tool 10: pool_config_tuner
# ---------------------------------------------------------------------------


@mcp.tool()
async def pool_config_tuner(env_id: str) -> str:
    """Analyze Hikari connection pool settings and recommend improvements.

    Reads current pool configuration from qracore.properties, checks active
    database connection counts, and compares against known-good values from
    P0 postmortems. Flags under-provisioned pools before they cause outages.

    Args:
        env_id: Environment identifier (e.g. 'als2moherp5wcy')
    """
    env_id = _validate_env_id(env_id)
    hostname = resolve_hostname(env_id)
    config_path = f"{SYSTEST_ROOT}/{MAIN_CONFIG}"

    # Known-good minimums derived from P0 postmortems
    RECOMMENDED = {
        "qad-qracore.hikari.centraldb.maxpoolsize": 20,
        "qad-qracore.hikari.maxpoolsize": 20,
        "qad-qracore.hikari.connectionTimeout": 30000,
        "qad-qracore.hikari.idleTimeout": 600000,
        "qad-qracore.hikari.maxLifetime": 1800000,
        "qad-qracore.hikari.minimumIdle": 5,
    }

    config_result, active_conns = await asyncio.gather(
        ssh.run(env_id, f"grep -i 'hikari' {config_path}"),
        ssh.run(env_id,
            "ps -eo pid,args --no-headers | grep '_mprosrv' | grep -v grep | wc -l"),
    )

    sections = [f"# Hikari Pool Tuning: {hostname}\n"]

    # Parse current config
    current: dict[str, str] = {}
    if config_result.ok:
        for line in config_result.stdout.splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                current[k.strip()] = v.strip()
    else:
        sections.append(f"WARNING: Could not read config: {config_result.stderr}\n")

    sections.append("## Current Hikari Settings\n")
    if current:
        for k, v in sorted(current.items()):
            sections.append(f"  {k} = {v}")
    else:
        sections.append("  (no hikari properties found in config)")

    sections.append("\n## Recommendations\n")
    issues: list[str] = []
    for key, min_val in RECOMMENDED.items():
        current_val = current.get(key)
        if current_val is None:
            issues.append(
                f"  MISSING  {key}\n"
                f"           Recommended minimum: {min_val}\n"
                f"           Add to config: {key}={min_val}"
            )
        else:
            try:
                numeric = int(current_val)
                if numeric < min_val:
                    issues.append(
                        f"  LOW      {key} = {current_val}\n"
                        f"           Recommended minimum: {min_val} "
                        f"(increase by {min_val - numeric})"
                    )
            except ValueError:
                pass  # non-numeric value, skip comparison

    if issues:
        sections.extend(issues)
    else:
        sections.append("  All checked Hikari settings meet recommended minimums.")

    # Active DB connections
    sections.append("\n## Active Database Connections\n")
    conn_count = active_conns.stdout.strip() if active_conns.ok else "unknown"
    sections.append(f"  Active _mprosrv processes: {conn_count}")

    try:
        n_conns = int(conn_count)
        max_pool = int(current.get("qad-qracore.hikari.maxpoolsize", 0))
        if max_pool and n_conns > max_pool * 0.8:
            sections.append(
                f"  WARNING: Connection count ({n_conns}) is over 80% of "
                f"maxpoolsize ({max_pool}). Consider increasing maxpoolsize."
            )
    except (ValueError, TypeError):
        pass

    sections.append(
        "\nNote: After changing pool settings, restart Tomcat with: yab tomcat-webui-restart"
    )

    return "\n".join(sections)




@mcp.resource("qad://help/paths")
async def resource_paths() -> str:
    """QAD environment directory layout reference."""
    return f"""QAD Environment Directory Layout
================================
Root:       {SYSTEST_ROOT}
Config:     {SYSTEST_ROOT}/{MAIN_CONFIG}
Catalina:   {SYSTEST_ROOT}/{CATALINA_LOG}
yab:        cd {SYSTEST_ROOT} && yab <command>

SSH Pattern: {{env_id}}.environments.qad.com
Username:   mfg
"""


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    """Run the QAD Environment MCP server."""
    logger.info("Starting QAD Environment MCP server")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
