"""
QAD Environment MCP Server.

Provides tools for managing QAD Adaptive ERP environments via SSH.
Supports configuration viewing/editing, log tailing, yab operations,
and general environment inspection.

Usage:
    QAD_SSH_USERNAME=user QAD_SSH_PASSWORD=pass qad-env-mcp
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
    BUILD_CONFIG_ABS,
    CATALINA_LOG,
    CBSERVER_XML_PATHS,
    CT_LOG_DEFAULT_DIR,
    CT_LOG_DIR_PROP,
    CT_LOG_LEVEL_PROP,
    CT_LOG_LOGIN_PROP,
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
  Do NOT use for backup or restore — use database_backup / database_restore instead.
- yab_start / yab_status: For long-running yab operations (update, install, deploy).
  yab_start kicks off the command in the background; yab_status polls progress.
  Do NOT use yab_start for backup or restore — use database_backup / database_restore.
- database_backup: ALWAYS use this for database backups. It runs in the background
  automatically. Choose the right method based on context:
    - User wants env to stay running (e.g. "backup before config change"):
      use method="environment-online-backup"
    - User wants a full reliable backup or doesn't specify:
      use method="environment-offline-backup" (default)
    - User wants database-only backup:
      use method="database-all-backup" or "database-backup" (older EE)
    - User wants a named backup: add tag="descriptive-name"
      (only effective when dbbackup.timestamp=false)
  After starting, ALWAYS call yab_status() to monitor progress — do not wait
  for the user to ask. Poll until completion or report initial progress.
- database_restore: Restore a database from backup. DESTRUCTIVE — requires confirm=True.
    Use tag= to restore a specific named backup. For database-all-restore and
    database-restore methods, environment must be stopped first (tool checks this).
    WARNING: running yab update after restore may reload system data (AB-21244).
- database_backup_manage: List or remove database backups. Use scope= to choose:
    "all" (default), "environment", or "database".
- backup_info: Backup config, filesystem view of backup files, sizes and ages.
- read_config / update_config: View or modify QAD property files.
- tail_log: Read recent log entries from Tomcat or other services.
- list_jars: List deployed QAD module JARs (WAR/lib). NOT for yab version.
- run_command: Escape hatch for allowlisted shell commands (df, ps, etc.).
- health_check / service_status / db_status: Diagnostics and monitoring.
- ct_log_status: Check if CT (Financials BL) logging is enabled and show config.
- ct_log_enable: Enable CT logging. Sets debug level in configuration.properties
  and runs yab fin-cbserver-xml-update. Requires user confirmation.
- ct_log_disable: Disable CT logging. Sets level to 0. Requires user confirmation.
- ct_log_list: List CT log files in the debug directory.
- ct_log_read: Read contents of a specific CT log file.""",
)

ssh = SSHManager(
    username=os.environ["QAD_SSH_USERNAME"],
    password=os.environ["QAD_SSH_PASSWORD"],
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
    "environment-offline-backup", "environment-online-backup",
    "database-all-backup", "database-all-restore",
    "environment-restore", "database-all-backup-remove",
}

