Two MCP servers — **qad-env** and **qfm** — let you manage QAD Linux environments and feature flags
directly from Claude Desktop using natural language. No SSH, no properties files, no yab commands to
memorise.

The clips below show real prompts against a live environment. Each scenario covers a task that comes
up regularly during development.

## Clip 01 — Environment status at a glance

Before starting work (or sharing an environment with a teammate), it is useful to know whether all
59 services are running and whether disk is under control. Normally this means SSH-ing in and running
yab status, then separately checking df -h on the right mount point.
With qad-env you ask in plain English and get a structured answer back immediately — service-by-service
status across every Tomcat, database, PAS appserver, Kafka, Elasticsearch, and more, followed by a disk
breakdown showing where space is being used (tomcat-webui/catalina.out was 61 MB on the recorded run,
nifi/work at 848 MB). If anything is wrong, the follow-up is one more prompt.
Prompt used in the clip

<video src="clips/register_health.mp4" controls width="800"></video>

## Clip 02 — Backup the database before a risky change

Any time you are about to touch configuration or run a destructive yab command, the right habit is to
take a database snapshot first. Without tooling, this means SSHing in, navigating to the systest root,
and running a yab backup command — then waiting and hoping the output confirms success.

With `qad-env` you start the backup with one prompt, get confirmation, then move straight into the config
change and restart in the same conversation. The whole workflow — backup, change, restart — stays in one
place with a readable audit trail.