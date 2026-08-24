# ADR-001: dependency-free collection UX fixture

Status: accepted for the backend-free vertical slice, 2026-08-24.

## Decision

Use semantic HTML, CSS, browser JavaScript, JSON fixtures, one bundled English/Korean message catalog, Python's `http.server`, and `unittest`. There is no package manifest, lockfile, build step, client store, router, component framework, translation service, or live API. English is the deterministic default; `?lang=ko` and the native language control change presentation and `html lang` only. Frontend state is always a replaceable rendering of one fixture/backend snapshot, while commands, paths, digests, codes, identifiers, and backend authority remain canonical.

The React/TypeScript/Vite baseline would add a package graph and build pipeline before the slice needs component reuse, type sharing, routing, or a scene canvas. Vercel's React rules therefore have no runtime target here; their useful constraint is satisfied structurally by shipping no React bundle, waterfall, hydration, or client cache.

If richer P8 scene visualization becomes qualified, migrate by keeping `states.json`/the proposed snapshot contract, porting the seven render states into React components, and replacing only `app.js`. The concrete cost is a locked Node toolchain and CI cache, schema-to-TypeScript generation or duplicated types, seven interaction checks, and a canvas/SVG scene component—roughly two focused engineering days before visualization qualification work. Do that only when dynamic object/slot selection or canvas interaction makes direct DOM rendering measurably hard.

## Operator and single job

The user is a lab operator standing near an FR5 cell. The page's single job is to show what is true now, why the next motion is or is not allowed, and the one safe next action without making the browser a second lifecycle owner.

The visual direction is a calibration bench: slate paper (`#f7f9fb`), blueprint ink (`#10243c`), instrument cyan (`#12647a`), verified green (`#1c6b50`), and safety amber/red (`#a64b00`/`#a52d2d`). System sans is paired with system monospace for digests and evidence; no font request can fail offline. The signature element is the six-station evidence rail, whose numbering is functional because collection order and stale-state invalidation matter. The deliberately imperfect fixture stamp resembles a physical inspection mark; the rest remains quiet.

## Current journey and bottlenecks

Current production authority is split across qualified config and runtime artifacts:

1. The operator prepares a campaign manifest containing ordered runs, release roles, profile, paths, budgets, and scene-bound job input, then invokes `direnv exec . python3 -m tools.data_factory.run_job campaign --manifest …`.
2. `run_job.py` validates the exact two-episode campaign, checks cell/readiness and camera warmup, resolves the current scene/start state, plans, and emits `AWAITING_HUMAN_APPROVAL` with the exact plan digest.
3. The operator types the exact digest approval. For the second episode, `LANDED_AND_APPROVE_NEXT <digest>` combines physical landing/clear-path confirmation with that episode's fresh plan approval; campaign selection never substitutes for motion approval.
4. The runner owns recorder/motion/progress, technical validation, scene/cell transitions, and candidate creation. Failures return stable code/state, but normalized preservation and next-action fields are inconsistent.
5. After children close, candidate review reads `technical_validator.json` plus `candidate_admission.json`; a backend file/context-digest CAS alone may change `PENDING` to `PASS|FAIL|UNCERTAIN`. Training approval remains separate.
6. Coverage is an offline `REPORT_ONLY` artifact whose `suggest_next` excludes blocked or pending-review conditions; choosing it does not schedule motion.

The primary bottlenecks are manual assembly/re-entry of values already present in profile, scene, coverage, campaign, and run artifacts; switching between commands/artifact paths to understand readiness; one terse blocked code without a consistently colocated preservation/next-action description; and review prompts that make operators reconstruct condition/evidence context. Exact per-episode approval is intentional safety work, not removable friction.

## Information architecture

- Persistent context: fixture/live mode, campaign/session, and the fact that no command was sent.
- Evidence rail: setup, readiness, exact approval, progress, review, recovery.
- Setup receipt: qualified profile, camera role/topic, object, condition, and coverage provenance; values are not editable after binding.
- Primary state: one status, plain-language reason, exact digest or progress when relevant, and one safe next action.
- Evidence chain: artifact names/digests and the authority boundary for the current state.
- Review: semantic checklist decision once; technical status and training approval are visibly separate.

On narrow screens the same reading order becomes setup → primary state → evidence. Native buttons, inputs, labels, landmarks, keyboard focus, a polite status region, sufficient palette contrast, and reduced-motion handling form the accessibility floor.

## Measured interaction targets

These are acceptance targets for later instrumented integration, not claims about current field timing:

| Measure | Target | Fixture evidence |
|---|---:|---|
| Artifact-ready setup to exact approval view | ≤ 90 seconds, excluding physical placement | One setup receipt and one backend plan action |
| Duplicate operator entry | 0 fields already present in profile/binding/coverage/scene | Setup values are bound, not editable |
| Normal approval round trips | Exactly 1 per episode | Exact digest form; no campaign-wide shortcut |
| Block diagnosis | Code, preserved state, and next action visible together | Blocked/recovery fixtures |
| Normal semantic review | 1 decision; 1 reason only for FAIL/UNCERTAIN | Review fixture |
| Unknown scene recovery | 0 later goals until fresh scene + plan + approval | Recovery fixture |

Measure these from backend event timestamps and UI interaction events without recording typed approval text. If the 90-second target misses, first remove duplicate setup entry and artifact hunting; do not weaken readiness or exact approval.

## Future information, not future UI

P5.5 Object–EE data is offline diagnostic context (`DECLARED_STATIC_PREGRASP_TO_CLOSE`), not actual observed object pose or an admission gate; show it later as evidence, never readiness. P6 condition/trajectory variants need explicit profile/recipe provenance and equal-budget comparison, not a generic variation toggle. P8 pick-place, dual-camera, and human-authored multi-object scenes require separate qualified contracts and recording boundaries. This fixture reserves evidence labels and a setup receipt but deliberately implements none of those controls.
