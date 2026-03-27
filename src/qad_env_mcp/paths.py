"""
QAD environment path constants and hostname resolution.

All QAD environments share the same directory layout rooted at SYSTEST_ROOT.
Environment hostnames follow the pattern: {env_id}.environments.qad.com
"""

# Root directory — all yab commands must be run from here
SYSTEST_ROOT = "/dr01/qadapps/systest"

# Configuration files (relative to SYSTEST_ROOT)
CONFIG_DIR = "servers/tomcat-webui/webapps/qad-central/WEB-INF/config"
MAIN_CONFIG = f"{CONFIG_DIR}/qad-qracore.properties"

# Deployed WAR lib directory — JAR filenames encode module versions
LIB_DIR = "servers/tomcat-webui/webapps/qad-central/WEB-INF/lib"

# Log files (relative to SYSTEST_ROOT)
LOG_DIR = "servers/tomcat-webui/logs"
CATALINA_LOG = f"{LOG_DIR}/catalina.out"

# Well-known log files that can be requested by short name
LOG_ALIASES: dict[str, str] = {
    "catalina": CATALINA_LOG,
    "catalina.out": CATALINA_LOG,
    # Add more as you discover them:
    # "access": f"{LOG_DIR}/localhost_access_log.txt",
    # "gc": f"{LOG_DIR}/gc.log",
}

# Well-known config files that can be requested by short name
CONFIG_ALIASES: dict[str, str] = {
    "main": MAIN_CONFIG,
    "qracore": MAIN_CONFIG,
    # Add more as you discover them:
    # "logging": f"{CONFIG_DIR}/logback.xml",
}

DNS_SUFFIX = "environments.qad.com"


def resolve_hostname(env_id: str) -> str:
    """Convert an environment ID to a fully qualified hostname.

    Accepts both bare IDs ('als2moherp5wcy') and full hostnames
    ('als2moherp5wcy.environments.qad.com').
    """
    if env_id.endswith(f".{DNS_SUFFIX}"):
        return env_id
    return f"{env_id}.{DNS_SUFFIX}"


def resolve_log_path(name_or_path: str) -> str:
    """Resolve a log name alias or return the path as-is if not aliased."""
    return LOG_ALIASES.get(name_or_path.lower(), name_or_path)


def resolve_config_path(name_or_path: str) -> str:
    """Resolve a config name alias or return the path as-is if not aliased."""
    return CONFIG_ALIASES.get(name_or_path.lower(), name_or_path)
