---
name: minimal-ai-agent-design
description: >
  Design patterns and architecture for AI agent systems — personal assistants, multi-tenant bots, sandboxed agent execution, and AI-native applications. Use this skill whenever the user is designing or building: a personal AI assistant or bot, a system where AI agents execute code, a multi-tenant AI application, an AI-powered automation system, a conversational agent with persistent memory, or any system that routes messages to AI agents. Also applies when discussing security isolation for agents, extensibility patterns, or AI-native operational philosophy. Trigger on keywords like "AI assistant", "agent system", "bot architecture", "sandbox agents", "multi-tenant AI", "agent isolation", "personal assistant", "AI integration", or when the user asks how to design something "like NanoClaw".
---

# Minimal AI Agent Design Patterns

Architecture principles for building AI agent systems that are secure, understandable, and built to last. These patterns are distilled from production AI assistant systems and apply regardless of language, framework, or platform.

---

## Philosophy: The Three Constraints

Before any technical decision, establish these constraints:

**1. Small enough to understand completely.**
If a collaborator can't read the entire codebase in an afternoon, it's too big. Resist the urge to add abstraction layers, framework wrappers, or microservices. Every file should have an obvious purpose. When you feel the pull to "architect" something, ask: is this complexity serving the user, or just making the system feel more serious?

**2. Secure by construction, not by policy.**
Application-level permission systems (allowlists, capability flags, pairing codes) are fragile. They fail when someone clever bypasses them. True security comes from OS-level isolation: the agent literally cannot access what isn't mounted/given to it. If it can't see the file, it can't read the file—no policy required.

**3. Bespoke over generic.**
A system trying to support every user's use case simultaneously ends up being a bloated compromise for everyone. Build for the actual user with their actual needs. When someone else wants different behavior, they fork and modify—they don't configure. This only works if the codebase is small enough to safely modify.

---

## Pattern 1: Minimal Surface Area

**Problem:** Codebases grow. Each new feature adds complexity, dependencies, and failure modes.

**Pattern:** Enforce a "one process, handful of files" rule. Assign each file exactly one responsibility. Before adding anything, ask whether it's truly needed or just "nice to have."

**Implementation signals:**
- If you're adding a 4th process, stop and reconsider
- If a config file is growing past ~20 lines, it's probably doing too much
- If you're importing a framework to solve a 50-line problem, solve it in 50 lines

**Anti-patterns to avoid:**
- Microservices for a single-user system
- Message queues when a database + polling loop suffices
- Plugin architectures with registries and lifecycle hooks when simple function calls work

---

## Pattern 2: OS-Level Isolation

**Problem:** Agents that can run arbitrary code need to be sandboxed. Application-level permission systems are a false sense of security.

**Pattern:** Each agent execution runs inside an OS-level sandbox (container, VM, subprocess with restricted filesystem). The sandbox only sees what is explicitly given to it. There is no permission system to bypass—the files simply don't exist inside the sandbox.

**Key decisions:**
- Mount only the directories the agent genuinely needs
- Mount read-only anything the agent should be able to read but not modify
- Never mount the entire host filesystem
- Source code of the host process should be read-only (or not mounted at all) to prevent agents from modifying the system that runs them

**Per-tenant isolation:** Each tenant (user, group, conversation) gets its own isolated sandbox instance. Tenant A cannot access Tenant B's filesystem by design, not by policy.

**Why this matters:** An agent with Bash access in a container is safe. The same agent with Bash access on your host machine is not. The container is the security model, not a permission flag.

---

## Pattern 3: Secrets via Ephemeral Channel

**Problem:** API keys and credentials must reach the agent without being persisted to disk, logged, or visible to subprocesses.

**Pattern:** Pass secrets via stdin (or equivalent ephemeral channel) immediately before execution. Delete them from the input structure before logging. Strip them from any subprocess environment the agent's tools spawn.

**Implementation:**
```
host → stdin → agent (reads, holds in memory only)
                    ↓
            agent spawns tool subprocess
                    ↓
            strip secrets from subprocess env
```

**Never:**
- Write secrets to mounted files
- Pass secrets as environment variables mounted from the host
- Log input structures that contain secrets

**Hook pattern:** If the agent framework supports pre-tool hooks, use them to strip credentials from subprocess environments before tool execution runs.

---

## Pattern 4: Store-then-Poll Message Loop

**Problem:** The path that receives messages and the path that processes them have different performance and reliability requirements. Blocking one on the other causes dropped messages and timeout cascades.

**Pattern:** Decouple receive from process with a durable store in between.

```
RECEIVE PATH (fast, never blocks):
  incoming message → store to durable DB → return immediately

PROCESS PATH (slow, can fail):
  poll DB for new messages → process batch → advance cursor → repeat
```

