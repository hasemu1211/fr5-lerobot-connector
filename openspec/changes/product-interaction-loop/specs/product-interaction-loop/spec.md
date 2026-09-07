# Product interaction loop

## ADDED Requirements

### Requirement: Exact native episode inspection returns to its original review

The existing prepared training review SHALL offer read-only native LeRobot/Rerun inspection of a named episode in its frozen batch. The server SHALL derive dataset paths from that prepared target; the browser SHALL send only the expected batch identity and episode index through the existing fresh-view intent contract. Inspection SHALL NOT approve, refuse, relabel, collect, train or authorize motion. The original preview and independent approval actions SHALL remain available after viewer failure or return.

#### Scenario: Open, inspect and return

- **WHEN** a person selects a committed episode in the existing Web review
- **THEN** the product opens the installed native viewer with synchronized UP/WRIST, action and state, and exposes the exact episode, dataset digest, feature order/units and canonical global/local frame mapping
- **AND** the person can inspect a nonterminal frame and return to the same batch and episode without path transcription, a new approval or an automatic decision retry.

#### Scenario: Evidence changes or the viewer cannot complete

- **WHEN** the target is missing, changed, outside the prepared batch, or has an unsupported frame/feature mapping
- **THEN** inspection rejects or explicitly reports that the prior target is no longer current, without changing the review's evidence or authority
- **AND** an export/viewer failure remains an inspection error; lost responses use the existing canonical state read without repeating the command.

#### Scenario: Exploration stays bounded and local

- **WHEN** native inspection runs
- **THEN** only one episode is exported per review process, with finite frame, output, resident-memory and lifetime limits; export uses local offline data and no GPU model
- **AND** viewer endpoints bind loopback, temporary output remains server-owned, and return, expiry or application shutdown cleans up only owned processes and temporary artifacts
- **AND** the Web flow closes its own viewer tab on return when it retains that tab handle; reloading the review does not grant authority over unrelated browser tabs.

### Requirement: Recover current training-review evidence without repeating authority

When a training-review intent response fails, the product SHALL make one automatic read of the existing canonical view and render the returned review status, exact batch and available operations. It SHALL NOT resend the intent or infer approval from a transport error. A successful read SHALL remove the need for an extra human refresh merely to discover an already completed result. Human and automation surfaces SHALL derive review meaning from the same domain projection; neither surface grants authority by refreshing it.

#### Scenario: A completed decision response is lost

- **WHEN** the backend completes preparation, approval or refusal but the page loses its response
- **THEN** the page performs one state read, shows the current canonical result and identifies that it did not repeat the request
- **AND** approval remains an explicit exact-batch decision and does not start training.

#### Scenario: The recovery read also fails

- **WHEN** the one automatic read cannot establish current review state
- **THEN** decision actions remain unavailable and the page offers its existing explicit state refresh
- **AND** it does not loop, replay the decision or present the previous batch as newly approved.

#### Scenario: The request succeeds normally

- **WHEN** the response arrives successfully
- **THEN** the page retains its existing single post-decision state read and renders only backend-permitted actions
- **AND** there is no additional confirmation, typed acknowledgment or filesystem approval step.

### Requirement: State-read efficiency preserves canonical meaning

The product SHALL avoid resolving an identical workspace cycle repeatedly within one operator projection when its consumers only read that value. This reduction SHALL preserve complete view content, view digests, available operations and rejection behavior for human and automation consumers. Reuse SHALL end with that projection; subsequent reads and commands SHALL validate current state without a cross-view cache or catalog-digest bypass.

#### Scenario: Several projection consumers need the same workspace cycle

- **WHEN** one current view needs the route for draft readiness, displayed endpoints, coverage or state-space summary
- **THEN** it resolves the route once and produces the same detached output as independent resolution
- **AND** the unchanged sampler-parity journey passes with fewer route calls and lower measured unprofiled elapsed time on the same machine.

#### Scenario: A later read observes a changed draft or catalog

