# Large Project Framework

> A **multi-role collaborative development framework for AI coding agents**: 12 specialist roles share one workspace, work is driven by requirement cards, execution is orchestrated by an event loop, and everything is closed out with contracts plus machine checks.
> The problem it solves fits in one sentence: **let "one person + one AI session" run a large project that has real role separation, real acceptance evidence, and can survive interruption and resume.**

Current version: **v5.28.0** (the authoritative version is the frontmatter of `agent.md`)

[简体中文](README.md) | **English**

---

## What it is / what it is not

**What it is**

- A set of **Markdown instruction contracts** (orchestrator `agent.md` + 12 role skills in `skills/*/SKILL.md` + contracts and acceptance checklists)
- A set of **Python self-check scripts** (26 scripts + 23 regression tests, Python 3 standard library only)
- A set of **on-disk workspace conventions** (`project-state/`: contracts, requirement cards, test cards, memory, progress, issues, code)

**What it is not**

- It does not generate business code skeletons (what gets initialized is a **collaboration skeleton**, not a project scaffold)
- It is not CI and not a scheduler ("when to start the run" belongs to the host; the framework only defines start conditions, gates and wrap-up actions)
- It does not replace any **single-purpose execution skill** (deployment, publishing, image generation, copyright filings… each has its own manual — see [Working with independent skills](#working-with-independent-skills))
- It does not guarantee that requirements are sound or that evidence is true — what it guarantees is that **the process never silently skips a step, every artifact has a traceable origin, and every conclusion carries its scope**

---

## Core mechanics (this section alone explains what it does)

### 1. Cards are the unit of work

All work hangs on cards. **No card, no dispatch; no card, no acceptance.**

| Card | One directory | What is inside |
|---|---|---|
| **Requirement card** `需求卡/card-NNN/` | One requirement | `card.md` (req / AC / anchor / entry / status) + approach notes + acceptance record + `证据/` |
| **Test card** `测试卡/tc-NNN/` | One system-level testing activity | `tc.md` + coverage matrix + findings + `证据/` |
| **CR** `问题/` | One change request | reason for change, blast radius, approval record, execution evidence |

Requirement cards move through five states: `todo → in_progress → done (after developer self-check) → reviewing (orchestrator dispatches acceptance) → GO keeps done / NO-GO returns to in_progress`, with `blocked` when stuck. **A status is never just a claim**: setting `done` requires an on-disk record, and an acceptance record may not be an empty shell (`guide_audit.py` G03).

### 2. Orchestrator + 12 roles = layered context

- The **orchestrator** (`agent.md`) only orchestrates, adjudicates, dispatches and accepts — it **never writes business code itself**
- The **12 roles** each have their own skill and their own output location: Product Manager, Architect, UI Designer, Frontend, Backend, Data Engineer, Test Engineer, Security Auditor, Integration Engineer, Ops Engineer, Code Analyst, Project Manager
- Each role works only inside its own slice of context and writes into `project-state/`; the next slice is continued by the orchestrator through **contracts and cards**. The orchestrator does not read everything: it loads on demand in three layers — **L0 overview / L1 index / L2 body**

This addresses the three ways a single session dies on a large project: the context fills up, fixing A breaks B, and three days later nobody remembers why a decision was made.

### 3. Single source of truth + derived artifacts

- Semantic truth lives in `project-state/契约/` (PRD, architecture, interfaces, data model, permission matrix, security, design tokens, coding standards)
- `工作区地图.md`, `进度/*.json` and solution exports are **derived artifacts**: recomputable, **never hand-edited**, overwritten on refresh
- The tree is split into two layers: **user layer** (`交付/`, `方案/`, `工作区地图.md` — the things you read) and **framework layer** (`project-state/` — the workspace of the orchestrator and roles)

### 4. On disk means evidence; creator ≠ verifier

- **Work that never landed in the workspace did not happen**; a conversation is not a deliverable, and anything left in `tmp/` is not part of the evidence chain
- The person who writes a card cannot mark it `done` personally, and the implementer cannot also be the acceptor (not even as a dual role)
- Acceptance conclusions may only cite on-disk evidence; "should be fine" is not verification

### 5. Traceability, memory and self-improvement

- **Decisions are traceable**: key decisions are recorded ADR-style in `记忆/决策日志.md` (context / decision / rationale / alternatives / consequences). Old decisions are **never deleted** — a change adds a new entry and marks the old one Superseded
- **Friction gets registered**: when a user gets stuck, works around the process, or the same spot is revised repeatedly, it is logged on the spot in `记忆/指导发现台账.md` (verbatim quotes, seven columns) — **every release of this framework came from real friction**
- **A fixed bug may not re-enter**: a fix card can only be set to `done` if it has a real sentinel test case that passed most recently (`regression_audit.py`)

### 6. Machine-checkable instead of "please comply"

Wherever the framework says "must", there is a script whenever possible; **the exit code is the criterion, natural-language description is not**.
One tri-state convention runs through the whole repo: `0` pass / `1` has ERROR / **`2` = could not check, which is ≠ no problem**.

---

## Operating modes

The framework has **3 project modes** (stored in `config.json`'s `mode`) plus **3 orthogonal switches** (which do not change the mode).

### Rapid mode (quick, default)

**This is the default**: prototypes, demos, small tools, single-module features, and anything not explicitly judged "large".

Exactly **three things may be trimmed**: number of roles (pick a subset as needed; the minimum loop is Product Manager + the relevant developer + Test Engineer), document depth (3–5 bullet points inside the card, no full PRD), test density (test card density drops to `ship`).

**Six things must never be trimmed** (lose one and it is no longer a "light process", it is "no framework"): requirement cards, card state transitions with acceptance records, user confirmation gates, creator ≠ verifier, on-disk output, path discipline.

> **Three hard rules**: ① the orchestrator may not **silently** upgrade to full mode just because "this looks like a big project"; ② when unsure, start in rapid mode (rapid → full simply continues existing cards, while full → rapid means rolling back the 12-role flow and its gates); ③ a mode switch must be written to `config.json`'s `mode` **and** state who proposed it and on what basis.

### Full mode (full)

For multi-module systems and projects with frontend/backend or multi-role collaboration. There are only two legitimate ways in: **the user explicitly asks for it** ("run the whole flow / treat it as a large project"), or the **Project Manager assesses it and informs the user** before switching.

Assessment criteria **U1–U7** (any hit calls for an assessment; several hits strongly suggest switching):

| Signal | Why rapid mode cannot carry it |
|---|---|
| U1 Multi-module/multi-service, frontend and backend split requiring integration | Needs a stable contract surface (`api.yaml`) and integration checks |
| U2 Multiple milestones or a schedule spanning weeks | Progress, risk and acceptance timing need an independent summarising arm |
| U3 External deliverables (proposal / quote / acceptance form / release package) | Deliverable closure and external actions carry audit-chain requirements |
| U4 Authentication and authorization / sensitive data (PII) / compliance targets | Security auditing must not be combined with implementation |
| U5 Going live / production changes / client delivery | Irreversible external actions; sign-off cannot be self-issued by the implementer |
| U6 Migration or destructive structural change | Needs technical-debt assessment and DDL review |
| U7 Multiple people or teams working in parallel | The collaboration surface itself becomes a risk item |

The criteria look at **delivery shape and irreversibility**, not lines of code — a large single-module CLI can still run fine in rapid mode, while a 200-line payment callback must go through full mode.

**Six-phase main line**: requirements and boundaries (PM) → architecture and contracts (Architect) → design (UI: clickable low-fidelity prototype + page-by-page high fidelity, two user confirmations) → implementation (frontend/backend/data, integration owns the wiring) → verification (Test Engineer + Security Auditor) → delivery (Ops: release channel, rollback path, evidence and the "dispatch record"). Each phase has gates, and **a gate only answers "can we proceed" — it never decides for the user**.

### Explore mode (explore)

Used when the project already runs, requirements are no longer changing much, and it enters a **continuous verification period**. The entry is named by the user or the scheduler ("enter explore mode / enter the polishing phase / run a hardening round"), and it is written as `mode=explore` in `config.json`.

**Five-step loop**: take a probe from the map in `契约/探索域.md` (single factor, each probe creates exactly one fault) → if it is not caught, **open a fix card** (reusing the requirement card template with `source: 探索#S-NNN`, `anchor` / `entry` mandatory) → the card goes through the existing five states → fix and re-verify → the **Product Manager evaluates three branches**: sink into a requirement card (spec gap) / sink into a test card (coverage gap) / archive only (one-off issue, with a reason) → cards are archived, never deleted. Machine checks: `explore_loop_audit.py`, `scope_audit.py`.

### Turbo repair mode (turbo-repair, overlay mode)

A temporary fast lane named by the user that **does not change `mode`**: for small customer-reported bugs you need to iterate on a few times. It is entered only when the user says "turbo / turbo repair / just fix it / no cards, no subagents"; **the executor may never choose it**, and it cannot be used during batch runs.

| What may be skipped | What may never be skipped |
|---|---|
| Subagent dispatch (the orchestrator works inline, editing directly against the role skill) | The ledger (`问题/极速修复台账.md`, twelve columns incl. `basis` and `pack`) |
| Card creation (the main token saving) — but existing cards / test cards / the case library must still be read as the basis | A rollback point before changes |
| Full testing (only `smoke` or `targeted`) | Self-check evidence (command + result) |
| Confirmation gates between iterations | **"Not independently verified" must be stated explicitly**: no setting the card to `done`, no claiming acceptance |

**Forced exit lines** (hitting one returns to the normal flow immediately, recording `exit_reason` in the ledger): X1 a single fix touches ≥4 files / X2 touches contracts, interfaces, data model, permissions, sensitive data, production changes or migrations / X3 the same problem is not fixed after 3 rounds / X4 testing reveals a larger surface than expected / X5 the user changes their mind / X6 a formal delivery package, external deliverable, or the round before going live.

### Three orthogonal switches (no mode change)

| Switch | When to enable | What it does |
|---|---|---|
| **Guidance mode** (on by default) | Any time | "Is the framework being used correctly?": `guide_audit.py` G01–G11 looks for traces of requirements without cards, `done` without records, degraded environments, changes executed without paperwork, etc. It only **detects, points to the existing clause, and suggests the smallest fix** — it is not a second commander and never edits files for you |
| **Batch runs** | The user says "run until X o'clock / until N tokens" and is away | Unattended contract: six pre-flight checks → `task_estimate.py --guard` before every round → the five wrap-up actions (stop at a safe point / persist / account / report four questions / distinguish "wrapped up" from "nothing to do") |
| **Solution view area** | Mandatory in full mode, on demand in rapid mode | Project-root `方案/`: `原始资料/` (the only archive location) + `产品稿/` + `原型图/` (both stamped with export date and source path). A human-readable view, **not a second source of truth** |

The batch-run gate's exit codes are the interface:

```
0 CONTINUE    dispatch and close out this round
3 STOP        stop at a safe point immediately and wrap up (no "one more round to see")
4 NOT_STARTED not a start time: do not dispatch, do not implement
5 UNKNOWN     a condition exists but could not be checked: fill the data first, do not dispatch
2 no gate     contract missing or not a single condition present → add conditions first
```

---

## Working with independent skills

This framework **does not absorb single-purpose skills**; the two relate by **layering**. Separate the three layers and you know who to ask:

```
Scheduling layer   who decides "when to start"        AI employee scheduler skill / cron / a human
   ↓
Orchestration      who decides "who gets it, how far, is the evidence enough,
layer              should we stop"                    this framework
   ↓
Execution layer    who decides "how exactly to do this step, and what are the pitfalls"
                                                      Aliyun ECS deploy / SaaS site generator / meta-skill generator …
```

**Division of labour in one sentence**: **the framework owns "should we do it, who does it, when is it done, where is the evidence"; an independent skill owns "how the hands and feet move for this one step".**

### Three typical collaboration patterns

**① Framework → independent execution skill (the framework orchestrates, the skill is the hands)**

Example: **Aliyun ECS deploy**. In full mode, at phase ⑥ delivery, the **Ops Engineer** role produces the release checklist, rollback path and dispatch record as required by the framework. **How exactly to sign in to the console, upload the package with Cloud Assistant, configure nginx/systemd, and what the pitfalls are** — all of that lives in the `Aliyun ECS deploy` skill.

- The framework does not copy deployment manuals; it only mandates: irreversible actions pass a confirmation gate first, the round before going live requires full testing (turbo-repair exit line X6), and deployment results land in `交付/` plus the evidence directory
- The skill likewise must not build a second progress/evidence system — its deployment report counts as evidence only when it lands in the framework's on-disk locations

**② Independent scheduling skill → framework (the skill hosts, the framework is the driven body)**

Example: **AI employee scheduler**. The framework **explicitly contains no scheduler** — "when to start" is always the host's business. The scheduling skill writes goals as shifts, wakes the agent on time, supervises artifacts, classifies and parks failures, and produces morning reports; the framework, once woken, decides **for each round whether to keep running and how to wrap up safely**.

The interface between them is exactly three rules:

| Interface | Convention |
|---|---|
| **Start** | The host prompt contains only "working directory + first command `--guard` + start only on exit code 0". It contains **no business judgement** (the framework carries its own gate) |
| **Wrap-up condition** | Lives in exactly one place: `project-state/跑批契约.json` + `task_estimate.py --guard`. It must **not be copied into the prompt** (a copy is a second hand-maintained checklist and will drift) |
| **No nesting** | A scheduled task may not create or schedule another scheduled task; every trigger must be self-contained |

Host prompt template (copy as is):

```
Batch run for a large project: working directory <project root>. First run python scripts/task_estimate.py --guard;
proceed along agent.md's event loop only if the exit code is 0, and re-check the gate before every dispatch;
for exit codes 2 / 3 / 4, wrap up per the "batch run" section (stop at a safe point, persist, write the run log, report) and end.
```

**③ Parallel and complementary (no calls either way)**

Examples: **SaaS site generator** (methodology for building a site from scratch), **meta-skill generator** (creating/optimising skills), **infographic generator**, etc. — these are independent surfaces. The framework neither needs to wrap them nor should re-invent their rules inside a project. If you do use one in a large project, treat it as a role's "specialised tool"; its artifacts still land in the framework workspace.

### Why split it this way

- **Single source of truth**: process truth exists only in `agent.md`, operational truth only in each skill — nothing is copied, so nothing drifts
- **Replaceable**: swap a deployment skill or a scheduling platform and the framework does not change; swap the framework and the skills still work alone
- **Each stays honest**: the framework only claims "the process does not skip steps", a skill only claims "how this operation is done and what the pitfalls are" — neither pretends to cover the other

---

## Quick start (three steps)

```bash
# 1) Copy this whole directory into your project root (the directory containing agent.md is the workspace root; the name is up to you)
# 2) Ask your AI agent to read agent.md in your project and declare "run with the Large Project Framework"
# 3) Cold start: generate the workspace summary (≤15s, ≤6k tokens)
python -X utf8 scripts/recall.py <project root>
```

On Windows with non-ASCII paths, always use `python -X utf8` (otherwise `open()` falls back to GBK and breaks).

Ways to land it differ per platform, but the essence is the same — **a Markdown instruction set plus local scripts, not tied to any agent vendor**:

| Platform | How to land it |
|---|---|
| DeepSeek Harness (dsh) | Register this directory as a skill entry (`skills/<name>/SKILL.md` pointing at this directory's `agent.md`), invoked manually by the user; dispatch roles with `subagent` |
| Claude Code / Codex / Cursor and similar | Put `agent.md` into the project and reference it from the project instruction file; dispatch roles to subagents one by one |
| ZCode / Trae | Load it as a sub-agent definition or as project instructions |

---

## Repository layout

```
.
├── agent.md                  # Orchestration layer: L0 overview, workspace conventions, modes, six phases, battle sequence, discipline
├── 工作区地图.md              # User-layer navigation page (derived, generated by scripts/workspace_map.py, do not edit)
├── skills/                   # The 12 role skills (with companion manuals and templates)
├── scripts/                  # 26 self-check / audit / generation scripts
│   └── tests/                # 23 regression tests (run them before changing a script)
├── project-state/            # Shared workspace (framework layer, the orchestrator's and roles' workspace)
│   ├── 契约/                 # Semantic truth: PRD, architecture, interfaces, security, design tokens, coding standards…
│   ├── 需求卡/               # One requirement per directory: card.md + notes + acceptance record + evidence
│   ├── 测试卡/               # One system-level test activity per directory: tc.md + coverage matrix + findings + evidence
│   ├── 记忆/                 # Decision log (ADR), change log, reuse ledger, guidance findings ledger…
│   ├── 进度/                 # Per-role progress and accounting JSON (derived artifacts)
│   ├── 问题/                 # CRs, review records and incident paperwork
│   ├── 代码/                 # Output area for code-level roles
│   └── config.json           # Project mode, path registry, policy switches
└── 方案/                     # Solution view area: raw material / product drafts / prototypes (human-readable, not a second source of truth)
```

---

## Machine-checkable checks (what separates this from a prompt collection)

| Command | Purpose | Exit-code convention |
|---|---|---|
| `python -X utf8 scripts/framework_lint.py <root>` | Structure / numbering / references / registry check | 0 pass, 1 has ERROR (do not commit) |
| `python -X utf8 scripts/workspace_map.py <root> --check` | Workspace map reconciliation | 0 / 1 / **2 = could not check** |
| `python -X utf8 scripts/recall.py <root>` | Cold-start summary | 0 |
| `python -X utf8 scripts/migrate.py <root> [--apply]` | Backfill new structure into an old project (dry-run by default; automatic backup before `--apply`) | 0 |
| `python -X utf8 scripts/regression_audit.py <root>` | Regression defence: a fixed bug may not come back (sentinel cases) | 0 / 1 / 2 |
| `python -X utf8 scripts/scope_audit.py` · `explore_loop_audit.py` | Explore-domain coverage / explore loop | 0 / 1 / 2 |
| `python -X utf8 scripts/solution_audit.py <root>` | Solution view area (raw material / product drafts / prototypes) | 0 / 1 / 2 |
| `python -X utf8 scripts/reuse_scan.py <root>` | Prior-art reuse inventory | 0 / 1 / 2 |
| `python -X utf8 scripts/path_audit.py <root>` | Path enforcement (artifacts may not be misplaced) | 0 / 1 / 2 |
| `python -X utf8 scripts/guide_audit.py <root>` | Guidance check (G01–G11) | 0 / 1 / 2 |
| `python -X utf8 scripts/test_scope_audit.py <root>` | Whether test scope and depth were quietly downgraded | 0 / 1 / 2 |
| `python -X utf8 scripts/workspace_hygiene.py <root>` | Workspace cleanup list (report-only by default) | 0 |
| `python -X utf8 scripts/task_estimate.py --guard` | Batch-run gate | 0 continue / 3 stop / 4 not started / 5 unknown / 2 no gate |

**`2 = could not check ≠ no problem`** is a point this framework keeps repeating: when a checker cannot run, that must never be treated as a pass.

---

## Three design principles (why it is worth a read)

- **On disk means evidence**: nothing written into the workspace means nothing happened. A conversation is not a deliverable.
- **Single source of truth + derived artifacts**: contracts live in `project-state/契约/`; maps, progress and JSON are derived, recomputable, and never hand-edited.
- **Friction gets registered**: when users get stuck, work around the process, or the same spot keeps being revised, it goes into the ledger immediately; three entries of the same kind mean the next release must change that clause.

---

## Requirements

- Python 3 (standard library only, no third-party dependencies)
- Windows / macOS / Linux; on Windows with non-ASCII paths use `python -X utf8`
- Any AI coding agent able to read/write local files and run commands

---

## About the contents of this repository

- This repository is the **framework itself**; it contains no real customer projects, real server addresses or commercial material.
- Names such as `示例工作台A`–`示例工作台H` (Example Workbench A–H) and `示例平台C` (Example Platform C) are anonymised placeholders.
- `Aliyun ECS deploy`, `AI employee scheduler` and the like are **independent skills** that are not part of this repository; they appear only to explain the collaboration model.
- The framework's **field chronicle** (which records which real project friction produced each mechanism) is not published here; only a pointer is kept. Mechanism provenance can be traced by searching the `（vX.Y.0…）` annotations in `注意事项.md` / `验收标准.md` / `完成定义.md`.

---

## Contributing

Issues about process design are welcome. Before opening a PR, run:

```bash
python -X utf8 scripts/framework_lint.py .
python -X utf8 scripts/tests/test_*.py   # or run them one by one
```

Requirements: `framework_lint.py` reports **0 ERROR**, and all regression tests pass.

## License

[Apache License 2.0](LICENSE) — free to use, modify, distribute and use commercially, provided you keep the copyright and licence notices and include the patent grant.

Copyright 2026 Large Project Framework contributors
