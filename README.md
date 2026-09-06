# Diagnose Em' All

A voice-driven diagnostic game powered by Azure OpenAI Realtime. Play as the
clinician, interview animated patients, request tests, and identify each disease.

## Requirements

- Python 3.11 or newer. Development is tested on Python 3.13/macOS.
- An Azure account with access to the configured Azure OpenAI Realtime deployment
- A Microsoft work or school account with access to the Azure OpenAI resource
- A working audio output device; microphone optional (typing is supported)
- Access to the `MAI-Thinking-1` deployment for post-consultation scoring (scoring failures do not undo a diagnosis)

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-voice.txt
```

The common-cold kid enables the optional voice filter, so install both manifests
for the default campaign. Without voice dependencies, set
`performance_profile.voice.enabled` to `false` in
[the kid's JSON](data/prompts/01_common_cold_kid.json), or run another persona.
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
python -m src.debug_example
```

Limit the roster by passing one or more prompt files:

```bash
python -m src.debug_example data/prompts/01_common_cold_kid.json
```

VS Code includes **Start Diagnose 'Em All** and **Test Common Cold Kid** tasks.
Different rosters have independent progress; testing the kid alone does not
reset the full campaign.

## Controls and game loop

- Move with arrow keys or WASD; press Enter near a highlighted patient.
- Enter Pediatrics and the Diagnostics Lab through their corridor doors.
- Hold either Shift key to speak. Click the text field and press Enter to type.
- Interview the patient, request diagnostic tests, and state your diagnosis.
- Submit the correct diagnosis, discuss the care plan, then choose Finish Visit.
- Scroll transcripts with the wheel or Page Up / Page Down.
- Close evidence using Enter, Escape or its close button.
- Escape leaves text focus first, otherwise opening the consultation exit prompt.
- In the hospital, Escape or the pause button opens Resume, New Game and Save & Quit.
- Solving cases unlocks additional rooms; clearing the roster completes the rounds.

- Dr. Ash switches from idle to talking animation while push-to-talk is active.
- Patient speech and your responses appear in the conversation transcript.
- Scroll long evidence reports with the wheel, arrow keys, or Page Up / Page Down.
- Click Diagnose or press F2 to open the final diagnosis dialog.
- Enter a diagnosis and click Submit Diagnosis or press Enter to commit it.
- Tab / Shift+Tab moves between the diagnosis field, Cancel, and Submit Diagnosis.
- Cancel or Escape returns to the conversation without submitting; your chat draft is preserved.
- Incorrect submissions leave the case open so you can continue investigating or try again.
- The stopwatch starts when the patient connection is ready and stops when you finish the visit, including care planning.
- Click Tests Found or press F3 to review tests you have discovered by requesting them.
- Click Care Plan or press F4 to add prescriptions, add referrals, or review recorded orders.
- Click the Dragon Copilot logo-and-text button to ask for help during the visit. F6 also opens it; Escape, F6, or Return to Patient closes the helper.
- After confirming a diagnosis, the Diagnose button becomes Finish Visit (also F2). Confirm to end the call and open the scorecard.

The hospital pauses when unfocused; a consultation does not. Losing focus
releases push-to-talk. Returning to the hospital cancels the unfinished case.
Retry starts a new conversation, not recovery of the previous session.

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
2/8) and all eight scores out of 100, followed by the detailed feedback.
MAI-Thinking-1 scores the transcript on eight axes using a 0-5 rubric; the display
multiplies each score by 20 (for example, 4 becomes 80/100). The axes have written
feedback: clinical professionalism, warmth and pleasantness, empathy and listening,
communication clarity, history-taking, diagnostic reasoning, prescribing decisions,
and referral decisions. Diagnostic reasoning is labeled Clinical knowledge in the
overview. Axes with insufficient evidence receive 0/100 as a game scoring penalty;
the written feedback explains the lack of evidence rather than inventing observations.
While scoring runs, values show Pending; a failed request shows Unavailable and Retry.
This is AI-generated game feedback, not an assessment of real clinical competence.

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

Image-only test results are represented by their configured paths, not uploaded
images; the grader is instructed not to infer image contents. No raw audio is sent
to the scoring endpoint. Transcripts and scorecards are not added to local saves.

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

Saves contain no conversation transcripts, audio or credentials. Corrupt or
unsupported saves are preserved and reported. Starting New Game backs up such
a file before replacement; an existing backup is never overwritten. Failed
writes leave the previous save intact.

## Tests and developer tools

```bash
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy python -m unittest discover -s tests -v
python -m compileall -q src tests tools
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
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy python -m tools.smoke_review --live
```

This prints the validated eight-axis scorecard and exports top/bottom screenshots
to `.artifacts/polish`. Use `--output path/to/scorecard.png` to choose another location.

An optional live smoke test sends one real request to the configured deployment,
captures the returned transcript/audio, and exports a screenshot without opening
the microphone or playing sound. It requires Azure authentication and incurs
normal service usage:

```bash
# Navigation preview without Azure, audio hardware or progress changes
python -m tools.preview_game --interactive

# Export UI and animation contact sheets under .artifacts/polish
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy python -m tools.preview_game

# Explicitly paid/live service check; no microphone or speaker output
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy python -m tools.smoke_conversation --live
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

Player walk/idle atlases use six/four columns and down/left/right/up rows.
Runtime validation checks dimensions, blank frames and clipped silhouettes,
falling back to legacy assets when needed. Source art and prompts are retained.
Use `python -m tools.prepare_atlas --help` and inspect exported contact sheets
when preparing replacements.

## Project layout

| Path | Purpose |
| --- | --- |
| `data/` | Patient prompts and game artwork |
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
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy python -m tools.prepare_atlas data/sprites/players/dr_ash_walk_source.png .artifacts/dr_ash_walk_original.png --columns 6
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy python -m tools.prepare_atlas data/sprites/players/dr_ash_side_walk_source.png .artifacts/dr_ash_side_walk.png --columns 6
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy python -m tools.prepare_atlas data/sprites/players/dr_ash_idle_source.png data/sprites/players/dr_ash_idle_atlas.png --columns 4
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
  animation_assets.py        Strict transparent atlas loader
tests/                       Offline regression tests
tools/                       Preview, atlas preparation, and opt-in live checks
```