- **WHEN** a prior view was returned and a canonical input subsequently changes
- **THEN** the next read resolves current state again, changes the digest when output changes, and rejects invalid catalog state
- **AND** a command bound to a stale view remains rejected and compile-time readiness validation remains independent.

#### Scenario: A consumer mutates a returned view

- **WHEN** a consumer changes displayed route fields in its returned view
- **THEN** the canonical route, sibling projection fields and subsequent views remain unchanged.

### Requirement: Complete an exact Curator review through the Web surface

For an existing native Curator review run, the product SHALL let a local person inspect the synchronized raw, keep/geometry overlay and actual candidate video, its selected clip coverage and exact evidence identities, explicitly approve or reject the candidate, and read the native durable result in the Web UI. The current constraint is a trusted local loopback server bound to one run, with a server-owned OS actor that is not authenticated personal identity. The browser SHALL send only the explicit choice and expected review digest inside the existing intent envelope. It SHALL NOT select paths, claim an actor, prepare production candidates, grant training authority or inherit source approvals.

#### Scenario: Review evidence and make an explicit decision

- **WHEN** a configured native review is valid and its bound video is available
- **THEN** the page presents the actual three-panel video with native playback, clip seeking and explicit sample-coverage limits
- **AND** the person's approve or reject choice is consumed by Curator's existing lock, exact-evidence validation, event and atomic publication/cleanup owner without a TTY, typed acknowledgment or filesystem approval errand.

#### Scenario: A stale, wrong-run or forged request arrives

- **WHEN** a request has a stale view, a different review identity, an extra actor/run/path input, or a choice conflicting with an already recorded decision
- **THEN** the existing view CAS and native review owner reject it without recording a new decision or granting additional authority
- **AND** media retrieval requires the loopback token and current native review identity; browser-provided paths are never read.

#### Scenario: A response is lost or a page is reopened

- **WHEN** a decision response is lost or the page is refreshed or reopened
- **THEN** the page reads the authoritative result without automatically sending the decision again
- **AND** only an explicitly pending in-process request may cause bounded state reads; there is no idle whole-dataset polling
- **AND** if the read fails, decision actions remain unavailable and an explicit state refresh remains possible.

#### Scenario: A recorded action needs completion

- **WHEN** native Curator reports a recorded decision with a recoverable publication or receipt failure
- **THEN** the page shows that recorded choice and offers only native-permitted completion of it
- **AND** recovery retains the original decision and actor without conflicting consent, duplicate publication, new approval authority or a required media-watching ritual.

#### Scenario: The next domain consumes the result

- **WHEN** Curator publishes or rejects the exact candidate
- **THEN** the Web view exposes the native receipt, exact target/evidence identities, decision provenance and permitted effects for the next native consumer without manual path or digest transcription
- **AND** this current human decision path does not define qualified future system judgment as human provenance or imply training admission. Curator retains lifecycle and lineage; training admission remains its existing owner's separate decision.

#### Scenario: Review media fails after a durable decision

- **WHEN** publication and its receipt are committed but the review video subsequently becomes missing or corrupt
- **THEN** the page distinguishes the native committed decision/receipt from unavailable playback evidence
- **AND** it does not describe the transport or media failure as rollback, permit an opposite choice, re-publish automatically, or claim the unavailable video remains valid.

### Requirement: Consume stored collection advice as the next actual draft

The Collection application SHALL retain the preceding campaign's server-owned stored-evidence and authoring bindings when returning to drafting. People and automation SHALL inspect and choose the same native evidence-derived recommendation through the existing view and intentional commands, without transcribing paths or digests. Applying advice SHALL only author a draft; it SHALL NOT create a campaign, obtain approval or cause robot, recorder, dataset or training effects. The next consumer is the existing collection compiler and execution owner with its unchanged gates.

#### Scenario: A pinned observed source still permits useful advice

