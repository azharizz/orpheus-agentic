# Product

<!-- impeccable:product-schema 1 -->

Orpheus · 2026-09-06 · Functional contract for the clean application, not a release certificate.

## Platform

web

## Stack

Existing prototype: Python/Google ADK, FFmpeg/NumPy, browser UI, OpenRouter, SQLite and local Grafana services. **Deployment target: GCP.** The clean frontend framework and exact managed services remain undecided; no stack change is authorized by this formatting update.

## Users

Primary users are filmmakers, editors, sound designers and creators fitting or performing effects for short scenes. Persona research and willingness-to-pay are not established.

## Product Purpose

### Authority and scope

This file defines what Orpheus does, its data responsibilities, permitted workflows, and acceptance conditions. [DESIGN.md](DESIGN.md) defines its appearance and interaction presentation. If appearance conflicts with data integrity, consent, evidence, or accessibility, this product contract wins.

The owner has requested two workflows: **fit independent SFX to picture** and **record/refine Foley takes**. Both belong in Orpheus. Independent fitting is the primary entry; recording is a connected workflow, not a second unrelated product.

**Confirmed deployment direction: GCP.** Local V2 is the current development/validation environment, not the final hosting strategy. Design for a GCP-hosted application and agent workflow; do not describe the future product as local-first. The owner confirmed this distinction during document preparation.

Creating these documents does not migrate, replace, deploy, or certify the prototype. Preserve the original lab, frozen V1, current V2, media, sessions, receipts, and human reviews. Future migration needs its own implementation and preservation checks.

**Turn a supplied sound recording into a reversible, inspectable alternative for an on-screen action, then help the creator decide what to change or record next.**

## Positioning

The useful mechanism is a closed experiment loop: picture/audio evidence → explicit edit → actual rendered sound → measurements and listening → evidence-linked revision. Google ADK coordinates the loop. Signal-processing tools perform the edits. Grafana provides cross-attempt observability through its official MCP server. The creator judges the result.

This mechanism is a product hypothesis, not a claim of market uniqueness. Agentic orchestration and Grafana are valuable only if they help users reach a better-supported decision with less correction effort. A dashboard that simply repeats numbers is insufficient.

## Operating Context

Primary working audience: filmmakers, editors, sound designers, and creators replacing or performing effects for short scenes. This audience follows the established product scope; detailed persona research and willingness-to-pay are not yet established.

The user has picture and some recorded sound, but may lack clean production audio, convincing synchronization, or a suitable performance. They need to inspect, listen, compare, and retain control—not supervise an opaque generator.

Desktop is the primary editing environment. Smaller screens must support inspection and auditioning with accessible alternatives to timeline gestures. Headphones are useful for evaluation but cannot be assumed. Microphone availability and browser recording latency vary.

The same job can occur in live-action, animation, AI-generated video, advertising, or other picture-led media. These are possible input contexts, not separately validated verticals. Orpheus is not film scheduling, crew management, marketing analytics, a render-farm monitor, or a complete DAW.

### Fit independent SFX

1. **Prepare:** validate formats, sizes, durations and tracks; clip before model inspection; extract analysis audio; hash the prepared inputs. Explain whole-soundtrack replacement before starting.
2. **Inspect picture:** obtain duration, request relevant frames, wait for actual frame delivery, and inspect transitions. A request for a screenshot is not evidence that the model saw it. Adaptive sampling is not a promise to inspect every frame at 0.25-second intervals.
3. **Inspect sound independently:** analyse target/source signals and, when enabled and authorized, ask the audio observer about each recording. Reconcile its hypotheses with picture and local evidence; do not accept regular event schedules simply because a model returned them.
4. **Read experiment history:** query scoped Grafana evidence through official MCP. Respect project, turn, candidate, measurement freshness and export status. Preserve the application's current editing state as authoritative; it is local in V2 and managed by the deployed application on GCP.
5. **Plan the scene:** distinguish contact sounds, sustained phases, connected sequences, confirmed quiet, unmatched source needs, and unknown regions. Do not fill unknown regions with silence labels or evenly spaced effects.
6. **Fit:** inspect source crops; locate the internal acoustic landmark; align that landmark to the supported target contact; preserve useful body/tail; apply bounded gain, fades and mixing. Use batches to preserve the controller budget, checking every row's result.
7. **Render:** enforce existing impact evidence gates. A failed gate is not permission to relabel an impact as a texture or fabricate IDs. Repair affected rows without discarding valid work.
8. **Check:** immediately measure the actual export, then query candidate evidence through MCP. Review decoded audio when available. Compare physical-event evidence separately from schedule-relative peak alignment.
9. **Revise:** choose a supported correction to missing/extra actions, phase boundaries, timing, source crop/character, or dynamics. More renders and different hashes do not establish improvement.
10. **Hand off:** present alternatives, measured changes, uncertainty and audition controls. Human approval or rejection is explicit. An unsuitable result is allowed when the source or evidence cannot support the job.

