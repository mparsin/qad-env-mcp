# qad-env-mcp

MCP server for managing QAD Adaptive ERP environments via natural language.

Connect this to Claude Desktop, Cursor, or Claude Code and manage your environments by asking things like:

- *"Show me the config for als2moherp5wcy"*
- *"What are the last 100 ERROR lines in catalina.out on abc123?"*
- *"Enable SSM feature flags on cdb546"*
- *"Restart tomcat on xyz789"*
- *"Check if als2moherp5wcy is reachable"*

## Quick Start (Step by Step)

Follow these steps to get the MCP server running on your machine:

1. **Install uv** (if you don't have it):
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. **Clone the repo:**
   ```bash
   git clone git@github.com:mparsin/qad-env-mcp.git
   cd qad-env-mcp
   ```

3. **Install dependencies:**
   ```bash
   uv sync
   ```

4. **Connect to your VPN** — environments are on the internal network.

5. **Add the server to your AI client** — pick one:

   **Claude Code** (quickest):
   ```bash
   claude mcp add qad-env \
     -e QAD_SSH_USERNAME=your-username \
     -e QAD_SSH_PASSWORD=your-password \
     -- uv run --directory /path/to/qad-env-mcp qad-env-mcp
   ```

   **Claude Desktop** — edit `~/Library/Application Support/Claude/claude_desktop_config.json`:
   ```json
   {
     "mcpServers": {
       "qad-env": {
         "command": "uv",
         "args": ["run", "--directory", "/path/to/qad-env-mcp", "qad-env-mcp"],
         "env": {
           "QAD_SSH_USERNAME": "<your-username>",
           "QAD_SSH_PASSWORD": "<your-password>"
         }
       }
     }
   }
   ```

   **Cursor** — add to `.cursor/mcp.json` (project) or `~/.cursor/mcp.json` (global):
   ```json
   {
     "mcpServers": {
       "qad-env": {
         "command": "uv",
         "args": ["run", "--directory", "/path/to/qad-env-mcp", "qad-env-mcp"],
         "env": {
           "QAD_SSH_USERNAME": "<your-username>",
           "QAD_SSH_PASSWORD": "<your-password>"
         }
       }
     }
   }
   ```

6. **Try it out** — open your AI client and ask:
   > "Check if als2moherp5wcy is reachable"

   If you get a response, you're all set.

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- Corporate VPN connected (environments are on internal network)
- SSH access to QAD environments (credentials via environment variables)

## Installation

### Option A: Install from Git (recommended for the team)

```bash
# Using uv
uv tool install git+https://github.com/mparsin/qad-env-mcp.git

# Or using pip
pip install git+https://github.com/mparsin/qad-env-mcp.git
```

### Option B: Local development

```bash
git clone git@github.com:mparsin/qad-env-mcp.git
cd qad-env-mcp
uv sync
```

## Configuration

The server uses sensible defaults. Override via environment variables if needed:

| Variable | Required | Description |
|---|---|---|
| `QAD_SSH_USERNAME` | **yes** | SSH username for all environments |
| `QAD_SSH_PASSWORD` | **yes** | SSH password for all environments |
| `QAD_SSH_PORT` | no (default `22`) | SSH port |

## Client Setup

### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS)
or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "qad-env": {
      "command": "qad-env-mcp",
      "env": {
        "QAD_SSH_USERNAME": "<your-username>",
        "QAD_SSH_PASSWORD": "<your-password>"
      }
    }
  }
}
```

If installed via `uv tool install`, the `qad-env-mcp` binary is on your PATH.
For local dev, use the full path:

```json
{
  "mcpServers": {
    "qad-env": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/qad-env-mcp", "qad-env-mcp"],
      "env": {
        "QAD_SSH_USERNAME": "<your-username>",
        "QAD_SSH_PASSWORD": "<your-password>"
      }
    }
  }
}
```

### Cursor

Add to `.cursor/mcp.json` in your project root (or globally in `~/.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "qad-env": {
      "command": "qad-env-mcp",
      "env": {
        "QAD_SSH_USERNAME": "<your-username>",
        "QAD_SSH_PASSWORD": "<your-password>"
      }
    }
  }
}
```

### Claude Code

```bash
claude mcp add qad-env qad-env-mcp
```

Or add to `~/.claude/claude_code_config.json`:

```json
{
  "mcpServers": {
    "qad-env": {
      "command": "qad-env-mcp"
    }
  }
}
```

## Available Tools

### Core Tools

| Tool | Description | Example prompt |
|---|---|---|
| `get_config` | Read configuration properties, optionally filtered by key | *"Show SSM config for abc123"* |
| `update_config` | Update a single property in a config file | *"Set ssm.enabled to true on abc123"* |
| `get_logs` | Tail log files with optional grep filtering | *"Last 200 ERROR lines from catalina on xyz789"* |
| `yab_run` | Execute yab subcommands (status, restart, backup, etc.) | *"Restart tomcat on cdb546"* |
| `run_command` | Run allowlisted read-only shell commands | *"Show disk usage on abc123"* |
| `check_connectivity` | Test if an environment is reachable via SSH | *"Can you reach als2moherp5wcy?"* |
| `get_version` | Show installed module versions | *"What version of webshell is on abc123?"* |

### Diagnostics & Health

| Tool | Description | Example prompt |
|---|---|---|
| `health_check` | Composite health dashboard: disk, memory, processes, OOM activity | *"Is abc123 healthy?"* |
| `service_status` | Per-service status with PID, RSS, uptime, and port bindings | *"What services are running on xyz789?"* |
| `tail_live_errors` | Grep for errors across all Tomcat logs in one call | *"Show recent errors on abc123"* |
| `thread_dump` | Capture a JVM thread dump via SIGQUIT (non-destructive) | *"Get a thread dump from tomcat-qxtend on xyz789"* |
| `database_status` | Progress OpenEdge DB processes, connection counts, AppServer | *"Check the database on abc123"* |
| `pool_config_tuner` | Analyze Hikari pool settings and flag under-provisioned pools | *"Are the connection pool settings OK on abc123?"* |

### Configuration & Versions

| Tool | Description | Example prompt |
|---|---|---|
| `compare_configs` | Diff a config file between two environments | *"What's different in qracore config between abc123 and xyz789?"* |
| `compare_versions` | Version matrix across 2+ environments | *"Compare module versions on abc123, xyz789, and def456"* |

### Disk & Backups

| Tool | Description | Example prompt |
|---|---|---|
| `disk_cleanup` | Identify disk usage hotspots (logs, backups, temp) | *"What's eating disk on abc123?"* |
| `backup_info` | List available backups with timestamps and sizes | *"What backups exist on xyz789?"* |

### Registry

| Tool | Description | Example prompt |
|---|---|---|
| `register_environment` | Register an environment with optional alias/tags | *"Register abc123 as my-staging-env"* |
| `unregister_environment` | Remove an environment from the registry | *"Forget abc123"* |
| `add_alias` | Add a friendly alias to a registered environment | *"Alias abc123 as prod-demo"* |
| `list_environments` | List registered environments, optionally filtered by tag | *"List all staging environments"* |
| `search_environments` | Search by keyword across ID, aliases, tags, owner | *"Find environments owned by maxim"* |

### yab Subcommands

**Safe (no confirmation needed):** `status`, `version`, `list`, `help`, `info`

**Destructive (LLM will confirm first):** `restart`, `stop`, `start`, `update`, `install`, `backup`, `restore`, `deploy`

### Allowed Shell Commands

`cat`, `date`, `df`, `du`, `find`, `free`, `grep`, `head`, `hostname`, `java`, `ls`, `ps`, `systemctl`, `tail`, `top`, `uptime`, `wc`, `whoami`

## Environment Naming

Environments follow the pattern `{env_id}.environments.qad.com`. You can use either:

- Just the ID: `als2moherp5wcy`
- Full hostname: `als2moherp5wcy.environments.qad.com`

## Adding New Config/Log Files

Edit `src/qad_env_mcp/paths.py` to add aliases:

```python
LOG_ALIASES = {
    "catalina": "servers/tomcat-webui/logs/catalina.out",
    "access": "servers/tomcat-webui/logs/localhost_access_log.txt",  # new
}

CONFIG_ALIASES = {
    "main": "servers/tomcat-webui/.../qad-qracore.properties",
    "logging": "servers/tomcat-webui/.../logback.xml",  # new
}
```

## Extending with New Tools

Add new tools in `server.py` using the `@mcp.tool()` decorator:

```python
@mcp.tool()
async def my_new_tool(env_id: str, param: str) -> str:
    """Description shown to the LLM.

    Args:
        env_id: Environment identifier
        param: What this parameter does
    """
    env_id = _validate_env_id(env_id)
    result = await ssh.run_checked(env_id, f"some-command {param}")
    return result.stdout
```

## Troubleshooting

**"Cannot reach environment"** — Make sure your VPN is connected. Try `ssh $QAD_SSH_USERNAME@{env_id}.environments.qad.com` manually.

**"Command timed out"** — Some yab operations (update, backup) can take several minutes. The timeout is 5 minutes for these. If you need longer, adjust `command_timeout` in `SSHManager`.

**Tool not appearing in client** — Restart the MCP client after config changes. Check logs in stderr for startup errors.