- **WHEN** stored native evidence identifies an unobserved qualified condition and the finite budget has room after mandatory pinned conditions
- **THEN** the native recommendation retains exact pins and exclusions and includes the unobserved condition within the existing count bound
- **AND** the actual application presents the exact ordered conditions, distinguishes mandatory conditions from coverage deficits, and applies them without changing the current object placement
- **AND** compilation rejects a translation that changes the selected conditions, start poses or split.

#### Scenario: The current application cannot represent the source

- **WHEN** stored authoring is absent or changed, current selection is incompatible, no eligible change remains, or the proposed sequence cannot preserve current placement and required transition bindings
- **THEN** the application exposes an unavailable reason and retains the current draft
- **AND** source-only advice is not presented as a complete pick/place transition or evidence of physical effectiveness.

#### Scenario: A person edits after inspecting advice

- **WHEN** the draft or selection changes after recommendation inspection
- **THEN** the previous choice is unavailable until a fresh derivation and current view establish eligibility
- **AND** a stale choice cannot overwrite the later edit; choosing to keep settings records the choice without changing the draft.

#### Scenario: Choice completion is ambiguous to the caller

- **WHEN** an apply or keep response is lost, the page is refreshed, or the same choice is delivered again
- **THEN** the Web surface reads the canonical choice result without automatically repeating the command; automation can read that same result
- **AND** duplicate or conflicting delivery cannot create another campaign or effect, changed stored evidence is revalidated before choice, and a failed recovery read leaves actions unavailable with explicit refresh available.

### Requirement: Observe canonical reviews across supported interfaces

The existing Collection review projection SHALL observe a canonical episode review
completed through another supported interface without issuing another decision.
It SHALL preserve the recorded reviewer and bind the result to the exact candidate,
immutable ledger and durable state. An unavailable or inconsistent observation
SHALL disable that review action without discarding unrelated execution facts or
changing physical or training authority. Observation SHALL NOT repair artifacts.

#### Scenario: A CLI review finishes while the Collection screen remains open

- **WHEN** the native review command durably resolves a queued candidate
- **THEN** a fresh view adopts its validated result and advances to the next pending candidate without a decision POST
- **AND** an old review intent cannot overwrite that decision or change its reviewer
- **AND** unchanged pending candidates do not trigger repeated full ledger validation.

### Requirement: Preserve execution awareness during Collection connection recovery

The Collection Web surface SHALL retain its last validated execution facts and current screen when state retrieval fails, clearly label them as stale, and state that current robot motion or stop is unknown. A failed browser request SHALL NOT imply that the lifecycle owner stopped, rolled back or remains healthy. The current constraint is the existing loopback state/intent transport; this behavior grants no offline command authority and changes no physical or approval gates. The next consumer is the existing lifecycle owner, whose fresh canonical view and exact intent CAS remain authoritative.

#### Scenario: State retrieval fails while execution continues

- **WHEN** a state read or watch fails during a previously observed execution
- **THEN** the screen retains the last episode, progress and evidence identities as explicitly stale information, without returning to environment setup or animating a live heartbeat
- **AND** the stop control remains visible with its existing pending semantics, but no command is emitted using the stale view; unavailable Web stop is not represented as confirmed physical stop.

#### Scenario: Recover current state without replaying effects

- **WHEN** a connection failure or HTTP 5xx prevents reading state
- **THEN** the browser makes at most three automatic read-only retries with delays, bounds each read's duration, and leaves explicit refresh available after exhaustion
- **AND** a fresh validated view restores only its native-permitted operations; a rollback or malformed view cannot change the retained identity or confer authority
- **AND** a response requested before a known disconnect cannot clear the stale indication; recovery requires a subsequent state read
- **AND** lost intent or stop responses cause state reads only, never automatic intent replay, including when one read supersedes another.

#### Scenario: Distinguish session and response failures

- **WHEN** the server rejects the session, returns an HTTP error, or returns an invalid state payload
- **THEN** the UI distinguishes these from a connection failure, preserves last-known execution context, and exposes the exact error in technical details
- **AND** session expiry offers the existing fresh-page bootstrap without token transcription; invalid contracts require explicit refresh rather than an unbounded retry loop.
