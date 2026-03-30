"""
SSH connection manager with connection pooling.

Manages asyncssh connections to QAD environments, reusing connections
where possible and handling reconnection transparently.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import asyncssh

from .paths import SYSTEST_ROOT, resolve_hostname

logger = logging.getLogger(__name__)

# How long to keep idle connections alive (seconds)
CONNECTION_TTL = 300  # 5 minutes
# Maximum concurrent connections
MAX_CONNECTIONS = 10


@dataclass
class CommandResult:
    """Result of a remote command execution."""

    stdout: str
    stderr: str
    exit_code: int | None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def raise_on_error(self, context: str = "Command") -> None:
        if not self.ok:
            detail = self.stderr.strip() or self.stdout.strip() or "(no output)"
            raise RuntimeError(
                f"{context} failed (exit {self.exit_code}): {detail}"
            )


@dataclass
class _PoolEntry:
    conn: asyncssh.SSHClientConnection
    last_used: float = field(default_factory=time.monotonic)


class SSHManager:
    """Manages pooled SSH connections to QAD environments.

    Usage:
        mgr = SSHManager(username="mfg", password="qad")
        result = await mgr.run("als2moherp5wcy", "yab status")
        await mgr.close()
    """

    def __init__(
        self,
        username: str,
        password: str,
        port: int = 22,
        connect_timeout: float = 15.0,
        command_timeout: float = 120.0,
    ):
        self._username = username
        self._password = password
        self._port = port
        self._connect_timeout = connect_timeout
        self._command_timeout = command_timeout
        self._pool: dict[str, _PoolEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._semaphore = asyncio.Semaphore(MAX_CONNECTIONS)

    async def _get_connection(self, env_id: str) -> asyncssh.SSHClientConnection:
        """Get or create a pooled SSH connection for an environment."""
        hostname = resolve_hostname(env_id)

        if hostname not in self._locks:
            self._locks[hostname] = asyncio.Lock()

        async with self._locks[hostname]:
            # Check if we have a live cached connection
            entry = self._pool.get(hostname)
            if entry is not None:
                try:
                    # Quick liveness check — run a no-op
                    await asyncio.wait_for(
                        entry.conn.run("true", check=True),
                        timeout=5.0,
                    )
                    entry.last_used = time.monotonic()
                    return entry.conn
                except Exception:
                    logger.debug("Stale connection to %s, reconnecting", hostname)
                    try:
                        entry.conn.close()
                    except Exception:
                        pass
                    del self._pool[hostname]

            # Create new connection
            logger.info("Connecting to %s@%s:%d", self._username, hostname, self._port)
            conn = await asyncio.wait_for(
                asyncssh.connect(
                    hostname,
                    port=self._port,
                    username=self._username,
                    password=self._password,
                    known_hosts=None,  # QAD internal envs — skip host key verification
                ),
                timeout=self._connect_timeout,
            )
            self._pool[hostname] = _PoolEntry(conn=conn)
            return conn

    async def run(
        self,
        env_id: str,
        command: str,
        *,
        timeout: float | None = None,
        cwd: str | None = None,
    ) -> CommandResult:
        """Execute a command on a QAD environment via SSH.

        Commands are automatically prefixed with `cd /dr01/qadapps/systest`
        unless a different cwd is specified.

        Args:
            env_id: Environment identifier (e.g. 'als2moherp5wcy')
            command: Shell command to execute
            timeout: Override the default command timeout
            cwd: Working directory (defaults to SYSTEST_ROOT)
        """
        effective_cwd = cwd or SYSTEST_ROOT
        # Source the login profile so PATH and aliases are available
        # (SSH non-interactive sessions skip .bashrc/.profile).
        # Then cd to the working directory so yab and relative paths work.
        full_command = (
            f"source /etc/profile 2>/dev/null; source ~/.bash_profile 2>/dev/null; "
            f"source ~/.bashrc 2>/dev/null; "
            f"cd {effective_cwd} && {command}"
        )

        async with self._semaphore:
            conn = await self._get_connection(env_id)
            effective_timeout = timeout or self._command_timeout

            try:
                result = await asyncio.wait_for(
                    conn.run(full_command),
                    timeout=effective_timeout,
                )
                return CommandResult(
                    stdout=result.stdout or "",
                    stderr=result.stderr or "",
                    exit_code=result.exit_status,
                )
            except asyncio.TimeoutError:
                return CommandResult(
                    stdout="",
                    stderr=f"Command timed out after {effective_timeout}s",
                    exit_code=-1,
                )

    async def run_checked(
        self, env_id: str, command: str, **kwargs
    ) -> CommandResult:
        """Like run(), but raises RuntimeError on non-zero exit."""
        result = await self.run(env_id, command, **kwargs)
        result.raise_on_error(f"Command on {env_id}")
        return result

    async def close(self) -> None:
        """Close all pooled connections."""
        for hostname, entry in self._pool.items():
            try:
                entry.conn.close()
                logger.debug("Closed connection to %s", hostname)
            except Exception:
                pass
        self._pool.clear()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()
