# Diagnose Em' All

A voice-driven diagnostic game powered by Azure OpenAI Realtime. Play as the
clinician, interview animated patients, request tests, and identify each disease.

## Requirements

- [uv](https://docs.astral.sh/uv/getting-started/installation/) for Python and project dependencies.
- Python 3.11 or newer for the audio runtime. Development is tested on Python 3.13/macOS.
- An Azure account with access to the configured Azure OpenAI Realtime deployment
- A Microsoft work or school account with access to the Azure OpenAI resource
- A working audio output device; microphone optional (typing is supported)
- Access to the `MAI-Thinking-1` deployment for post-consultation scoring (scoring failures do not undo a diagnosis)

## Setup

Install uv and make sure the `keyring` CLI with the `artifacts-keyring` backend
is installed and available on your PATH. The project uses the private Azure
Artifacts package feed:

```text
https://pkgs.dev.azure.com/mshealthil/HealthIL/_packaging/healthil_PublicPackages/pypi/simple/
```

This feed is configured as uv's default index in `pyproject.toml`, disabling the
implicit public PyPI index. Your account must have read access to the feed.
uv's subprocess keyring provider is enabled in the project configuration, and
the feed URL includes the non-secret username `VssSessionToken` so uv can request
credentials from keyring. Complete the Microsoft sign-in flow if prompted by the
Azure Artifacts credential provider. No manually configured PAT is required.

If keyring is already provisioned on your machine, reuse it. To install or update
the tool through the private feed with an existing working keyring provider:

```bash
uv tool install keyring --with artifacts-keyring --default-index https://VssSessionToken@pkgs.dev.azure.com/mshealthil/HealthIL/_packaging/healthil_PublicPackages/pypi/simple/ --keyring-provider subprocess
uv tool update-shell
```

On a new machine without keyring, first provision `keyring` and `artifacts-keyring`
using your organization's approved workstation setup; installing them from an
authenticated feed cannot bootstrap its own missing credential provider.
Restart your shell if needed after updating PATH. Keep credentials out of project
files and chat. The game's Azure sign-in is separate from package-feed authentication.

Then sync the project from the repository root:

```bash
keyring --list-backends
uv sync --locked --python 3.13
```

uv creates and manages `.venv` automatically and downloads Python if needed.
No manual environment creation or activation is required. Dependencies are declared
in `pyproject.toml` and pinned in `uv.lock` to keep dependency versions reproducible
across machines. Use `uv sync --locked` in CI to fail if the lockfile is out of date.
Use `uv add <package>` or `uv remove <package>` to change dependencies; use
`uv lock --upgrade` followed by `uv sync` to update locked versions.
Commit both `pyproject.toml` and `uv.lock` when dependencies change.

The common-cold kid enables the voice filter. Its NumPy, PyWORLD and setuptools
dependencies are locked in the default `voice` dependency group, so ordinary
`uv sync` and `uv run` include them without a separate requirements-file install.
Use `uv add --group voice <package>` to change voice dependencies.
For a lightweight environment, use `uv sync --locked --no-group voice` and
`uv run --no-group voice ...`. Without voice dependencies, set
`performance_profile.voice.enabled` to `false` in
[the kid's JSON](data/prompts/01_common_cold_kid.json), or run another persona.
The requirements files remain available only for legacy standalone auditions.
The pinned PyWORLD/setuptools combination may emit an upstream
`pkg_resources` deprecation warning.

Select **Sign in to Azure** when prompted and complete browser sign-in with an
authorized Microsoft work or school account. Azure CLI is not required.
Alternatively:

```bash
az login --scope https://cognitiveservices.azure.com/.default
```

The game reuses a valid Azure access token in memory between patient visits.
It requests a fresh token when fewer than five minutes remain before expiry,
or after Azure rejects the cached token. Fresh Azure CLI requests have a
60-second timeout; the consultation window remains responsive while connecting.
No tokens are written to game saves. Restart the game after changing Azure accounts.

If `Timed out waiting for Azure CLI` persists, verify `az login` completes in
your terminal, then use Retry. This failure occurs before the patient session
opens and does not remove completed cases.

Browser sign-in stays responsive and can be cancelled with **Return to Hospital**
or by closing the game. An abandoned sign-in times out after about three minutes
and can be retried. Tokens are held only in memory and reused between patients
until they approach expiry; restarting the game may require signing in again.
No passwords or tokens are written to game saves or logs.

Use an account with the **Cognitive Services OpenAI User** role (or equivalent
permissions) on the configured resource. Signing in does not grant resource
access: if Azure denies access, use another authorized account or ask the resource
owner to grant the required role. This development game uses the Azure Identity
SDK's default development application for browser sign-in; production deployments
should use their own registered Microsoft Entra application.

## Run

```bash
uv run python -m src.debug_example
```

Limit the roster by passing one or more prompt files:

```bash
uv run python -m src.debug_example data/prompts/01_common_cold_kid.json
```

VS Code includes **Start Diagnose 'Em All** and **Test Common Cold Kid** tasks.
Different rosters have independent progress; testing the kid alone does not
reset the full campaign.

## Controls and game loop

- Move with arrow keys; press Enter near a highlighted patient.
- In the hospital, press D to cycle **Groove -> Disco -> Robot -> stop**.
  Arrow keys also stop dancing and resume movement.
- Hold C to crouch; use arrow keys while holding C to crawl at half walking
  speed with an animated hands-and-knees gait. Stop moving to return to the
  stationary crouch. Release C to stand and resume normal walking.
  Crouching stops dancing.
- Press Space to jump, including from a crouch. Arrow keys let you move while
  airborne, but walls, patients, obstacles, and locked rooms still block you.
  Hold C when landing to return to a crouch. There is no double-jump or automatic
  repeat jump when holding Space.
- Dances and jumps pause with the hospital. Dance input is ignored while
  crouching or jumping; stand/land before entering a consultation. These keys
  still type normally during consultations. WASD movement is no longer used.
- Enter Pediatrics and the Diagnostics Lab through their corridor doors.
- Hold either Shift key to speak. Click the text field and press Enter to type.
- In chat, diagnosis, care-plan text fields and Dragon Copilot, use Cmd+V on
  macOS (Ctrl+V elsewhere) to paste. Cmd/Ctrl+A selects the whole field;
  Cmd/Ctrl+C copies and Cmd/Ctrl+X cuts the selection. Pasted line breaks become
  spaces; pasting never sends a message, submits a diagnosis or orders a test.
- Interview the patient in the diagnosis battle and request skills from the shared catalog.
- Submit the correct diagnosis, discuss the care plan, then choose Finish Visit to complete the case.
- Help patient 01 to unlock the Patient Ward.
- Help patient 02 to unlock the Pharmacy Lounge.
- Return to the same hospital position and find the next patient.
- Press Escape or click the pause icon for Resume, New Game, and Save & Quit.
- Closing every case in the current roster completes the hospital rounds.
- Close evidence using Enter, Escape or its close button.
- Escape leaves text focus first, otherwise opening the consultation exit prompt.

- Dr. Ash switches from idle to talking animation while push-to-talk is active.
- Patient speech and your responses appear in the conversation transcript.
- Scroll the transcript with the mouse wheel or Page Up / Page Down.
- Request a skill by voice or text. Review the interpreted skill and parameters, then explicitly confirm or cancel.
- Scroll long evidence reports with the wheel, arrow keys, or Page Up / Page Down.
- Click Diagnose or press F2 to open the final diagnosis dialog.
- Enter a diagnosis and click Submit Diagnosis or press Enter to commit it.
- Tab / Shift+Tab moves between the diagnosis field, Cancel, and Submit Diagnosis.
- Cancel or Escape returns to the conversation without submitting; your chat draft is preserved.
- Incorrect submissions leave the case open so you can continue investigating or try again.
- A correct submission gives every patient a personal thank-you, an animated happy
  gesture, and a brief burst of pixel confetti. It plays once per visit, stays clear
  of the chat controls, and does not add points or finish the visit. Care planning
  and conversation remain available throughout.
- The stopwatch starts when the patient connection is ready and stops when you finish the visit, including care planning.
- Click Tests Found or press F3 to review used skills, actual findings, and point rationales.
- Click Care Plan or press F4 to add prescriptions, add referrals, or review recorded orders.
- Click Skills or press F5 to browse the entire shared catalog for any patient. Use Up/Down, Page Up/Page Down, Home/End or the mouse wheel; Enter or click opens details and illustrated art. Details scroll independently. Enter or Draft Request fills the chat draft without sending it. Escape returns or closes.
- Click the Dragon Copilot logo-and-text button to ask for help during the visit. F6 also opens it; Escape, F6, or Return to Patient closes the helper.
- After confirming a diagnosis, the Diagnose button becomes Finish Visit (also F2). Confirm to end the call and open the scorecard.

The hospital pauses when unfocused; a consultation does not. Losing focus
releases push-to-talk. Returning to the hospital cancels the unfinished case.
Retry starts a new conversation, not recovery of the previous session.

Chat input and transcripts support Russian and Hebrew, including right-to-left
Hebrew text mixed with English and numbers. Chat uses bundled DejaVu Sans fonts
so no system font installation is needed; their license is in [data/fonts/LICENSE](data/fonts/LICENSE).

Only an explicit diagnosis submission followed by Finish Visit can close a case.
Mentioning a diagnosis in voice or text chat does not count, and the patient model
cannot declare a win. Diagnosis submissions
are checked locally against the configured `disease` name, ignoring capitalization,
spacing, and punctuation separators. Use the condition name rather than a sentence;
synonyms, abbreviations, and lists of possible diagnoses are not accepted in this
version. The microphone is disabled while the diagnosis dialog is open.

## Dragon Copilot

The in-visit Dragon Copilot helper uses the HAS HealthBot Direct Line protocol from the HAS Bench
observability component's underlying client. It is separate from the patient model
and MAI scoring. Type a question and press Enter or Send; scroll responses with the
mouse wheel or Page Up / Page Down. Your patient-chat draft is preserved. The patient
connection and visit stopwatch stay active, but microphone input is blocked while
the helper is open. Closing it cancels any pending local request. Failed or cancelled
questions stay in the input for manual retry; requests time out after 120 seconds.

Each question sends the observed clinician/patient transcript, discovered test names
and text findings, and recorded prescriptions/referrals. Image findings are marked
as unavailable rather than uploading images or local paths. Hidden case answers,
patient system prompts, and undiscovered tests are not sent. Successful helper turns
are replayed for follow-up questions within the same visit. Helper messages are not
added to the patient transcript, scorecard, or save file. HAS may retain requests
under its own service policy; cancelling locally cannot retract an already-sent
request. Use fictional game cases only, not real patient information.

The default HCP endpoint is `https://eastus.healthbot-dev.microsoft.com/account/obs-hcp-1-kz3jzz4`, using the HAS Bench scenario and Key Vault configuration:

| Environment Variable | Default / Purpose |
| --- | --- |
| `HAS_BOT_ID` | `obs-hcp-1-kz3jzz4`; bot account ID, not an Entra tenant ID |
| `HAS_SCENARIO` | `dsb_debug_scenario` |
| `HAS_KEY_VAULT_URL` | `https://hlsamlta4hwork0724448635.vault.azure.net/` |
| `HAS_SECRET_NAME` | `bot-<bot-id>-webchat-secret`; a canonical-name 404 tries the legacy `bot-<bot-id>-web-chat-secret` |
| `HAS_DIRECT_LINE_SECRET` | Optional Web Chat secret supplied securely through the environment; bypasses Key Vault |

Configure overrides before launching the game. Key Vault access uses the existing
Azure credential helper with the `https://vault.azure.net/.default` scope. An Azure
CLI sign-in with secret-read permission is required unless a token for that scope
is already cached or a Direct Line secret is supplied. Access to the game's OpenAI
resource does not grant HAS Key Vault access. Never put secret values in source,
debug configuration committed to git, or patient JSON.

Live verification on September 6, 2026 succeeded with `obs-hcp-1-kz3jzz4` and
`dsb_debug_scenario`: Key Vault secret lookup, Direct Line conversation creation,
scenario startup, and a response to a synthetic clinical question all completed.

HAS guidance is AI-generated educational assistance, not real medical advice;
it does not automatically order tests, prescribe, submit a diagnosis, or finish a visit.

## Prescriptions and Referrals

Care Plan is available before and after diagnosis confirmation. A prescription
records medication, directions (including dose, route and frequency as appropriate),
and reason. A referral records destination, reason, and Routine, Urgent, or Emergency
urgency. The forms do not suggest medication or calculate doses. All fields are
required to submit an order; submissions are simulated game decisions, not real
prescriptions, and form validation does not establish clinical safety.

Use Tab / Shift+Tab to move between fields and buttons. Enter advances through
fields and submits from the final field or submit button. Referral urgency supports
Left / Right arrow keys or direct clicks. Escape or Cancel discards the form without
issuing an order and preserves your conversation draft. Microphone input is blocked
while a form is open.

Issuing an order records it and sends its details to the patient as a conversation
message, so you can discuss it during the call. Identical duplicate orders are ignored.
Review Orders shows all recorded prescriptions and referrals. Neither is mandatory:
you may finish a visit without prescribing or referring when appropriate. Finish Visit
waits for pending outgoing messages to be sent. Order records and the transcript are
included in the final review but are not stored in local saves.

## Patient audio

The common-cold kid automatically uses the selected voice filter and inserts
coughs, sniffles, sneezes or throat-clears at suitable internal pauses. **No sound
requests are needed.** Every ordinary reply is an opportunity after the shared
20-second cooldown, with at most one cue per clinician turn. No safe gap means
no spontaneous cue. Shift or a typed message interrupts speech and cues.

Captions display complete speech segments at playback onset, with separate
audible cue indicators. They are not word-aligned. Speech processing adds
latency; it is not a clinically validated simulation.

For device failures, select a working system output and retry. If macOS changed
devices, restarting the game may help. Authentication, connection and audio
failures have distinct in-game recovery messages.

## Consultation Review

After Finish Visit, the realtime call closes and a scrollable review opens.
The first screen shows a compact numerical overview: Tests discovered (for example,
2/86), separate appropriate-use points, and all eight AI scores out of 100,
followed by the detailed feedback.
MAI-Thinking-1 scores the transcript on eight axes using a 0-5 rubric; the display
multiplies each score by 20 (for example, 4 becomes 80/100). The axes have written
feedback: clinical professionalism, warmth and pleasantness, empathy and listening,
communication clarity, history-taking, diagnostic reasoning, prescribing decisions,
and referral decisions. Diagnostic reasoning is labeled Clinical knowledge in the
overview. Axes with insufficient evidence receive 0/100 as a game scoring penalty;
the written feedback explains the lack of evidence rather than inventing observations.
While scoring runs, values show Pending; a failed request shows Unavailable and Retry.
This is AI-generated game feedback, not an assessment of real clinical competence.

Deterministic **appropriate-use points** are displayed separately from these AI
axes and remain available if the review service fails. The used-skills report and
scorecard show each action's status, descriptive result, and scoring rationale.
This score runs from **-20 to +20**, not a percentage. The default formula is
`clamp(sum(action.points), -20, 20)`: indicated decisions earn +2, reasonable
alternatives or low-burden baseline assessments +1, and unnecessary procedures -1.
Only evidence disclosed or obtained before confirmation can justify an order;
negative rule-out results can earn credit. A relevant-but-not-yet-supported order
earns 0 rather than hindsight credit. Repeating it after learning more does not
retroactively earn points.

Overlapping components share a +2 clinical-goal budget and linked burdens are
charged once. Distinct investigations such as influenza testing and SARS-CoV-2
PCR remain independently scoreable. Duplicate, unavailable and cancelled actions
earn 0; failed validation or missing evidence never completes a procedure.
The unclamped running total retains all rewards and penalties, so reaching a
display limit does not erase prior decisions. `ScoringConfig` in
`src/diagnostic_skills.py` exposes the weights, goal budget and bounds; configurable
limits stay inside -100..100 for save compatibility. These are authored
educational-game rubrics, not clinically validated scoring rules.

The report also shows elapsed consultation time and unique tests discovered out of
the configured total, plus their names. Repeating a test does not increase discovery.
Connection setup and scoring time are excluded from the stopwatch; reading evidence,
diagnosis retries, and time outside the focused window are included. Time and test
coverage are descriptive statistics, not incentives to rush or order every test.

Scoring uses the existing Azure CLI sign-in and sends the full text transcript,
case context, diagnosis, elapsed time, discovered test results, and recorded care orders to:

```text
https://tabaumel-resource.services.ai.azure.com/mai/v1/chat/completions
model: MAI-Thinking-1
```

The skill engine provides descriptive text evidence, including an explicit
description for the legacy thermometer; illustrations are not interpreted as
patient findings or uploaded. No raw audio is sent to the scoring endpoint.
Transcripts and detailed scorecards are not added to local saves.

Scroll with the wheel, arrow keys, or Page Up / Page Down. Return to Hospital,
Enter, and Escape wait until scoring finishes before allowing you to close the review.
While scoring is pending, the return button is disabled and shows Scoring...
Failed scoring unlocks Return and offers Retry (F5); this retries only the score
request, not the consultation. Closing the game window can still cancel scoring.
Scoring has a 120-second overall deadline and does not automatically retry paid
requests. A finished visit remains complete if scoring fails or is cancelled.
Closing the review window saves the completed case before exiting the hospital.

## Patient configuration

Each file under [data/prompts/](data/prompts/) defines `system_prompts`, `disease`,
`patient_type`, and a nonempty `tests` list. Test `results` may be text or an image
path. Optional test `audio` accepts a nonempty mono 24 kHz PCM16 WAV of at most
10 seconds; it plays through the same interruptible output queue.

The bundled patients also define `age` (integer years, 0-120) and `gender`
(nonempty text). These are authored fictional character choices informed by the
artwork, not demographics inferred with certainty from appearance. The loader
adds them to the conversation instructions; both fields remain optional for
custom patients. The personas specify age-appropriate vocabulary, speaking pace,
concerns, and reactions without changing the authored clinical results. They
discourage assistant-style replies, repeated catchphrases, and narrated gestures.
Vocal delivery remains model-dependent; prompts do not guarantee a particular
voice timbre. Only the common-cold kid has the optional local voice filter enabled.

The optional `performance_profile` independently configures delivery, voice
processing, catalog-selected cues and scheduling. Only the common-cold kid is
enabled by default.

See **[NPC audio performance](docs/audio-performance.md)** for the JSON schema,
runtime architecture, cue catalog, source licenses, offline auditions and limits.

## Saved progress

Completed cases and hospital position save before consultations, after finished
visits, and on exit. The active patient roster resumes automatically; different
rosters have independent progress. New Game resets only the current roster after
confirmation.

- macOS: `~/Library/Application Support/DiagnoseEmAll/progress.json`
- Windows: `%LOCALAPPDATA%/DiagnoseEmAll/progress.json`
- Linux: `$XDG_DATA_HOME/diagnose_em_all/progress.json` or
  `~/.local/share/diagnose_em_all/progress.json`

Saves contain patient identifiers, completion flags, coordinates, and optional
completed-visit appropriate-use totals, not
conversation transcripts, prompts, audio, or credentials. Invalid restored
positions fall back to the hospital entrance. Corrupt or unsupported saves are
preserved and reported; explicitly starting a New Game backs up such a file
to `progress.json.bak` before replacing it. An existing backup is never overwritten.
Write failures keep the previous save and allow you to continue playing.

## Tests and developer tools

Preview navigation without Azure or audio hardware (no progress is saved;
selecting a patient exits this navigation-only preview):

```bash
uv run python -m tools.preview_game --interactive
```

Export hospital, consultation, evidence, completion, and animation contact sheets:

```bash
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy uv run python -m tools.preview_game
```

Images go to `.artifacts/polish`. Use `--size 480`, `--size 720`, or `--size 960`
to check window scaling, and `--output-dir` to keep separate sets.

Run the offline regression suite and syntax checks:

```bash
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy uv run python -m unittest discover -s tests -v
uv run python -m compileall -q src tests tools
```

The suite uses unittest, mocked services and controlled audio sinks; it does not
authenticate to Azure or use microphone/speaker hardware. Processing tests use
the installed optional voice libraries. Audio preparation tests also need
`ffmpeg`; they report a skip when it is unavailable. Tests generate their own
temporary listening fixtures, not files from a prior developer session.

The tests exercise movement, camera timing, atlas validity and fallback, modal
input, token reuse/expiry and authentication recovery, connection cancellation,
evidence layout, saves, room unlocks, and a
complete eleven-case campaign using stubbed consultation results. They do not
authenticate to Azure or access microphone/speaker hardware.

To verify the scoring deployment with one synthetic consultation (incurs normal
model usage, with no microphone or speaker access):

```bash
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy uv run python -m tools.smoke_review --live
```

This prints the validated eight-axis scorecard and exports top/bottom screenshots
to `.artifacts/polish`. Use `--output path/to/scorecard.png` to choose another location.

An optional live smoke test sends one real request to the configured deployment,
captures the returned transcript/audio, and exports a screenshot without opening
the microphone or playing sound. It requires Azure authentication and incurs
normal service usage:

```bash
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy uv run python -m tools.smoke_conversation --live
```

The live tool accepts `--message` for an ordinary clinician question and
`--speech` for supplied input audio. `--capture-dir NEW_DIR --line "..."` saves
the first complete generated speech part with a neutral delivery and disables
local cues/tests for source collection. Check its actual transcript rather than
assuming verbatim generation. Live mode requires authentication and incurs
normal Azure usage. Never upload private recordings to public conversion demos.

## Adding patients and artwork

1. Add a `PatientType` member and matching `PATIENT_SPRITE_SHEETS` entry in enum order in
   [realtime_conversation.py](src/realtime_conversation.py).
2. Add coordinates in [hospital_game.py](src/hospital_game.py).
3. Add a matching JSON file under [data/prompts/](data/prompts/).
4. Add a transparent 1024x1024 atlas under [patient sprites](data/sprites/patients/):
   a 4x4 grid of 256x256 cells, with idle/talking/worried/relieved rows.
   Use the sprite name plus `_atlas.png`. Keep full-body characters centered with
   feet on a common baseline. Legacy 280x660 cards remain supported when no matching atlas exists.
5. Add explicit skill outcomes and appropriateness rules in `data/skills/cases.json`
   and extend the engine's case coverage tests. A new prompt alone must not
   inherit invented normal results for the universal catalog.

Player walk/idle atlases use six/four columns and down/left/right/up rows.
Runtime validation checks dimensions, blank frames and clipped silhouettes,
falling back to legacy assets when needed. Source art and prompts are retained.
Use `python -m tools.prepare_atlas --help` and inspect exported contact sheets
when preparing replacements.

## Universal diagnostic skills
The existing per-patient tests remain authoritative results, not an availability
filter. The universal catalog and explicit fictional case extensions live in
`data/skills`: **86 skills** (68 illustrated entries plus 18 distinct legacy
entries), with **946 explicitly authored simulated skill/case mappings** across
the original eleven patients. These are fictional teaching outcomes, not
clinically validated findings. Skills not supported by a
case's specimen, target, consent or workflow report what is missing instead of
inventing a normal finding. Advanced skills do not imply that new oncology,
inherited-condition, recurrent-infection or chronic-diarrhea cases exist.

### Universal skills and language routing

The realtime model interprets both speech and typed requests through stable-ID
function tools. Every case receives the same neutral descriptions, aliases, three
example phrasings and parameter schemas. There is no fixed-sentence dispatcher
and no patient-relevance or point information in the tool descriptions.
Underspecified requests such as "blood tests" or "sequence it" require clarification.
Negated, hypothetical, educational and mention-only requests must not invoke a
procedure. As a second safeguard, **nothing executes or scores until you approve
the interpreted action locally**. Cancel is the default; Tab/Left/Right changes
selection, Enter confirms the selected button, and Escape cancels. Long details
scroll. Misrecognitions can therefore be cancelled without a completed procedure.

Complete tool batches are processed in order with one model continuation per
batch. Replayed call IDs are ignored, incomplete/cancelled model responses do not
execute, and repeated or overlapping procedures cannot farm points. Finish Visit
waits while a skill proposal or evidence report is pending. History, diary review,
pedigree and counseling skills require the relevant conversation/review, not just
ordering the skill or making a counseling referral.

The read-only `get_skill_context` tool returns indices for already-visible
clinician/patient dialogue only, never hidden diagnoses, rubrics, or undisclosed
findings. History/consent tools cite those indices; the backend checks the actual
speaker, exchange and specific consent, including withdrawal. Existing sleep-diary
and Epworth records remain available for the patient to discuss when asked.
Evidence checks deliberately use conservative English patterns for this roster;
unsupported or ambiguous phrasing must be clarified rather than supplied as a
model-authored fact. Live paraphrase/consent behavior still needs the separate
playtest.

Catalog art is conceptual, never patient-specific diagnostic evidence. The
original blue/ivory thermometer remains the authoritative legacy image. Viral
metagenomic sequencing uses only the selected larger handheld nanopore
illustration. Its nasal DNA, RNA and combined workflows have authored simulated
results and are unnecessary for these simple current cases; DNA-only results never
exclude RNA infection. Sequencing is simulated: there are no sequencing services, uploads,
or real clinical orders. Microbiome profiling is research-only.

Targeted nasal PCR supports SARS-CoV-2 and influenza A/B for every patient,
including the common-cold kid. Say "Do a COVID PCR on a nasal swab" or "Please
test a nasal swab for influenza A and B by PCR." Asking for both produces two
separately confirmed proposals; "do PCR" alone needs target/specimen clarification.
The cold case has authored negative results for both; the influenza case has
influenza A detected. These are simulated findings, not sequencing or antigen
tests. Each PCR target is tracked independently for repeats and results; influenza
PCR and rapid influenza antigen testing share a positive reward budget, while
COVID and influenza investigations remain separate. Existing evidence-at-order
scoring rules still apply; PCR is available, not a mandatory cold workup.

For one broader test, say **"Run one respiratory viral PCR panel on a nasal
swab."** This is one confirmed order under the PCR skill, covering SARS-CoV-2,
influenza A/B, rhinovirus/enterovirus (combined), RSV, adenovirus, human
metapneumovirus, parainfluenza 1-4 and seasonal coronaviruses
229E/NL63/OC43/HKU1. It does **not** cover every cold virus or bacteria such as
group A Streptococcus, and it is not sequencing. The cold case's authored panel
detects rhinovirus/enterovirus; the influenza case detects influenza A.
Each report lists the covered targets and interpretation limits.

The broad panel remains performable but scores as unnecessary (-1) in the
current uncomplicated cases, rather than rewarding its positive result in
hindsight. It is one action, not a separate charge per virus. Completed panel
components cover subsequent COVID/flu PCR requests without additional
execution or points. A panel after targeted PCR only adds the remaining
components; repeats add nothing. Restart an already-running game to load changes.

### Deferred live playtest

Automated protocol fixtures establish local guards, not the live model's ability
to route natural language. Interactive speech/text testing is a separate task;
do not start a second game while an existing instance is running. When ready:

```bash
.venv/bin/python -m src.debug_example data/prompts/07_feverish_patient.json
# Entire existing roster:
.venv/bin/python -m src.debug_example
```

1. Interview the feverish patient; paraphrase an appropriate request such as "check how hot I am", confirm temperature, and inspect text evidence and rationale.
2. Browse all skills; request nasal viral metagenomic sequencing with an RNA-capable workflow (or noncontrast chest CT), confirm it, and inspect the simulated result and penalty.
3. Ask "blood tests" and "sequence it"; expect clarification, not execution. Supply the missing specimen/target when appropriate.
4. Try a negated request, a hypothetical and an educational question. No execution should occur; cancel any erroneous proposal and verify no completed action or points.
5. Request temperature twice and a compound request with overlapping components; confirm intended actions and verify no duplicate credit or charge.
6. Cancel a proposal, inspect used results, then submit the diagnosis, discuss care, Finish Visit, review separate points/AI feedback, and save/resume hospital progress.

## Project layout

| Path | Purpose |
| --- | --- |
| `data/` | Patient prompts and game artwork |
| `data/skills/` | Shared skill metadata, illustrations and simulated case outcomes |
| `assets/audio/` | Source recordings, prepared cues, catalog and license records |
| `src/` | Game, Realtime routing, playback, cue timing and shared voice engine |
| `tools/` | Offline auditions, asset preparation, previews and opt-in live checks |
| `tests/` | Offline regressions and shared audio test doubles |
| `docs/` | Detailed audio configuration and design |
| `.artifacts/` | Ignored local developer outputs |

Generated cue listening packs also remain locally under `assets/audio/`, but
are ignored by Git. Do not force-add them to a PR. Source recordings and
prepared gameplay candidates remain versioned with provenance.

## Player Animation Assets

The hospital prefers `data/sprites/players/dr_ash_walk_prepared.png` (six columns)
and `dr_ash_idle_atlas.png` (four columns). Both use four direction rows in
down, left, right, up order, with `256x256` cells. Runtime validation rejects
wrong dimensions, blank frames, or silhouettes touching cell boundaries and
falls back to the original directional sheet if either new atlas is invalid.
All directions and both states share one normalization scale and foot baseline.
Consultation sprites and legacy `128x200` frame contracts are unchanged.

The D-key dances use `dr_ash_dance_atlas.png` (Groove), `dr_ash_disco_atlas.png`,
and `dr_ash_robot_atlas.png`: four frames per direction in the same
down/left/right/up row order, looping at six frames per second. Each sheet is
normalized separately to preserve the existing walk/idle scale. The matching
`*_source.png` files preserve full-resolution artwork from the Azure
image-generation skill, using the idle atlas as the character reference.

`dr_ash_actions_atlas.png` uses the same grid, with standing, crouching, takeoff,
and airborne poses in columns 1-4. One scale across all action frames keeps the
crouch visibly shorter instead of stretching it to standing height. Jumps last
0.6 seconds with a 40-world-pixel parabolic lift; the shadow, camera target,
collision footprint, and depth ordering stay anchored to the ground position.
All action atlases are required and validated at startup rather than silently
substituting walking frames.

`dr_ash_crawl_atlas.png` adds four hands-and-knees poses per direction. Crawling
uses a lower 62-pixel canvas and advances one cycle per 72 world pixels actually
traveled, including wall sliding. It stops animating when blocked or stationary
and returns to the crouch pose. It has no standing walk bob or dust. The atlas
preparer fits wide silhouettes within the cell width as well as its height so
outstretched crawling limbs are not clipped.

The offline preview exports `dance.png`, `dance_disco.png`, `dance_robot.png`,
their `*_frames.png` contact sheets, and `crouch.png`, `jump.png`, `landed.png`,
`action_frames.png`, `crawl_frames.png`, and `crawl_motion.png` (four successive
gameplay frames) alongside the existing animation previews.

The prepared walk atlas retains the original down/up rows and uses the repaired
left/right rows from `dr_ash_side_walk_source.png`. Side cycles include contact,
recoil, and narrow passing poses for each leg. They advance once per 80 world
pixels traveled, including reduced movement along obstacles; no extra vertical
lift is applied to their planted feet.

The generated white-background sources, initial walk candidate, and prompts are
preserved next to the prepared assets. White-background generation avoided
colored fringe in the model's transparent output. The local preparation tool
removes only the connected exterior background, keeps enclosed coat whites,
extracts isolated figures in row order, and repacks a strict transparent grid:

```bash
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy uv run python -m tools.prepare_atlas data/sprites/players/dr_ash_walk_source.png .artifacts/dr_ash_walk_original.png --columns 6
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy uv run python -m tools.prepare_atlas data/sprites/players/dr_ash_side_walk_source.png .artifacts/dr_ash_side_walk.png --columns 6
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy uv run python -m tools.prepare_atlas data/sprites/players/dr_ash_idle_source.png data/sprites/players/dr_ash_idle_atlas.png --columns 4
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy uv run python -m tools.prepare_atlas data/sprites/players/dr_ash_dance_source.png data/sprites/players/dr_ash_dance_atlas.png --columns 4
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy uv run python -m tools.prepare_atlas data/sprites/players/dr_ash_disco_source.png data/sprites/players/dr_ash_disco_atlas.png --columns 4
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy uv run python -m tools.prepare_atlas data/sprites/players/dr_ash_robot_source.png data/sprites/players/dr_ash_robot_atlas.png --columns 4
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy uv run python -m tools.prepare_atlas data/sprites/players/dr_ash_actions_source.png data/sprites/players/dr_ash_actions_atlas.png --columns 4
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy uv run python -m tools.prepare_atlas data/sprites/players/dr_ash_crawl_source.png data/sprites/players/dr_ash_crawl_atlas.png --columns 4
```

For a walk-atlas rebuild, replace only the original atlas's second and third
rows (y=256 through 767) with those from the prepared side-walk source. Preserve
transparency when replacing rows; do not alpha-blend onto the old figures.

Inspect exported contact sheets after generating or preparing replacements.
Existing patient atlases are reused, with independent animation phases and
idle/greeting/worried/relieved behavior in the hospital.

## Module reference

```text
data/
  prompts/           Patient conversation configurations
  sprites/
    patients/        One animation sheet per patient
    players/         Dr. Ash battle and directional animation sheets
    tests/           Images displayed as diagnostic results
    world/           Generated hospital floor and environment art
src/
  debug_example.py           Executable game launcher
  hospital_game.py           Overworld, movement, and battle transitions
  realtime_conversation.py   Realtime client, tools, and animation UI
  game_progress.py           Validated, atomic local progress storage
  game_ui.py                 Shared choice menus and text wrapping
  consultation_review.py     Stopwatch, discovery metrics, and MAI transcript scoring
  care_plan.py               Simulated prescription and referral records
  care_plan_ui.py            Prescription and referral forms
  diagnostic_skills.py       Shared catalog, case outcomes and deterministic scoring
  skill_browser.py           Scrollable universal catalog and conceptual art
  skill_confirmation.py      Local confirmation of interpreted requests
  skill_routing.py           Completed realtime tool-batch deduplication
  animation_assets.py        Strict transparent atlas loader
tests/                       Offline regression tests
tools/                       Preview, atlas preparation, and opt-in live checks
```