# Valid database backup/restore yab commands
YAB_BACKUP_COMMANDS = {
    "environment-offline-backup",
    "environment-online-backup",
    "database-all-backup",
    "database-backup",
}
YAB_RESTORE_COMMANDS = {
    "environment-restore",
    "database-all-restore",
    "database-restore",
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

    Do NOT use this tool for backup or restore — use database_backup() or database_restore() instead.

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

    # Reject backup/restore commands — must use dedicated tools
    if command in YAB_BACKUP_COMMANDS:
        return (
            f"Error: Use the database_backup() tool for '{command}', not yab_run.\n"
            f"Example: database_backup(env_id=\"{env_id}\", method=\"{command}\")"
        )
    if command in YAB_RESTORE_COMMANDS:
        return (
            f"Error: Use the database_restore() tool for '{command}', not yab_run.\n"
            f"Example: database_restore(env_id=\"{env_id}\", method=\"{command}\", confirm=True)"
        )

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
    """Start a long-running yab command in the background (update, install, deploy, etc.).

    The command runs via nohup so it survives disconnection. Use yab_status()
    to monitor progress and retrieve output.

    Do NOT use this for backup or restore — use database_backup() or database_restore() instead.

    Syntax: yab [options] COMMAND
    Options are placed BEFORE the command.

    Useful options:
        -v                    - Verbose: write log messages to console (default, captured to log file)
        -clean                - Force certain updates
        -log-level:LEVEL      - Set logging threshold (TRACE|DEBUG|INFO|WARN|ERROR|OFF)

    Args:
        env_id: Environment identifier (e.g. 'als2moherp5wcy')
        command: The yab command to execute (e.g. 'update', 'install', 'deploy')
        options: Yab options placed before the command (default: '-v' for verbose output)
    """
    env_id = _validate_env_id(env_id)
    hostname = resolve_hostname(env_id)
    command = command.strip().lower()

    for val, label in [(command, "command"), (options, "options")]:
        if any(c in val for c in [";", "|", "&", "`", "$", "(", ")"]):
            return f"Error: Invalid characters in {label}."

    # Reject backup/restore commands — must use dedicated tools
    if command in YAB_BACKUP_COMMANDS:
        return (
            f"Error: Use the database_backup() tool for '{command}', not yab_start.\n"
            f"Example: database_backup(env_id=\"{env_id}\", method=\"{command}\")"
        )
    if command in YAB_RESTORE_COMMANDS:
        return (
            f"Error: Use the database_restore() tool for '{command}', not yab_start.\n"
            f"Example: database_restore(env_id=\"{env_id}\", method=\"{command}\", confirm=True)"
        )

    # yab syntax: yab [options] COMMAND
    yab_cmd = f"yab {options} {command}".strip() if options else f"yab {command}"

    # Use a log/pid file keyed by host and command
    log_file = f"{YAB_LOG_PREFIX}_{hostname}_{command}.log"
    pid_file = f"{YAB_LOG_PREFIX}_{hostname}_{command}.pid"

    # Check if this command is already running
    check_cmd = (
        f"test -f {pid_file} && pid=$(cat {pid_file}) && "
        f"kill -0 $pid 2>/dev/null && echo \"RUNNING:$pid\" || echo \"NONE\""
    )
    check = await ssh.run(env_id, check_cmd, timeout=10.0)
    if check.ok and check.stdout.strip().startswith("RUNNING:"):
        existing_pid = check.stdout.strip().split(":")[1]
        return (
            f"A '{command}' job is already running on {hostname} (PID {existing_pid}).\n"
            f"Log file: {log_file}\n\n"
            f"Use yab_status(env_id=\"{env_id}\", command=\"{command}\") to check progress.\n"
            f"Do NOT start another — wait for this one to finish."
        )

    # Clean up any previous log, start command in background.
    # nohup runs in a subshell, so we must cd to SYSTEST_ROOT explicitly
    # (the cd prepended by ssh.run applies to the outer shell only).
    launch_cmd = (
        f"rm -f {log_file} {pid_file} && "
        f"nohup bash -c 'cd {SYSTEST_ROOT} && {yab_cmd}' > {log_file} 2>&1 & "
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
        marker = "  *** MISMATC=H" if len(set(vers)) > 1 else ""
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
# Tool 9: backup_info, database_backup, database_restore, database_backup_manage
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
        ("Backup configuration (yab)",
            f"cd {SYSTEST_ROOT} && yab config dbbackup.dir dbbackup.timestamp dbbackup.timestampmax dbbackup.compress dbbackup.delaycompression 2>/dev/null || echo '(cannot read yab config)'"),
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


@mcp.tool()
async def database_backup(
    env_id: str,
    method: str = "environment-offline-backup",
    tag: str | None = None,
) -> str:
    """Create a database backup on a QAD environment.

    This is the ONLY tool for creating backups. Do not use yab_run or yab_start.

    The backup runs in the background via nohup. After calling this tool,
    ALWAYS follow up with yab_status(env_id, command=method) to monitor
    progress — do not wait for the user to ask.

    Method selection guide:
        environment-offline-backup  - Use when the user wants maximum reliability or
                                      says "full backup" or does not specify a preference.
                                      Stops the environment during backup. (default)
        environment-online-backup   - Use when the user wants the environment to stay
                                      running (e.g. "backup before config change",
                                      "quick backup", "backup without downtime").
        database-all-backup         - Use when the user says "database only" or
                                      "just the databases" (all Progress DBs, no env
                                      lifecycle).
        database-backup             - Use for single Progress database backup on older
                                      EE environments.

    Tag behaviour (from YAB docs):
        When dbbackup.timestamp=true (default), backups auto-name with timestamps
        and the -tag option is ignored.
        When dbbackup.timestamp=false, the -tag option names the backup subdirectory
        under dbbackup.dir. Without -tag, backups go to a "default" subdirectory
        and overwrite any existing backup there.

    Args:
        env_id: Environment identifier (e.g. 'als2moherp5wcy')
        method: Backup method — one of 'environment-offline-backup',
                'environment-online-backup', 'database-all-backup', or
                'database-backup'. Choose based on context.
        tag: Optional backup tag name (e.g. '20260330pre-upgrade'). Creates a
             named subdirectory under dbbackup.dir. Only effective when
             dbbackup.timestamp=false. Use descriptive names like YYYYMMDD+context.
    """
    env_id = _validate_env_id(env_id)
    hostname = resolve_hostname(env_id)
    method = method.strip().lower()

    if method not in YAB_BACKUP_COMMANDS:
        return (
            f"Error: Unknown backup method '{method}'. "
            f"Valid methods: {', '.join(sorted(YAB_BACKUP_COMMANDS))}"
        )

    yab_cmd = f"yab -v {method}"
    if tag:
        # Sanitize tag: allow alphanumeric, hyphens, underscores, dots only
        safe_tag = re.sub(r"[^a-zA-Z0-9._-]", "", tag)
        if not safe_tag:
            return "Error: Tag contains no valid characters. Use alphanumeric, hyphens, underscores, or dots."
        yab_cmd += f" -tag:{safe_tag}"
    log_file = f"{YAB_LOG_PREFIX}_{hostname}_{method}.log"
    pid_file = f"{YAB_LOG_PREFIX}_{hostname}_{method}.pid"

    # Check if a backup of this type is already running
    check_cmd = (
        f"test -f {pid_file} && pid=$(cat {pid_file}) && "
        f"kill -0 $pid 2>/dev/null && echo \"RUNNING:$pid\" || echo \"NONE\""
    )
    check = await ssh.run(env_id, check_cmd, timeout=10.0)
    if check.ok and check.stdout.strip().startswith("RUNNING:"):
        existing_pid = check.stdout.strip().split(":")[1]
        return (
            f"A '{method}' backup is already running on {hostname} (PID {existing_pid}).\n"
            f"Log file: {log_file}\n\n"
            f"Use yab_status(env_id=\"{env_id}\", command=\"{method}\") to monitor progress.\n"
            f"Do NOT start another backup — wait for this one to finish."
        )

    launch_cmd = (
        f"rm -f {log_file} {pid_file} && "
        f"nohup bash -c 'cd {SYSTEST_ROOT} && {yab_cmd}' > {log_file} 2>&1 & "
        f"echo $! | tee {pid_file}"
    )

    result = await ssh.run(env_id, launch_cmd, timeout=30.0)

    if result.ok:
        pid = result.stdout.strip()
        return (
            f"Started `{yab_cmd}` on {hostname} (PID {pid}).\n"
            f"Log file: {log_file}\n\n"
            f"Use yab_status(env_id=\"{env_id}\", command=\"{method}\") to monitor progress."
        )

    # Launch timed out or failed — check if the process started anyway
    # (nohup may have succeeded before the SSH channel returned)
    verify_cmd = (
        f"test -f {pid_file} && pid=$(cat {pid_file}) && "
        f"kill -0 $pid 2>/dev/null && echo \"RUNNING:$pid\" || echo \"NONE\""
    )
    verify = await ssh.run(env_id, verify_cmd, timeout=10.0)
    if verify.ok and verify.stdout.strip().startswith("RUNNING:"):
        pid = verify.stdout.strip().split(":")[1]
        return (
            f"Started `{yab_cmd}` on {hostname} (PID {pid}).\n"
            f"(SSH was slow but the backup launched successfully.)\n"
            f"Log file: {log_file}\n\n"
            f"Use yab_status(env_id=\"{env_id}\", command=\"{method}\") to monitor progress."
        )

    return (
        f"Failed to start backup on {hostname}: {result.stderr}\n\n"
        f"The backup process did not launch. This is likely an SSH connectivity "
        f"issue with {hostname} (server under load, network timeout, etc.).\n"
        f"Retry this same database_backup() call. "
        f"Do NOT use yab_run or yab_start as a workaround — they will reject backup commands."
    )


@mcp.tool()
async def database_restore(
    env_id: str,
    method: str = "environment-restore",
    tag: str | None = None,
    confirm: bool = False,
) -> str:
    """Restore a database backup on a QAD environment.

    *** DESTRUCTIVE OPERATION — requires confirm=True ***

    This will overwrite the current database with a previously created backup.

    Available restore methods:
        environment-restore    - Full environment restore. Handles stop/start
                                 lifecycle automatically. (default)
        database-all-restore   - Restore all Progress databases. The environment
                                 MUST already be stopped (`yab stop` first).
        database-restore       - Restore single Progress database (older EE envs).
                                 Environment MUST already be stopped.

    *** WARNING (AB-21244): Running `yab update` after a database-restore resets
    an internal ID, causing system data (browses, Reporting Framework) to reload
    and overwrite customisations. If you need to preserve those changes, consider
    using `prorest <database> <backup file>` manually instead of yab restore. ***

    The restore runs in the background. Use yab_status() to monitor progress.

    Args:
        env_id: Environment identifier (e.g. 'als2moherp5wcy')
        method: Restore method — 'environment-restore', 'database-all-restore',
                or 'database-restore'.
        tag: Optional backup tag to restore from (e.g. '20260330pre-upgrade').
             Selects which backup subdirectory under dbbackup.dir to use.
             Without -tag, restores from the "default" subdirectory.
        confirm: Must be set to True to proceed. This is a destructive operation
                 that overwrites the current database.
    """
    env_id = _validate_env_id(env_id)
    hostname = resolve_hostname(env_id)
    method = method.strip().lower()

    if not confirm:
        return (
            f"⚠️  DATABASE RESTORE is a destructive operation on {hostname}.\n"
            f"This will OVERWRITE the current database with the backup.\n\n"
            f"To proceed, call database_restore with confirm=True.\n"
            f"Make sure you have a current backup before restoring.\n\n"
            f"⚠️  Note (AB-21244): Running `yab update` after restore reloads system data\n"
            f"and may overwrite browse/Reporting Framework customisations."
        )

    if method not in YAB_RESTORE_COMMANDS:
        return (
            f"Error: Unknown restore method '{method}'. "
            f"Valid methods: {', '.join(sorted(YAB_RESTORE_COMMANDS))}"
        )

    # For methods that require the environment to be stopped, verify status first
    if method in ("database-all-restore", "database-restore"):
        status_result = await ssh.run(
            env_id, f"cd {SYSTEST_ROOT} && yab status 2>&1 | head -5", timeout=30.0
        )
        status_out = status_result.stdout.lower()
        if "started" in status_out and "ok" in status_out:
            return (
                f"Error: Method '{method}' requires the environment to be stopped first.\n"
                f"The environment on {hostname} appears to still be running.\n\n"
                f"Run `yab stop` first (via yab_run), then retry the restore.\n"
                f"Or use method='environment-restore' which handles stop/start automatically."
            )

    yab_cmd = f"yab -v {method}"
    if tag:
        safe_tag = re.sub(r"[^a-zA-Z0-9._-]", "", tag)
        if not safe_tag:
            return "Error: Tag contains no valid characters. Use alphanumeric, hyphens, underscores, or dots."
        yab_cmd += f" -tag:{safe_tag}"

    log_file = f"{YAB_LOG_PREFIX}_{hostname}_{method}.log"
    pid_file = f"{YAB_LOG_PREFIX}_{hostname}_{method}.pid"

    launch_cmd = (
        f"rm -f {log_file} {pid_file} && "
        f"nohup bash -lc 'cd {SYSTEST_ROOT} && {yab_cmd}' > {log_file} 2>&1 & "
        f"echo $! | tee {pid_file}"
    )

    result = await ssh.run(env_id, launch_cmd, timeout=15.0)
    if not result.ok:
        return f"Failed to start restore on {hostname}: {result.stderr}"

    pid = result.stdout.strip()
    return (
        f"Started `{yab_cmd}` on {hostname} (PID {pid}).\n"
        f"Log file: {log_file}\n\n"
        f"Use yab_status(env_id=\"{env_id}\", command=\"{method}\") to monitor progress.\n\n"
        f"⚠️  After restore, be cautious with `yab update` — it may reload system data\n"
        f"and overwrite browse/Reporting Framework customisations (see AB-21244)."
    )


@mcp.tool()
async def database_backup_manage(
    env_id: str,
    action: str = "list",
    scope: str = "all",
    confirm: bool = False,
) -> str:
    """List or remove database backups on a QAD environment.

    Actions:
        list   - List available backups (default). Non-destructive.
        remove - Remove all database backups. Requires confirm=True.

    Scope controls which yab list command is used:
        all         - database-all-backup-list (all Progress DBs, default)
        environment - environment-backup-list (full environment backups including
                      Mongo, files, etc.)
        database    - database-backup-list (single Progress DB, older EE envs)

    Args:
        env_id: Environment identifier (e.g. 'als2moherp5wcy')
        action: 'list' to show backups, 'remove' to delete them
        scope: 'all' (default), 'environment', or 'database' — selects which
               yab backup-list command to use
        confirm: Required for 'remove' action (destructive operation)
    """
    env_id = _validate_env_id(env_id)
    hostname = resolve_hostname(env_id)
    action = action.strip().lower()
    scope = scope.strip().lower()

    list_commands = {
        "all": "database-all-backup-list",
        "environment": "environment-backup-list",
        "database": "database-backup-list",
    }

    if action == "list":
        yab_list_cmd = list_commands.get(scope)
        if not yab_list_cmd:
            return (
                f"Error: Unknown scope '{scope}'. "
                f"Valid scopes: {', '.join(sorted(list_commands))}"
            )
        result = await ssh.run(
            env_id, f"cd {SYSTEST_ROOT} && yab {yab_list_cmd}", timeout=120.0
        )
        output = result.stdout
        if result.stderr:
            output += f"\n--- stderr ---\n{result.stderr}"
        status = "✓" if result.ok else "✗"
        return f"{status} Backup list ({yab_list_cmd}) on {hostname}:\n\n{output}"

    elif action == "remove":
        if not confirm:
            return (
                f"⚠️  This will REMOVE ALL database backups on {hostname}.\n"
                f"This operation cannot be undone.\n\n"
                f"To proceed, call database_backup_manage with action='remove', confirm=True."
            )
        result = await ssh.run(
            env_id, f"cd {SYSTEST_ROOT} && yab database-all-backup-remove", timeout=120.0
        )
        output = result.stdout
        if result.stderr:
            output += f"\n--- stderr ---\n{result.stderr}"
        status = "✓" if result.ok else "✗"
        return f"{status} Database backup removal on {hostname}:\n\n{output}"

    else:
        return f"Error: Unknown action '{action}'. Valid actions: list, remove"


# ---------------------------------------------------------------------------
# CT Log tools (Financials BL debugging)
# ---------------------------------------------------------------------------


async def _find_cbserver_xml(env_id: str) -> str | None:
    """Return the first existing cbserver.xml path (absolute), or None."""
    for rel in CBSERVER_XML_PATHS:
        result = await ssh.run(env_id, f"test -f {SYSTEST_ROOT}/{rel} && echo found")
        if result.ok and "found" in result.stdout:
            return f"{SYSTEST_ROOT}/{rel}"
    return None


async def _get_ct_debug_directory(env_id: str) -> str:
    """Determine the CT log output directory from cbserver.xml or defaults."""
    # 1. Check configuration.properties for explicit override
    result = await ssh.run(
        env_id,
        f"grep '^{CT_LOG_DIR_PROP}=' {BUILD_CONFIG_ABS} 2>/dev/null",
    )
    if result.ok and result.stdout.strip():
        val = result.stdout.strip().split("=", 1)[1].strip()
        if val:
            return val

    # 2. Check cbserver.xml for <DebugDirectory>
    xml_path = await _find_cbserver_xml(env_id)
    if xml_path:
        result = await ssh.run(
            env_id,
            f"grep -oP '<DebugDirectory>\\K[^<]+' {xml_path} 2>/dev/null",
        )
        if result.ok and result.stdout.strip():
            return result.stdout.strip()

    # 3. Fall back to default
    return f"{SYSTEST_ROOT}/{CT_LOG_DEFAULT_DIR}"


async def _set_build_config_prop(env_id: str, key: str, value: str) -> None:
    """Add or update a property in configuration.properties."""
    escaped_key = re.escape(key).replace("/", "\\/")
    escaped_value = value.replace("/", "\\/").replace("&", "\\&")

    check = await ssh.run(env_id, f"grep -c '^{key}=' {BUILD_CONFIG_ABS} 2>/dev/null")
    if check.ok and check.stdout.strip() not in ("0", ""):
        # Property exists — update in place
        await ssh.run_checked(
            env_id,
            f"sed -i 's/^{escaped_key}=.*/{escaped_key}={escaped_value}/' {BUILD_CONFIG_ABS}",
        )
    else:
        # Property doesn't exist — append
        await ssh.run_checked(
            env_id,
            f"echo '{key}={value}' >> {BUILD_CONFIG_ABS}",
        )


async def _remove_build_config_prop(env_id: str, key: str) -> None:
    """Remove a property from configuration.properties if it exists."""
    escaped_key = re.escape(key).replace("/", "\\/")
    await ssh.run(
        env_id,
        f"sed -i '/^{escaped_key}=/d' {BUILD_CONFIG_ABS}",
    )


@mcp.tool()
async def ct_log_status(env_id: str) -> str:
    """Check CT (Financials BL) logging status on a QAD environment.

    Reports:
    - Current debug level, directory, and login filter from configuration.properties
    - Applied values from cbserver.xml
    - Whether CT logging is active
    - Recent CT log files in the debug directory

    Args:
        env_id: Environment identifier (e.g. 'als2moherp5wcy')
    """
    env_id = _validate_env_id(env_id)
    hostname = resolve_hostname(env_id)

    # Read configuration.properties for fin.cbserverxml.* props
    config_result = await ssh.run(
        env_id,
        f"grep 'fin.cbserverxml' {BUILD_CONFIG_ABS} 2>/dev/null",
    )

    # Find and read cbserver.xml
    xml_path = await _find_cbserver_xml(env_id)
    xml_info = ""
    if xml_path:
        xml_result = await ssh.run(
            env_id,
            f"grep -E '<Debug(Level|Directory|Login)>' {xml_path} 2>/dev/null",
        )
        xml_info = xml_result.stdout.strip() if xml_result.ok else "(could not read)"
    else:
        xml_info = "(cbserver.xml not found)"

    # Determine debug directory and list log files
    debug_dir = await _get_ct_debug_directory(env_id)
    log_list = await ssh.run(
        env_id,
        f"ls -lhtr {debug_dir}/ct*.log {debug_dir}/ServerLog.csv "
        f"{debug_dir}/UnitTestReport.txt 2>/dev/null | tail -n 20",
    )

    # Parse current level
    level_str = "not set"
    is_active = False
    if config_result.ok:
        for line in config_result.stdout.splitlines():
            if CT_LOG_LEVEL_PROP in line and "=" in line:
                val = line.split("=", 1)[1].strip()
                level_str = val
                try:
                    is_active = int(val) > 0
                except ValueError:
                    pass

    # Build output
    sections = [f"# CT Log Status: {hostname}\n"]

    sections.append(f"## Status: {'ENABLED (level {level_str})' if is_active else 'DISABLED'}\n")

    sections.append("## configuration.properties (fin.cbserverxml.*)")
    if config_result.ok and config_result.stdout.strip():
        sections.append(config_result.stdout.strip())
    else:
        sections.append("  (no fin.cbserverxml.* properties found)")

    sections.append(f"\n## cbserver.xml ({xml_path or 'not found'})")
    sections.append(xml_info)

    sections.append(f"\n## Debug directory: {debug_dir}")
    if log_list.ok and log_list.stdout.strip():
        sections.append(f"## Recent CT log files:\n{log_list.stdout.strip()}")
    else:
        sections.append("  (no CT log files found)")

    sections.append(
        "\n## Debug level reference (additive bitmask):\n"
        "  +1  = Limited BL execution logging\n"
        "  +2  = Extended BL execution logging\n"
        "  +4  = Log parameter values\n"
        "  +8  = Database access logging\n"
        "  +16 = Detailed DB update logging\n"
        "  +32 = Unit testing\n"
        "  Common: 31 (all except unit testing)"
    )

    return "\n".join(sections)


@mcp.tool()
async def ct_log_enable(
    env_id: str,
    debug_level: int = 31,
    debug_login: str | None = None,
    trim_appserver: bool = True,
) -> str:
    """Enable CT (Financials BL) logging on a QAD environment.

    Updates configuration.properties with the debug level, runs
    yab fin-cbserver-xml-update to apply changes to cbserver.xml,
    and optionally trims the Financials appserver to pick up the new config.

    Debug level is a bitmask (add values together):
      +1  = Limited BL execution logging (entry-level methods)
      +2  = Extended BL execution logging (all methods)
      +4  = Log parameter values of business methods
      +8  = Database access logging (reads and updates)
      +16 = Detailed database update logging (create/modify/delete)
      +32 = Unit testing / performance analysis
    Common: 31 (all except unit testing), 6 (extended + params)

    This is a potentially destructive operation — requires user confirmation.

    Args:
        env_id: Environment identifier (e.g. 'als2moherp5wcy')
        debug_level: CT log debug level bitmask (default 31 = all except unit testing)
        debug_login: Optional login to filter logging to a specific user
        trim_appserver: Whether to trim the Financials appserver after update (default True)
    """
    env_id = _validate_env_id(env_id)
    hostname = resolve_hostname(env_id)

    if debug_level < 1 or debug_level > 63:
        return "Error: debug_level must be between 1 and 63 (bitmask of levels 1+2+4+8+16+32)."

    sections = [f"# Enabling CT Logging on {hostname}\n"]

    # Step 1: Set debug level in configuration.properties
    await _set_build_config_prop(env_id, CT_LOG_LEVEL_PROP, str(debug_level))
    sections.append(f"  Set {CT_LOG_LEVEL_PROP}={debug_level}")

    # Step 2: Optionally set debug login
    if debug_login:
        await _set_build_config_prop(env_id, CT_LOG_LOGIN_PROP, debug_login)
        sections.append(f"  Set {CT_LOG_LOGIN_PROP}={debug_login}")

    # Step 3: Run yab fin-cbserver-xml-update
    sections.append("\n## Running yab fin-cbserver-xml-update...")
    result = await ssh.run(env_id, "yab fin-cbserver-xml-update", timeout=120.0)
    if result.ok:
        sections.append(f"  ✓ cbserver.xml updated successfully")
        if result.stdout.strip():
            sections.append(result.stdout.strip())
    else:
        output = result.stdout
        if result.stderr:
            output += f"\n{result.stderr}"
        sections.append(f"  ✗ cbserver.xml update failed:\n{output}")
        return "\n".join(sections)

    # Step 4: Optionally trim appserver
    if trim_appserver:
        sections.append("\n## Trimming Financials appserver...")
        trim_result = await ssh.run(env_id, "yab appserver-fin-trim", timeout=120.0)
        if trim_result.ok:
            sections.append("  ✓ Appserver trimmed — new agents will use updated config")
        else:
            sections.append(
                f"  ⚠ Trim returned exit code {trim_result.exit_code}. "
                "Agents may need manual restart to pick up changes."
            )
            if trim_result.stdout.strip():
                sections.append(trim_result.stdout.strip())

    # Report debug directory
    debug_dir = await _get_ct_debug_directory(env_id)
    sections.append(f"\n## CT log files will be written to: {debug_dir}")
    sections.append("  Log filenames: ct<sessionid>.log")
    sections.append(
        "\nNote: CT logs are created per session. Use ct_log_list to find files "
        "and ct_log_read to view them."
    )

    return "\n".join(sections)


@mcp.tool()
async def ct_log_disable(
    env_id: str,
    trim_appserver: bool = True,
) -> str:
    """Disable CT (Financials BL) logging on a QAD environment.

    Sets the debug level to 0 in configuration.properties, removes the
    debug login filter, runs yab fin-cbserver-xml-update, and optionally
    trims the Financials appserver.

    This is a potentially destructive operation — requires user confirmation.

    Args:
        env_id: Environment identifier (e.g. 'als2moherp5wcy')
        trim_appserver: Whether to trim the Financials appserver after update (default True)
    """
    env_id = _validate_env_id(env_id)
    hostname = resolve_hostname(env_id)

    sections = [f"# Disabling CT Logging on {hostname}\n"]

    # Set debug level to 0
    await _set_build_config_prop(env_id, CT_LOG_LEVEL_PROP, "0")
    sections.append(f"  Set {CT_LOG_LEVEL_PROP}=0")

    # Remove debug login if present
    await _remove_build_config_prop(env_id, CT_LOG_LOGIN_PROP)
    sections.append(f"  Removed {CT_LOG_LOGIN_PROP} (if present)")

    # Run yab fin-cbserver-xml-update
    sections.append("\n## Running yab fin-cbserver-xml-update...")
    result = await ssh.run(env_id, "yab fin-cbserver-xml-update", timeout=120.0)
    if result.ok:
        sections.append("  ✓ cbserver.xml updated successfully")
        if result.stdout.strip():
            sections.append(result.stdout.strip())
    else:
        output = result.stdout
        if result.stderr:
            output += f"\n{result.stderr}"
        sections.append(f"  ✗ cbserver.xml update failed:\n{output}")
        return "\n".join(sections)

    # Optionally trim appserver
    if trim_appserver:
        sections.append("\n## Trimming Financials appserver...")
        trim_result = await ssh.run(env_id, "yab appserver-fin-trim", timeout=120.0)
        if trim_result.ok:
            sections.append("  ✓ Appserver trimmed")
        else:
            sections.append(
                f"  ⚠ Trim returned exit code {trim_result.exit_code}. "
                "You may need to restart the appserver manually."
            )

    sections.append("\nCT logging is now disabled. Existing log files are preserved.")

    return "\n".join(sections)


@mcp.tool()
async def ct_log_list(
    env_id: str,
    debug_directory: str | None = None,
) -> str:
    """List CT (Financials BL) log files on a QAD environment.

    Searches the debug directory for ct*.log files, ServerLog.csv, and
    UnitTestReport.txt. Shows file sizes and modification dates.

    Args:
        env_id: Environment identifier (e.g. 'als2moherp5wcy')
        debug_directory: Override debug directory path. If not specified,
                         auto-detected from cbserver.xml or configuration.properties.
    """
    env_id = _validate_env_id(env_id)
    hostname = resolve_hostname(env_id)

    if debug_directory:
        debug_dir = debug_directory
    else:
        debug_dir = await _get_ct_debug_directory(env_id)

    # List all CT log related files
    result = await ssh.run(
        env_id,
        f"ls -lhtr {debug_dir}/ct*.log {debug_dir}/ServerLog.csv "
        f"{debug_dir}/UnitTestReport.txt 2>/dev/null",
    )

    # Also count total files and disk usage
    count_result = await ssh.run(
        env_id,
        f"find {debug_dir} -maxdepth 1 -name 'ct*.log' 2>/dev/null | wc -l",
    )
    du_result = await ssh.run(
        env_id,
        f"du -sh {debug_dir} 2>/dev/null",
    )

    sections = [f"# CT Log Files: {hostname}"]
    sections.append(f"## Directory: {debug_dir}\n")

    if du_result.ok and du_result.stdout.strip():
        sections.append(f"Total directory size: {du_result.stdout.strip().split()[0]}")

    ct_count = count_result.stdout.strip() if count_result.ok else "?"
    sections.append(f"CT log file count: {ct_count}\n")

    if result.ok and result.stdout.strip():
        sections.append("## Files (oldest first):\n")
        sections.append(result.stdout.strip())
    else:
        sections.append("(no CT log files found in this directory)")

    return "\n".join(sections)


@mcp.tool()
async def ct_log_read(
    env_id: str,
    file_name: str,
    lines: int = 200,
    grep_pattern: str | None = None,
) -> str:
    """Read contents of a CT (Financials BL) log file.

    Reads the specified CT log file from the debug directory. Supports
    tailing a specific number of lines and optional grep filtering.

    Args:
        env_id: Environment identifier (e.g. 'als2moherp5wcy')
        file_name: CT log file name (e.g. 'ct12345.log') or absolute path.
                   If just a filename, it is resolved against the debug directory.
        lines: Number of lines to retrieve (default 200, max 500)
        grep_pattern: Optional pattern to filter log lines
    """
    env_id = _validate_env_id(env_id)
    hostname = resolve_hostname(env_id)
    lines = min(max(lines, 1), 500)

    # Block shell injection in file_name and grep_pattern
    for val, label in [(file_name, "file_name")]:
        if any(c in val for c in [";", "|", "&", "`", "$", "(", ")"]):
            return f"Error: Invalid characters in {label}."
    if grep_pattern and any(c in grep_pattern for c in [";", "|", "&", "`", "$", "(", ")"]):
        return "Error: Invalid characters in grep_pattern."

    # Resolve file path
    if file_name.startswith("/"):
        file_path = file_name
    else:
        debug_dir = await _get_ct_debug_directory(env_id)
        file_path = f"{debug_dir}/{file_name}"

    # Verify file exists
    check = await ssh.run(env_id, f"test -f '{file_path}' && echo found")
    if not (check.ok and "found" in check.stdout):
        return f"File not found: {file_path} on {hostname}"

    # Get file size for context
    size_result = await ssh.run(env_id, f"ls -lh '{file_path}' | awk '{{print $5}}'")
    file_size = size_result.stdout.strip() if size_result.ok else "unknown"

    # Read the file
    if grep_pattern:
        cmd = (
            f"tail -n {lines * 5} '{file_path}' | "
            f"grep -i '{grep_pattern}' | tail -n {lines}"
        )
    else:
        cmd = f"tail -n {lines} '{file_path}'"

    result = await ssh.run(env_id, cmd)

    if not result.ok:
        return f"Failed to read {file_path} on {hostname}: {result.stderr}"

    if not result.stdout.strip():
        qualifier = f" matching '{grep_pattern}'" if grep_pattern else ""
        return f"No content{qualifier} found in {file_path} on {hostname}"

    header = f"# CT Log: {file_name} on {hostname} (size: {file_size})"
    if grep_pattern:
        header += f" (filtered: '{grep_pattern}')"
    header += f"\n# Showing last {lines} lines\n"

    return f"{header}\n{result.stdout}"


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
Username:   $QAD_SSH_USERNAME
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
