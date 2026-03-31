Two MCP servers — **qad-env-mcp** and **qfm-mcp** — let you manage QAD Linux environments and feature flags
directly from Claude Desktop using natural language. No SSH, no properties files, no yab commands to
memorize.

The clips below show real prompts against a live environment. Each scenario covers a task that comes
up regularly during development.

## Clip 01 — Environment status at a glance

**What this demonstrates**

Before starting work (or sharing an environment with a teammate), it is useful to know whether all
59 services are running and whether disk is under control. Normally this means SSH-ing in and running
yab status, then separately checking df -h on the right mount point.
With qad-env-mcp you ask in plain English and get a structured answer back immediately — service-by-service
status across every Tomcat, database, PAS appserver, Kafka, Elasticsearch, and more, followed by a disk
breakdown showing where space is being used (tomcat-webui/catalina.out was 61 MB on the recorded run,
nifi/work at 848 MB). If anything is wrong, the follow-up is one more prompt.
Prompt used in the clip

https://github.com/user-attachments/assets/08f0ddb5-d5c0-49a0-94c5-0ef5e814a01d

## Clip 02 — Backup the database before a risky change

**What this demonstrates**

Any time you are about to touch configuration or run a destructive yab command, the right habit is to
take a database snapshot first. Without tooling, this means SSHing in, navigating to the systest root,
and running a yab backup command — then waiting and hoping the output confirms success.

With `qad-env-mcp` you start the backup with one prompt, get confirmation, then move straight into the config
change and restart in the same conversation. The whole workflow — backup, change, restart — stays in one
place with a readable audit trail.

https://github.com/user-attachments/assets/bc223ca4-1da0-44f8-a795-1593429ee29a

## Clip 03 — Update the environment and verify versions

**What this demonstrates**

After a new build lands, the typical workflow is: run `yab update`, then check the deployed JAR
versions to confirm the right modules came through. If you share the environment with others, you also
want to know they are on the same versions — catching a mismatch before testing saves a lot of time.

This clip shows the full cycle: check current versions, trigger an update, then compare the result
against staging. The version matrix makes drift immediately visible — per module, per environment,
no manual grep-ing through `WEB-INF/lib`.

https://github.com/user-attachments/assets/8cb7ef92-bcbe-48df-8231-75568b947b85


## Clip 04 — Toggle a feature flag during development

**What this demonstrates**

Feature flags let you test a code path on your environment without it being visible to anyone else.
The old approach was editing an SSM parameter directly or changing a properties file and restarting.
With `qfm-mcp` you toggle a flag with a single prompt — no restart required, and it is trivially
reversible.

This clip shows the full iteration loop: check which flags are active, turn one on, verify the
behaviour in the browser, then turn it off again so teammates are unaffected. The flag used in the
recording is `menu-search-ranking`, which was `off` on `development` at the start of the clip.

https://github.com/user-attachments/assets/f8dd03eb-4f9a-4027-9172-9b180dcf283f

## Clip 05 — Investigate logs

When something looks wrong in the UI, or you want to understand what the environment is doing
after a restart or flag change, the first step is usually the logs. Without tooling this means
SSH-ing in and `tail`-ing the right `catalina.out` out of the dozen or so Tomcat instances — and
knowing which one to look at.

`tail_live_errors` greps for `ERROR`, `FATAL`, `Exception`, and `OutOfMemory` across all Tomcats
simultaneously and returns a unified timestamped feed. If the error scan comes back clean and you
want to dig deeper, `run_command` lets you tail or grep a specific log file directly. On the
recorded run, the error scan was clean, and the raw tail of `tomcat-webui/catalina.out` revealed
recurring `WARN` entries from `UrlUtil` (null business keys on entity notifications) alongside
`FeatureFlagService.refresh` lines confirming flag state was being picked up from SSM.

https://github.com/user-attachments/assets/20f2166a-c007-4ce3-a2bd-8525963341bf