### Record and refine

1. Choose a project and picture cue. Enter one optional prop/performance brief.
2. Explicitly start microphone capture or upload a recording. Request microphone permission only for capture. Mute the target during capture to reduce bleed; restore its prior mute state afterward.
3. Make recording state and stop controls continuously available. Release microphone tracks on stop, failure or leaving the page. A permission failure offers upload; it must not initiate inference.
4. Audition in the browser, then save. In the GCP product, distinguish unsaved browser capture, uploading and confirmed cloud persistence. Persist the cue captured at recording start, not a subsequently edited field. Browser capture/picture clocks are provisional, not sample-synchronized.
5. Compare actual takes through Grafana MCP using measurements plus local brief context. Equalize audition level only when explicitly selected and label it: louder must not be silently presented as better.
6. The agent proposes one controlled prop, performance, or microphone-position change and a testable prediction. It cannot claim the prop was recognized from spectral metrics or that a future recording exists.
7. The creator performs/uploads the next take. Retain prior takes and compare against the same picture region.
8. Fit the chosen take using Workflow A in a linked project. Preserve parent project and recording; send media to the configured model only after the explicit run action.

Level-matched comparison, reliable synchronized A/B transport, and experiment-outcome linking are clean-app requirements, not claims that all are complete in current V2. Do not conceal these gaps behind active-looking controls.

## Capabilities and Constraints

### Functional inventory and current evidence

Status here is based on the V2 code and recorded project history available when this contract was written. “Implemented” does not imply perceptual validation or production readiness.

| Area | Current prototype evidence | Clean-app requirement |
| --- | --- | --- |
| Independent input and preparation | V2 prepares short video/audio and independent SFX | Preserve source identity, original-audio presence, clipping window, and preparation receipts |
| Agentic editing | ADK runner, LoopAgent, tool execution, persistent project sessions | Retain stateful inspect/edit/measure/revise behavior, not a single prompt disguised as a workflow |
| Impact fitting | Source crops, acoustic landmarks, contact anchors, bounded balancing, temporal-review gates | Preserve these primitives and per-row receipts; do not hand sample placement back to the model |
| Sustained and multi-stage actions | Mixed arrangements, texture path, and stage fitting exist | Test these paths separately; the simpler fallback texture path is not equivalent to a complete mixed arrangement |
| Candidate evaluation | Exported-audio measurements and independent audio review tools exist | Keep processing measurements separate from model listening hypotheses and physical timing truth |
| Record/refine | Capture/upload, take receipts, derivative fitting projects, and experiment proposal code added | Complete route, microphone, playback, and real-agent integration validation before calling the workflow verified |
| Grafana | Local stack, exports, and successful official MCP Loki/Prometheus queries demonstrated | Show fresh-run evidence and an actual agent decision that uses it, not only historical backfill |
| Human review | Candidate approval/rejection and assisted edits exist | Persist judgment against the exact candidate/audio hash; never transfer approval silently |
| Generalization | Shoes, crackers, scanner and later heavy-bag experiments expose differing failure modes | No guarantee of arbitrary-video accuracy; keep a truly unseen validation case |
| Managed cloud | GCP project `orpheus-agentic` hosts Firebase Hosting, Cloud Run API/job, Agent Engine Runtime, managed Sessions, selective Memory Bank, and Grafana MCP | Provision Cloud SQL product records later in `project-cb6f73d4-12f4-4aa6-98b`; keep hosted runtime and media boundaries explicit |

Earlier chat checklists overstate completion when they equate configuration or code presence with end-to-end success. In particular, a clean final recording-route test, fresh-run Grafana validation, and evidence-driven quality improvement remain acceptance work; this document does not mark them passed.

### Input, identity, and output contracts

#### Inputs

- One target video and one independent SFX recording per fitting project. A selected recorded take can supply that SFX recording through a linked project.
- No more than two optional short context fields: **What should make sound?** and **How should it feel?** A recording brief can combine prop and performance context without a long questionnaire.
- Target audio, when present, is evidence—not automatically clean ground truth. A silent target is permitted only when its absence is explicit; it cannot stand in for a test that requires original audio.
- A file import, project selection, or audition must not by itself start paid inference. An explicit run action authorizes that work. In the GCP product, distinguish browser-local audition from persisted cloud upload and disclose storage/inference destinations before transmission.
- No silent substitution of source media, provider, approved data destination, or requested crop.

