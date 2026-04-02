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
LIB_ABS = f"{SYSTEST_ROOT}/{LIB_DIR}"

# Prefix for QAD application JARs (vs. third-party libraries)
QAD_JAR_PREFIX = "qad-"

# Log files (relative to SYSTEST_ROOT)
LOG_DIR = "servers/tomcat-webui/logs"
CATALINA_LOG = f"{LOG_DIR}/catalina.out"

# Additional Tomcat log files
QXTEND_LOG = "servers/tomcat-qxtend/logs/catalina.out"
EVENTSERVICE_LOG = "servers/tomcat-eventservice/logs/catalina.out"
DEFAULT_LOG = "servers/tomcat-default/logs/catalina.out"

# CT Log (Financials BL debugging) — configuration.properties in build/config
BUILD_CONFIG = "build/config/configuration.properties"
BUILD_CONFIG_ABS = f"{SYSTEST_ROOT}/{BUILD_CONFIG}"

# CT log property keys in configuration.properties
CT_LOG_LEVEL_PROP = "fin.cbserverxml.debug-level"
CT_LOG_DIR_PROP = "fin.cbserverxml.debug-directory"
CT_LOG_LOGIN_PROP = "fin.cbserverxml.debug-login"

# Default CT log output directory (relative to SYSTEST_ROOT)
CT_LOG_DEFAULT_DIR = "build/logs"

# Possible cbserver.xml locations (relative to SYSTEST_ROOT), checked in order
CBSERVER_XML_PATHS = [
    "build/work/config/cbserver.xml",
    "config/cbserver.xml",
    "config/fin/local/cbserver.xml",
]

# Backup directory
BACKUP_DIR = "backup"

# YAB log directory
YAB_LOG_DIR = "log"

# All Tomcat service names mapped to their log paths and process identifiers
TOMCAT_SERVICES: dict[str, dict[str, str]] = {
    "tomcat-webui": {
        "log": CATALINA_LOG,
        "grep": "tomcat-webui",
    },
    "tomcat-qxtend": {
        "log": QXTEND_LOG,
        "grep": "tomcat-qxtend",
    },
    "tomcat-eventservice": {
        "log": EVENTSERVICE_LOG,
        "grep": "tomcat-eventservice",
    },
    "tomcat-default": {
        "log": DEFAULT_LOG,
        "grep": "tomcat-default",
    },
}

# Well-known log files that can be requested by short name
LOG_ALIASES: dict[str, str] = {
    "catalina": CATALINA_LOG,
    "catalina.out": CATALINA_LOG,
    "webui": CATALINA_LOG,
    "qxtend": QXTEND_LOG,
    "eventservice": EVENTSERVICE_LOG,
    "default": DEFAULT_LOG,
}

# Well-known config files that can be requested by short name
CONFIG_ALIASES: dict[str, str] = {
    "main": MAIN_CONFIG,
    "qracore": MAIN_CONFIG,
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
