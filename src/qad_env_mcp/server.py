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

import logging
import os
import re
import sys

from mcp.server.fastmcp import FastMCP

from .paths import (
    CATALINA_LOG,
    LIB_ABS,
    LIB_DIR,
    MAIN_CONFIG,
    QAD_JAR_PREFIX,
    SYSTEST_ROOT,
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
Use the tools to inspect configuration, read logs, run yab commands, and manage environments.
When the user mentions an environment by name or ID, use that as the env_id parameter.
Always confirm destructive operations (restart, update, backup) with the user before executing.""",
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
YAB_SAFE_COMMANDS = {"status", "version", "list", "help", "info"}

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
    subcommand: str,
    args: str = "",
) -> str:
    """Execute a yab (Your Application Builder) command on a QAD environment.

    Common subcommands:
        status                - Show environment status
        tomcat-webui-restart  - Restart Tomcat (requires confirmation)
        stop                  - Stop Tomcat (requires confirmation)
        start                 - Start Tomcat (requires confirmation)
        update                - Update the environment (requires confirmation)
        backup                - Backup the database (requires confirmation)
        version               - Show version information
        list                  - List available operations
        help                  - Show yab help

    Args:
        env_id: Environment identifier (e.g. 'als2moherp5wcy')
        subcommand: The yab subcommand to execute (e.g. 'restart', 'status')
        args: Additional arguments for the subcommand
    """
    env_id = _validate_env_id(env_id)
    hostname = resolve_hostname(env_id)
    subcommand = subcommand.strip().lower()

    # Block obviously dangerous patterns
    if any(c in subcommand for c in [";", "|", "&", "`", "$", "(", ")"]):
        return "Error: Invalid characters in subcommand."

    if any(c in args for c in [";", "|", "&", "`", "$", "(", ")"]):
        return "Error: Invalid characters in arguments."

    # Warn about destructive commands (the LLM should confirm with user)
    warning = ""
    if subcommand in YAB_DANGEROUS_COMMANDS:
        warning = (
            f"⚠️  '{subcommand}' is a potentially destructive operation on {hostname}.\n"
            f"Proceeding as instructed.\n\n"
        )

    cmd = f"./yab {subcommand}"
    if args:
        cmd += f" {args}"

    # yab commands can be slow (especially update/backup)
    timeout = 300.0 if subcommand in {"update", "backup", "restore", "deploy"} else 120.0

    result = await ssh.run(env_id, cmd, timeout=timeout)

    output = result.stdout
    if result.stderr:
        output += f"\n--- stderr ---\n{result.stderr}"

    status = "✓" if result.ok else "✗"
    return f"{warning}{status} yab {subcommand} on {hostname} (exit {result.exit_code}):\n\n{output}"


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
# Resources (optional — provides context to LLMs about the environment)
# ---------------------------------------------------------------------------

@mcp.resource("qad://help/paths")
async def resource_paths() -> str:
    """QAD environment directory layout reference."""
    return f"""QAD Environment Directory Layout
================================
Root:       {SYSTEST_ROOT}
Config:     {SYSTEST_ROOT}/{MAIN_CONFIG}
Catalina:   {SYSTEST_ROOT}/{CATALINA_LOG}
yab:        cd {SYSTEST_ROOT} && ./yab <command>

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