#### Identities

| Object | Meaning and boundary |
| --- | --- |
| Project | Prepared video/SFX identities, briefs, and associated session; changing source requires a new linked project or explicitly versioned input |
| Turn | One bounded execution in that session; actual model, calls, failures, configuration and prompt/code versions |
| Evidence | Inspected role, source hash, time range, method/model and delivery receipt; model labels remain hypotheses |
| Mapping row | One impact, sustained phase, composite action, omission, or unmatched requirement |
| Arrangement | Versioned set of rows; its ID is not a row ID |
| Candidate | Successfully rendered MP4/WAV and measurement receipt; a requested or failed render is not a candidate |
| Take | Saved recording, brief, preparation/hash and clock uncertainty; multiple uploads of one file are not independent performances |
| Review | Human or agent provenance, exact candidate identity, timestamp, and unresolved concerns |
| Experiment | Proposed change, rationale, predicted observable effect, evidence receipt, and later outcome if actually tested |

#### Outputs

Retain preview video, fitted audio, arrangement/plan, source provenance, measurements, failure history, and explanation. Offer local export of available artifacts without treating export as approval. Never overwrite an approved or original artifact to save a revision.

### Runtime and data flow

```text
Video + independent SFX / selected take + brief
                    |
         Validated preparation + immutable identities
                    |
       ADK runner + project session <------ Human feedback
                    |
     Inspect -> map -> fit -> render -> measure -> revise
                    |                        ^
          Redacted evidence outbox           |
                    |                        |
       Loki / Prometheus / Tempo -> official Grafana MCP
                    |                 scoped observations
              Grafana dashboards

Rendered alternatives -> human audition -> approve / reject / revise
```

| Owner | Responsibility | Not its responsibility |
| --- | --- | --- |
| ADK/model | Stateful tool choice, interpretation, proposed changes and explanation | Sample-accurate DSP, infallible hearing, artistic approval |
| Media tools | Deterministic processing, bounds, exported measurements and receipts | Inferring every physical contact or material from peaks |
| App/session/artifacts | Editing state, identity, authorized storage, consent and review | Treating a telemetry mirror as an editable project database |
| Grafana stack/MCP | Queryable processing history, failures, comparisons and runtime health | Hearing raw media, fitting audio, replacing session memory |
| Creator | Source rights, context, performance, perceptual judgment and approval | Debugging every internal tool protocol |

### Grafana is a decision input, not decoration

Use the official `grafana/mcp-grafana` server or official hosted Grafana Cloud MCP endpoint. The current development setup runs the server locally; the production application/agent target is GCP. Current app tools scope Loki history/sound/failure/take queries and Prometheus runtime queries. GCP hosting does not decide whether Grafana itself is self-hosted or Grafana Cloud; resolve that separately against operational and hackathon requirements. Do not claim every advertised MCP tool or cloud feature is enabled. Broad administrative/write access is unnecessary for fitting; keep the runtime identity read-only and scoped.

Export useful observations with units, media time, observation time, project/turn/candidate identity, source hash when applicable, method and provenance. Grafana is the primary cross-attempt observability interface; JSONL/outbox are recovery/audit storage, not a replacement for runtime MCP use.

| Evidence family | Useful decision | Misinterpretation to prohibit |
| --- | --- | --- |
| Coverage and phase ranges | Investigate missing planned sounds or a short crop in a sustained phase | Every uncovered hypothesis interval needs continuous sound |
| Target anchor, source landmark, decoded peak | Locate which row/crop shifted during processing | Zero peak error proves the correct visible contact |
| LUFS, true peak, clipping, local body/crest | Compare mix level, spikiness and weak event bodies | One loudness target suits all scenes; louder is better |
| Quiet percentile, activity fraction, envelope | Locate quiet/loud regions and compare recording conditions | Quiet percentile is calibrated noise; low energy means no action |
| Spectral balance, source duration and crops | Compare recordings and investigate unsuitable crop shapes | Frequency balance establishes material, distance or realism |
| Provider/tool failures, latency, usage, export lag | Decide whether to repair, defer or explicitly use degraded evidence | HTTP 402 means the scene is silent; missing usage means no charge |
| Human review and experiment lineage | Compare what was actually accepted and changed | Agent preference or history counts as human approval |

