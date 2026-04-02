# YAB Database Backup & Restore — Reference Guide

Compiled from QAD Confluence documentation across YAB, QSM, and QEP spaces.

## Overview

YAB provides tiered backup/restore commands. **Environment-level** commands handle the
full stack (Progress DBs, Mongo, files, event service cleanup). **Database-level** commands
target only the Progress databases.

All backups are stored locally under the directory configured by `dbbackup.dir`.

## Backup Commands

| Command | Scope | Behaviour |
|---------|-------|-----------|
| `yab environment-offline-backup` | Full environment | Stops env, backs up everything, restarts. **Recommended for consistency.** |
| `yab environment-online-backup` | Full environment | Backs up without full stop. May briefly take individual components offline. |
| `yab database-all-backup` | All Progress DBs | No environment lifecycle. |
| `yab database-backup` | Progress DBs (EE) | Older EE-only variant of database-all-backup. |

### Tag & Timestamp Behaviour

Controlled by `dbbackup.timestamp` (default: `true`):

- **`dbbackup.timestamp=true`**: Backup subdirectories are auto-named with timestamps
  (`YYYYMMDDHHMMSS`). The `-tag` option is ignored. Use `dbbackup.timestampmax` to
  limit the number of retained backups (oldest is discarded).

- **`dbbackup.timestamp=false`**: The `-tag:NAME` option controls the subdirectory name.
  Without `-tag`, backups go to a subdirectory named `default` and **overwrite** any
  existing backup there.

Example with tag:
```
yab database-backup -tag:20260330pre-upgrade
```

### Compression

Controlled by `dbbackup.compress` (default: `true`):

- On non-Windows, backup is compressed as written (saves disk, increases backup time).
- `dbbackup.delaycompression=true` — compress after backup completes (faster backup,
  needs more temporary disk).
- `dbbackup.compress=false` — no compression.

### Listing Backups

| Command | What it lists |
|---------|---------------|
| `yab environment-backup-list` | Full environment backups |
| `yab database-all-backup-list` | All Progress DB backups |
| `yab database-backup-list` | Progress DB backups (EE) |

### Cleaning Up

```
yab database-all-backup-remove   # removes all Progress DB backups
```

Verify removal with `database-all-backup-list` and check `dbbackup.dir` on disk.

## Restore Commands

| Command | Scope | Pre-requisite |
|---------|-------|---------------|
| `yab environment-restore` | Full environment | Handles stop/start automatically |
| `yab database-all-restore` | All Progress DBs | **`yab stop` first** |
| `yab database-restore` | Progress DBs (EE) | **`yab stop` first** |

Restore from a specific tag:
```
yab database-restore -tag:20260330pre-upgrade
```

Without `-tag`, restores from the `default` subdirectory.

### Cross-Environment Restore

To restore one environment's backup into a second:

1. Both environments must have the **same EE version / PC 4.0 recipe**.
2. Find backup dir: `yab config dbbackup.dir`
3. Copy backup files: `scp -r <env1-backup-dir>/default mfg@<env2>:<env2-backup-dir>/`
4. On env2: `yab stop && yab environment-restore && yab start`

### Post-Restore Validation

1. Check database file timestamps: `ll databases/*.db`
2. `yab start` followed by `yab status` — all processes should show `STARTED/OK`.

## Known Issues

### AB-21244: `yab update` after restore reloads system data

**Symptoms**: After `yab database-restore` + `yab update`, QAD browse customisations
and Reporting Framework changes are deleted/overwritten.

**Root cause**: Database restore resets an internal ID. When `yab update` runs, processes
that update the database see the reset ID and redo their work from scratch.

**Workaround**: Use `prorest <database> <backup file>` manually instead of `yab
database-restore` to avoid resetting the internal ID. Tracked in RFRD-7591.

**Affected commands**: `database-restore`, `database-all-restore`,
`database-qadadm-restore`, and all individual `database-*-restore` variants.

### AC-11101: Don't run `yab reconfigure` during backups

Running `yab reconfigure` while an online or offline backup is in progress crashes
the backup and can take down the mfgdb. Progress (Case 01554828) confirmed this is
unsupported.

## MCP Tool Mapping

The qad-env MCP server maps these yab commands to dedicated tools:

| MCP Tool | Yab Commands | Notes |
|----------|-------------|-------|
| `database_backup` | `environment-offline-backup`, `environment-online-backup`, `database-all-backup`, `database-backup` | Supports `tag` param. Runs in background via nohup. |
| `database_restore` | `environment-restore`, `database-all-restore`, `database-restore` | Supports `tag` param. Checks env is stopped for DB-only methods. Warns about AB-21244. |
| `database_backup_manage` | `database-all-backup-list`, `environment-backup-list`, `database-backup-list`, `database-all-backup-remove` | Use `scope` param to select list variant. |
| `backup_info` | (filesystem + yab config) | Shows `dbbackup.*` settings, backup files, sizes, ages. |

## Confluence Sources

| Page | Space | ID |
|------|-------|----|
| Environment Backup (YAB 1.19) | YABEE119 | 558799164 |
| YAB - Database Backup and Restore (Channel Islands) | YAB | 104433476 |
| YAB - Database Backup and Restore (EE) | YAB | 101978224 |
| I19 YAB Database Backup and Restore Instruction | I19TS | 216486328 |
| Partner - Environment Backup / Restore | QEP210 | 257385401 |
| AB-21244: database-restore + update reloads data | QSM | 224444810 |
| AC-11101: reconfigure crashes online backups | QSM | 493818404 |
