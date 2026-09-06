# Diagnose Em' All

A voice-driven diagnostic game powered by the Azure OpenAI Realtime API. Play as the clinician, interview an animated patient, request diagnostic tests, and identify the correct disease.

## Requirements

- [uv](https://docs.astral.sh/uv/getting-started/installation/) (manages Python 3.10 or newer and project dependencies)
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
uv sync --locked
```

uv creates and manages `.venv` automatically and downloads Python if needed.
No manual environment creation or activation is required. Dependencies are declared
in `pyproject.toml` and pinned in `uv.lock` to keep dependency versions reproducible
across machines. Use `uv sync --locked` in CI to fail if the lockfile is out of date.
Use `uv add <package>` or `uv remove <package>` to change dependencies; use
`uv lock --upgrade` followed by `uv sync` to update locked versions.
Commit both `pyproject.toml` and `uv.lock` when dependencies change.

When a consultation needs authentication, select **Sign in to Azure** in the
game. Complete Microsoft sign-in in the browser; the consultation continues
automatically. Azure CLI is not required for this flow.

Alternatively, if you have Azure CLI installed, authenticate before launching:

```bash
az login
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

Start the hospital with every patient from `data/prompts`:

```bash
uv run python -m src.debug_example
```

Pass one or more prompt files to limit the hospital roster:

```bash
uv run python -m src.debug_example data/prompts/11_eccentric_neighbor.json
```

## Game Loop

- Move Dr. Ash through the hospital with the arrow keys or WASD.
- Enter Pediatrics and the Diagnostics Lab through their corridor doors.
- Approach a patient and press Enter when the patient is highlighted.
- Interview the patient in the diagnosis battle and request configured tests.
- Submit the correct diagnosis, discuss the care plan, then choose Finish Visit to complete the case.
- Help patient 01 to unlock the Patient Ward.
- Help patient 02 to unlock the Pharmacy Lounge.
- Return to the same hospital position and find the next patient.
- Press Escape or click the pause icon for Resume, New Game, and Save & Quit.
- Closing every case in the current roster completes the hospital rounds.

During a diagnosis battle:

- Hold either Shift key to speak.
- Dr. Ash switches from idle to talking animation while push-to-talk is active.
- Click the text field to type, then press Enter or click Send.
- Patient speech and your responses appear in the conversation transcript.
- Scroll the transcript with the mouse wheel or Page Up / Page Down.
- Ask the patient to perform one of the configured diagnostic tests.
- Scroll long evidence reports with the wheel, arrow keys, or Page Up / Page Down.
- Close evidence with Enter, Escape, or its close button.
- Escape leaves text focus first; otherwise it opens a return-to-hospital confirmation.
- Click Diagnose or press F2 to open the final diagnosis dialog.
- Enter a diagnosis and click Submit Diagnosis or press Enter to commit it.
- Tab / Shift+Tab moves between the diagnosis field, Cancel, and Submit Diagnosis.
- Cancel or Escape returns to the conversation without submitting; your chat draft is preserved.
- Incorrect submissions leave the case open so you can continue investigating or try again.
- The stopwatch starts when the patient connection is ready and stops when you finish the visit, including care planning.
- Click Tests Found or press F3 to review tests you have discovered by requesting them.
- Click Care Plan or press F4 to add prescriptions, add referrals, or review recorded orders.
- After confirming a diagnosis, the Diagnose button becomes Finish Visit (also F2). Confirm to end the call and open the scorecard.

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

The hospital pauses when the window loses focus. A live consultation is not
paused: losing focus releases push-to-talk, while returning to the hospital
cancels the unfinished case. Authentication failures offer Sign in to Azure or
Return to Hospital; other connection failures offer Retry or Return to Hospital.
Retrying starts a new consultation, not a recovered conversation.

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

## Saved Progress

Completed cases and your hospital position save before consultations, after
finished visits, and when leaving the hospital. The active patient roster
resumes automatically; different rosters have independent progress. New Game
requires confirmation and resets only the current roster.

Save locations:

- macOS: `~/Library/Application Support/DiagnoseEmAll/progress.json`
- Windows: `%LOCALAPPDATA%/DiagnoseEmAll/progress.json`
- Linux: `$XDG_DATA_HOME/diagnose_em_all/progress.json`, or
  `~/.local/share/diagnose_em_all/progress.json`

Saves contain only patient identifiers, completion flags, and coordinates, not
conversation transcripts, prompts, audio, or credentials. Invalid restored
positions fall back to the hospital entrance. Corrupt or unsupported saves are
preserved and reported; explicitly starting a New Game backs up such a file
to `progress.json.bak` before replacing it. An existing backup is never overwritten.
Write failures keep the previous save and allow you to continue playing.

## Offline Preview and Tests

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

Add `--speech path/to/speech.wav` to test supplied mono 24 kHz, 16-bit PCM speech
instead of typed input. This does not test physical microphone/speaker devices.

## Patient Configuration

Each patient has a JSON file in `data/prompts`. The file maps directly to the parameters accepted by `strat_conversation`:

```json
{
  "system_prompts": "Patient personality and symptom instructions.",
  "disease": "diagnosis hidden from the clinician",
  "patient_type": "PATIENT_TYPE_ENUM_MEMBER",
  "tests": [
    {
      "description": "test name",
      "results": "Text result or path to an image"
    }
  ]
}
```

A test result can be plain text or an image path such as `data/sprites/tests/thermometer.png`.

## Add A Patient

1. Add a member to `PatientType` in `src/realtime_conversation.py`.
2. Add the patient's sprite-sheet filename to `PATIENT_SPRITE_SHEETS` in the same order as the enum.
3. Place a transparent `1024x1024` PNG atlas in `data/sprites/patients` using
  the same base name plus `_atlas` (for example, `12_new_patient_atlas.png`).
4. Add the patient's hospital coordinates to `PATIENT_POSITIONS` in `src/hospital_game.py`.
5. Add a matching JSON configuration in `data/prompts`.

Patient atlases use a strict `4x4` grid of `256x256` cells. Rows are idle,
talking, worried, and relieved; each row contains four animation frames. Keep
every full-body character centered on a transparent background with feet on the
same baseline. Legacy `280x660` patient cards remain supported when no matching
`_atlas.png` file exists.

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
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy uv run python -m tools.prepare_atlas data/sprites/players/dr_ash_walk_source.png .artifacts/dr_ash_walk_original.png --columns 6
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy uv run python -m tools.prepare_atlas data/sprites/players/dr_ash_side_walk_source.png .artifacts/dr_ash_side_walk.png --columns 6
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy uv run python -m tools.prepare_atlas data/sprites/players/dr_ash_idle_source.png data/sprites/players/dr_ash_idle_atlas.png --columns 4
```

For a walk-atlas rebuild, replace only the original atlas's second and third
rows (y=256 through 767) with those from the prepared side-walk source. Preserve
transparency when replacing rows; do not alpha-blend onto the old figures.

Inspect exported contact sheets after generating or preparing replacements.
Existing patient atlases are reused, with independent animation phases and
idle/greeting/worried/relieved behavior in the hospital.

## Project Layout

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