A useful trace is: measurable defect → MCP receipt → bounded change → new render → comparison → human assessment. For example, a missing printing body requires phase/source investigation, not a gain-only revision. This is a testable decision path, not a claim that the scanner case is solved.

Raw audio/video, images, transcripts, free-text notes, prompts and credentials must not enter telemetry by default. Model inference destinations are a separate consent boundary. Historical imports retain observation time and identify ingestion/backfill context; they must not masquerade as newly occurring provider failures or new experiments.

Telemetry outage must not erase edits. Show stale/pending/unavailable evidence, retain the outbox, and retry within bounds. A query attempt or HTTP success alone cannot certify candidate evidence freshness. A degraded preview may be offered with explicit limitations; never call it Grafana-verified or automatically approved.

### Permitted transitions and failure behavior

Conceptual states below are product states, not a requirement to rename existing API values.

| Situation | Permitted action/result | Forbidden shortcut |
| --- | --- | --- |
| Valid inputs prepared | Explicitly start a bounded run | Start paid work on drag/drop alone |
| Frames requested, not delivered | Wait/continue until delivered or report failure | Record visual certainty before delivery |
| Some batch rows fail | Preserve successes; repair failed rows | Claim all rows fitted or discard the whole plan silently |
| Source lacks needed phase | Preserve unmatched/uncertain evidence; ask for another source/take | Invent the missing sound or loop an impact as machine texture |
| Provider denies audio request | Report failure; use permitted signal fallback explicitly | Hide observer failure behind a completed render |
| Render exists but measurement fails | Keep provisional artifact; retry safely or explain limitation | Mark technical checks passed |
| Limit reached or turn interrupted | Preserve completed work and recovery state; resume only on supported action | Invent a candidate, infinite retry, or silent new paid turn |
| Human accepts candidate | Save approval against exact hash/version | Promote all future revisions to approved |
| User edits candidate | Create assisted revision and fresh checks | Count manual correction as autonomous benchmark success |

Do not expose cancellation, restart, undo, or resume as working UI until the corresponding backend behavior is implemented and tested. Existing completed media remain accessible after a failed run.

### Baseline limits and capability boundaries

These are current prototype constraints, not permanent product goals. Read active configuration at runtime; UI limits and provider caps must agree with it.

| Bound | Current baseline |
| --- | --- |
| Input media | Opening 30 seconds; maximum 100 MiB per input file |
| Record/upload take | Prepared take up to 30 seconds; browser cue latency uncalibrated |
| Execution | Single local worker; up to 40 controller calls, 5 loop cycles, 1 successful render/cycle; 30-minute turn timeout |
| Audio observer | Up to 6 shared requests per turn; rendered listening consumes this budget |
| Model calls | 180-second request timeout, 30,000-token requested output ceiling; provider may impose lower caps |
| Inventory/arrangement | Up to 100 events/rows; inspect/fit batches up to 12; source options up to 24 |
| Frames/review | Bounded adaptive windows; not exhaustive full-video inspection; current prompts impose earlier evidence-gathering cutoffs |
| Memory | Hosted project-scoped Agent Engine Sessions plus selective Memory Bank context/style entries; local development uses SQLite sessions and disables Memory Bank |
| Soundtrack | Whole-track replacement; no promised selective denoising or dialogue/ambience preservation |
| Scope | No guaranteed arbitrary-video accuracy, automatic material/perspective matching, sound generation, voice cloning, surround/Atmos certification, or complete DAW |

A call budget is not a currency budget. Long session history can increase input cost. Cloud packaging, authentication, authorization, media isolation, retention/backups, cancellation and multi-user scheduling require explicit future work. Local deployment does not certify hackathon compliance; verify the published cloud/Gemini/MCP requirements before submission.

### Deployment and implementation boundary

Existing runtime: Python/Google ADK, FFmpeg/NumPy, local web UI, OpenRouter provider integration, SQLite and local Grafana services. Production target: **GCP**, confirmed by the owner. Hosted deployment uses Firebase Hosting, Cloud Run API/job, Agent Engine Runtime/Sessions, selective Memory Bank, private Grafana MCP, and Cloud Storage; Cloud SQL product records are deferred to `project-cb6f73d4-12f4-4aa6-98b`. Preserve the working agent/media behavior; do not replace it solely to change hosting.

#### GCP deployment contract

