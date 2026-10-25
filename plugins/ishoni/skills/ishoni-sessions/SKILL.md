---
name: ishoni-sessions
description: Show which of the user's machines and agents are online and which resident session (name and exact id) handles each agent's Ishoni messages, and bind an agent to the user's existing conversation without creating a duplicate. Use when the user asks "which agents are online", "which session handles messages", "where do my Ishoni messages go", or wants to name or find the fixed conversation for an agent.
---

# Ishoni: agents, sessions and where messages go

## Run the Ishoni command

The command ships in this plugin: `scripts/ishoni`, two folders above this file
(`<folder of this SKILL.md>/../../scripts/ishoni`). Use its quoted absolute path with `sh`; below it is
written `Y`. Windows: `py -3 "<plugin>\scripts\ishoni.py"`. A non-default data home goes before the
command: `sh Y --home DIR agents`. If it says "not set up", use the ishoni-setup skill.

## Which machines and agents are online

    sh Y agents --online          # `sh Y peers` is the same command; without --online it also lists
                                  # offline machines; add --json for structured output

All read-only. For every machine: Tailscale online/offline, whether its receiver answered, whether
this machine may send there, its Ishoni version, and per agent the session that handles it:

- `name "..." (native codex session name, read <time>)`: the session's real title, read by that
  machine from its own runtime (Codex `Thread.name`) without opening a turn. `cached` / `stale` say
  how fresh it is; a rename shows up within about five minutes. `lookup in progress`: ask again.
- `confirmed title "..."`: what the user confirmed when binding; it may be outdated.
- `alias "..."`: a local label configured on that machine, not the session's own name.
- `id ...`: the exact session id. `fixed Codex bridge dispatcher` means the installed bridge
  delivers there; `configured binding` means it was named with `session bind`.
- `busy/idle: unknown`: Ishoni never claims an agent is awake, busy or idle.
- `no session bound`: no session id was recorded; its wake adapter (if any) still applies.

When you answer the user, give the name with its source and always the exact id, so they can find
the right conversation among many. Never present an alias or confirmed title as the native name.

Receiver states: `ok` (answered /info), `legacy` (an older Ishoni without /info: version, agents and
sessions unknown, not offline), `blocked` (403: its access policy denies this machine; policy, not a
network failure), `unreachable` (no answer in time: not running, another port, ACL or shields-up;
not proof that Ishoni is missing), `offline` (Tailscale says so). Peers only reveal this to machines
permitted by their access policy. New installs default to `tailnet`; legacy allowlists remain until
an authorized migration, and deny rules take precedence.

## Bind an agent to its existing session

Each agent should point at the one conversation the user already uses. Never create, resume, fork
or rename a session to do this, and never pick one by its name:

1. Ask the user which existing conversation handles messages for the agent, and for its exact id
   (for Codex the session UUID, from that session's own details). If you are running inside that
   very session and the host shows you its id, confirm it with the user.
2. Codex with the bridge installed: the bridge's dispatcher UUID is already reported automatically;
   only add an alias if wanted: `sh Y session bind --agent codex --display-name "Inbox dispatcher"`.
   A different UUID is refused: the bridge's session changes only through `codex-bridge` after its
   queue is drained.
3. Other agents (for example a Grok Bot chat or routine):

       sh Y session bind --agent grok --runtime grok --session-id <exact id> --display-name "Research desk"

   Add `--confirmed-title "..."` only with the title exactly as the user confirmed it; runtimes
   without a read-only lookup show it labeled as confirmed by the user.
4. Review the dry run, then repeat with `--apply` (sessions.json is backed up). Replacing a different
   existing id needs `--replace` and the user's explicit request. `sh Y session list` checks it;
   `session unbind --agent NAME --apply` removes a binding.

Binding names the session; it does not wake it. How the agent is woken is configured separately
(Codex bridge or `wake-config`, see the ishoni-wake skill).