**Two cursors:** Maintain two independent position markers:
- **Received cursor**: "what's the latest message we've acknowledged from the source"
- **Processed cursor**: "what's the latest message the agent has actually acted on"

These advance independently. On crash, messages between them are recovered automatically on restart.

**Cursor rollback:** If processing fails _before_ sending output to the user, roll back the processed cursor so the messages will be retried. If processing fails _after_ sending output, do NOT roll back—you'd send duplicates.

---

## Pattern 5: Per-Tenant Queue with Global Concurrency Cap

**Problem:** Multiple tenants sending messages simultaneously. Each tenant needs serialized processing (messages in order), but tenants shouldn't starve each other.

**Pattern:** Per-tenant FIFO queue + global cap on total concurrent active workers.

```
Tenant A: [msg1, msg2, msg3] → worker (one at a time per tenant)
Tenant B: [msg4]             → worker (concurrent with A)
Tenant C: [msg5, msg6]       → WAITING (global cap reached)
```

**Key behaviors:**
- While a worker is active for a tenant, new messages queue (don't spawn a second worker)
- If an active worker can accept new messages via IPC, pipe to it instead of queueing
- When a worker finishes, drain the tenant's queue before accepting work from waiting tenants
- Prioritize scheduled tasks over interactive messages in the drain order (tasks won't be re-discovered from DB; messages will)

**Exponential backoff retry:** On processing failure, schedule retry with exponential backoff. Cap retries. After max retries, drop and wait for next incoming message to re-trigger.

---

## Pattern 6: Filesystem IPC

**Problem:** The host process needs to communicate bidirectionally with agents running in sandboxes. Network sockets require port management; stdin/stdout are one-time pipes.

**Pattern:** Use the shared filesystem (via mounts) as the IPC channel. Both sides poll a shared directory.

```
Outbound (host → agent):
  host writes JSON file to /ipc/input/
  agent polls directory every N ms
  agent reads and deletes the file

Inbound (agent → host):
  agent writes JSON file to /ipc/messages/ or /ipc/tasks/
  host polls directory every N ms
  host reads, processes, and deletes the file
```

**Atomic writes:** Always write to a `.tmp` file then `rename` to the final name. Rename is atomic on POSIX filesystems—the reader never sees a partial write.

**Per-tenant namespacing:** Give each tenant its own IPC directory. This prevents cross-tenant messaging and allows identity-by-path (see Pattern 7).

**Sentinel files:** Use named files (not content) as signals. `_close` means "wind down." Checking for file existence is cheaper and more reliable than parsing content.

---

## Pattern 7: Identity by Path

**Problem:** In IPC, an agent could claim to be a different tenant to escalate privileges.

**Pattern:** Never trust identity claims in message content. Determine identity by _which directory_ the message came from. The host controls directory creation and mounting—agents cannot forge their origin path.

```
/ipc/tenant-a/tasks/register.json  →  identity = tenant-a (verified)
/ipc/tenant-b/tasks/register.json  →  identity = tenant-b (verified)
```

**Authorization tiers:** Designate one tenant as "admin" (e.g., the self-chat). Only the admin can perform privileged operations (registering other tenants, cross-tenant task scheduling). Verify at path, not at content.

**Validation rule:** If the message says "I am tenant X" but came from `/ipc/tenant-b/`, reject it. The source path is authoritative.

---

## Pattern 8: Sentinel-based Output Parsing

**Problem:** Streamed output mixes log lines, debug output, and structured results. Parsing the "last line" or parsing by line number is fragile.

**Pattern:** Wrap structured output in unique sentinel markers. Parse by marker, not by position.

```
[log noise]
---OUTPUT_START---
{"status":"success","result":"..."}
---OUTPUT_END---
[more log noise]
```

**Stream parsing:** Don't wait for the process to end. Accumulate a parse buffer, scan for `START`, then scan forward for `END`. When found, extract and process the JSON. Clear the consumed portion. This enables real-time streaming of results.

**Multiple results:** A single execution can emit multiple START/END pairs (e.g., agent teams). Treat each as an independent result.

---

## Pattern 9: Activity-Aware Timeouts

**Problem:** Agents doing complex, long-running work need hard timeouts, but idle agents should be reaped gracefully without treating it as an error.

**Pattern:** Distinguish between two timeout behaviors:

```
Hard timeout:
  Kill container if no meaningful output arrives within N minutes
  Used when agent appears stuck with no output

Activity reset:
  Each time meaningful output arrives, reset the timer
  The clock measures "time since last output," not "total execution time"

Idle cleanup:
  Container times out AFTER producing output = normal idle cleanup
  Container times out WITHOUT producing output = error
```

**Grace period:** Hard timeout must be at least `idle_timeout + buffer` so the container's graceful wind-down has time to complete before the kill fires.

**Graceful stop first, force kill second:** On timeout, send a graceful stop signal (SIGTERM, container stop). Wait N seconds. Only then force kill. This allows in-progress work to checkpoint.

---

## Pattern 10: Skills over Features

**Problem:** Different users want different features. Adding everything to the core creates a bloated system no one fully understands. Keeping everything out makes the system too bare.

**Pattern:** The core does the minimum. Extensions are transformation scripts that modify a fork to add a capability. Each user ends up with exactly what they need.

```
Core repo (minimal, stable):
  - Message routing
  - Agent execution
  - Storage

Skill: /add-telegram
  Reads existing code → modifies it → result: clean Telegram integration
  Not: "add Telegram config option" → detect at runtime

Skill: /add-email
  Same pattern: transform the code, not configure it
```

**Why this works:** The skill runs once (during customization), modifies the codebase, and then disappears. The resulting codebase has no trace of the skill—just the feature, cleanly integrated. No feature flags, no runtime detection, no configuration sprawl.

**What goes in core:** Only things every user needs: message routing, agent execution, persistence, core security. When in doubt, leave it out.

---

## Pattern 11: AI-Native Operations

**Problem:** Traditional software needs monitoring dashboards, setup wizards, debugging tools, and configuration UIs. These are expensive to build and maintain.

**Pattern:** Replace operational tools with AI interface. The codebase is designed to be read and modified by an AI collaborator, not by a configuration system.

**Practical implications:**
- No setup wizard → AI guides setup via conversation
- No monitoring dashboard → ask the AI "what's happening?"
- No debugging tools → describe the problem, AI reads logs and fixes it
- No config UI → tell the AI what behavior you want, it modifies the code

**Design for AI readability:** Code should be honest and readable rather than abstracted and "defensive." An AI collaborator doesn't need the code to handle every edge case—it can help fix edge cases when they arise. This is a genuine trade-off: less defensive code that's easier to understand vs. more defensive code that's harder to modify.

**Logging for AI:** Structure logs so an AI can diagnose from them. Include context (group, operation, state before/after). Don't log everything—log the things that distinguish "working normally" from "something went wrong."

---

## Pattern 12: Code as Configuration

**Problem:** Configuration files accumulate. Each option adds complexity, documentation burden, and combinations that need testing.

**Pattern:** Only make things configurable if users genuinely need different values day-to-day. Everything else is a code change.

**Configurable:** Things that vary per deployment (trigger word, API keys, ports). These go in environment variables or a minimal config file.

**Not configurable:** Architectural decisions (how messages are processed, what tools agents have, how isolation works). These are code. If a user wants them different, they fork and change the code.

**The test:** "Would two different installations of this software ever need different values here at the same time?" If yes, make it configurable. If no, hardcode it and document why.

---

## Putting It Together: Startup Sequence

A minimal AI agent system typically starts in this order:

1. **Verify isolation runtime** (containers, VMs) — fail fast if unavailable
2. **Initialize durable storage** (DB, filesystem structure)
3. **Load state** (cursors, registered tenants, active sessions)
4. **Register shutdown handlers** — graceful, not force-kill
5. **Connect I/O channel** (messaging platform, webhook, socket)
6. **Start background workers** (scheduler, IPC watcher)
7. **Run startup recovery** — find messages received but not processed
8. **Start main processing loop**

---

## Anti-Pattern Reference

| Anti-Pattern | Problem | Pattern Instead |
|---|---|---|
| Application-level permission checks | Bypassable, complex to maintain | OS-level isolation (Pattern 2) |
| Secrets in env vars or files | Logged, persistent, visible to subprocesses | Ephemeral stdin channel (Pattern 3) |
| Blocking receive on processing | Dropped messages, cascading timeouts | Store-then-poll (Pattern 4) |
| One global queue | Fast tenants starve slow ones | Per-tenant queues (Pattern 5) |
| Network IPC between sandbox and host | Port management, auth complexity | Filesystem IPC (Pattern 6) |
| Identity from message content | Forgeable, privilege escalation | Identity by path (Pattern 7) |
| Parse output by line number | Fragile with log noise | Sentinel markers (Pattern 8) |
| Fixed execution timeout | Kills long-running work prematurely | Activity-aware timeout (Pattern 9) |
| Feature flags for variants | Runtime complexity, combinatorial testing | Transformation skills (Pattern 10) |
| Config files for everything | Config sprawl, documentation burden | Code as config (Pattern 12) |