- Run the application and agent execution on GCP with project/user authorization; localhost Host/Origin checks are not cloud authentication.
- Store media and artifacts in access-controlled object storage with explicit upload, retention, deletion and export behavior. Use short-lived authorized access; never make benchmark or user recordings public by default.
- Use persistent, project-scoped session storage suitable for the chosen runtime. Local SQLite is the prototype implementation, not the managed-session design. Cloud Memory Bank remains optional cross-session retrieval, not a synonym for sessions or automatic learning.
- Isolate media processing and bound jobs/timeouts; package FFmpeg and its dependencies where supported. Verify restart, cancellation, idempotency and artifact preservation before exposing those controls.
- Keep provider and Grafana credentials server-side using managed secrets and least-privilege identities. Preserve the owner's existing model authorization; changing infrastructure is not permission to send media to additional providers.
- Maintain the same redacted telemetry and official MCP decision loop across deployment. Grafana credentials never go to the frontend. Validate secure connectivity, query scoping, delayed exports and trace correlation in the deployed environment.
- Display real storage and inference destinations. Cloud upload/save must not be labelled “saved locally.” Budget enforcement, tenancy, backup/recovery and migration checks are release requirements, not completed features.

## Brand Commitments

Name: **Orpheus**. Voice: direct, specific, candid about uncertainty. Explain what changed and why. Avoid claims such as perfect sync, studio-grade sound, or autonomous artistic approval.

The owner wants an expressive cinematic identity and dislikes button-heavy interfaces. Preserve clear, accessible actions and consent; DESIGN.md defines the low-chrome presentation. Functional controls must not become invisible gestures.

## Evidence on Hand

- [Original scope](../foley-lab/PRODUCT_SCOPE.md): product thesis and falsification criteria; its implementation status is historical.
- [Current V2 overview](../foley-agent-lab/v2/README.md), [controller contract](../foley-agent-lab/v2/prompts/controller-mapped.md), [shared evidence rules](../foley-agent-lab/v2/prompts/perception.md).
- [Workflow](../foley-agent-lab/v2/workflow.py), [worker](../foley-agent-lab/v2/worker.py), [takes](../foley-agent-lab/v2/takes.py), [observability](../foley-agent-lab/v2/observability.py).
- [Grafana notes](../foley-agent-lab/v2/observability/README.md), [three-video report](../foley-agent-lab/v2/diagnostics/THREE_VIDEO_VALIDATION.md).

Resolve conflicts against current code and reproducible receipts, not an earlier optimistic chat summary. Do not copy private media, `.env`, session databases, generated logs or service tokens into the new repository while migrating these contracts.

### Success, falsification, and release acceptance

Evaluate the same scenes against the older reference outputs, a basic manual editing workflow, and a non-agent checklist. Record time to a human-accepted preview, missing/extra actions, phase ordering, independently annotated timing, source suitability judgments, manual interventions, usage/cost availability, and unresolved uncertainty. Do not combine them into an unvalidated universal quality score.

- Shoes: test contact timing and source attack/body/tail fitting, not only schedule-relative peak error.
- Crackers: test irregular bites/chews, quiet events and dynamics; no equal-spacing assumption.
- Scanner: preserve the owner's approximate observations—initial quiet, screen-touch beeps, then printing around 14 seconds. These are development annotations, not exact millisecond ground truth or hard-coded production rules.
- Unseen input: freeze workflow/prompt/config hashes before the run; collect evaluation afterward. A case used for tuning is no longer a blind test.

Acceptance must independently establish: original preservation, valid inputs, real ADK tool looping, session reload, rendered-output checks, actual MCP queries, fresh evidence visibility, outage recovery, human-review provenance, and complete UI paths. A scripted controller test proves wiring, not autonomous usefulness. Passing signal checks proves neither naturalness nor generalization.

Narrow or reconsider the feature if it requires more user effort than ordinary editing, if recommendations fail to outperform a checklist, or if Grafana evidence changes no decision. No percentage of time/cost saved, accuracy guarantee, customer claim, or public benchmark claim is permitted without supporting evidence.

## Product Principles

1. Preserve originals; edits create identifiable alternatives.
2. Measure the exported result, not just the intended arrangement.
3. Distinguish observations, hypotheses, processing checks, and human judgments.
4. Repair coverage, sequence, and synchronization before polishing gain.
5. Admit unsupported source material or insufficient evidence. Never invent a result to finish a loop.

## Accessibility & Inclusion

Retain keyboard-accessible alternatives to dragging, labelled media controls, explicit microphone permission, readable error states, and non-color status cues. Never hide paid execution or human approval behind an undiscoverable gesture. Mobile web remains web, not a separate native application. DESIGN.md contains the presentation requirements.
