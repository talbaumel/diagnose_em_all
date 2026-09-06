# Diagnose 'Em All

A voice-driven diagnostic game powered by the Azure OpenAI Realtime API. Play as the clinician, interview an animated patient, request diagnostic tests, and identify the correct disease.

## Requirements

- Python 3.10 or newer
- An Azure account with access to the configured Azure OpenAI Realtime deployment
- A Microsoft work or school account with access to the Azure OpenAI resource
- A working audio output device; microphone optional (typing is supported)

## Setup

Create and activate a virtual environment, then install the dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

When a consultation needs authentication, select **Sign in to Azure** in the
game. Complete Microsoft sign-in in the browser; the consultation continues
automatically. Azure CLI is not required for this flow.

Alternatively, if you have Azure CLI installed, authenticate before launching:

```bash
az login
```

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
python -m src.debug_example
```

Pass one or more prompt files to limit the hospital roster:

```bash
python -m src.debug_example data/prompts/11_eccentric_neighbor.json
```

## Game Loop

- Move Dr. Ash through the hospital with the arrow keys or WASD.
- Enter Pediatrics and the Diagnostics Lab through their corridor doors.
- Approach a patient and press Enter when the patient is highlighted.
- Interview the patient in the diagnosis battle and request configured tests.
- Make the correct diagnosis to mark that patient as complete.
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
- Diagnose the disease to win.

The hospital pauses when the window loses focus. A live consultation is not
paused: losing focus releases push-to-talk, while returning to the hospital
cancels the unfinished case. Authentication failures offer Sign in to Azure or
Return to Hospital; other connection failures offer Retry or Return to Hospital.
Retrying starts a new consultation, not a recovered conversation.

## Saved Progress

Completed cases and your hospital position save before consultations, after
successful diagnoses, and when leaving the hospital. The active patient roster
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
python -m tools.preview_game --interactive
```

Export hospital, consultation, evidence, completion, and animation contact sheets:

```bash
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy python -m tools.preview_game
```

Images go to `.artifacts/polish`. Use `--size 480`, `--size 720`, or `--size 960`
to check window scaling, and `--output-dir` to keep separate sets.

Run the offline regression suite and syntax checks:

```bash
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy python -m unittest discover -s tests -v
python -m compileall -q src tests tools
```

The tests exercise movement, camera timing, atlas validity and fallback, modal
input, connection cancellation, evidence layout, saves, room unlocks, and a
complete eleven-case campaign using stubbed consultation results. They do not
authenticate to Azure or access microphone/speaker hardware.

An optional live smoke test sends one real request to the configured deployment,
captures the returned transcript/audio, and exports a screenshot without opening
the microphone or playing sound. It requires Azure authentication and incurs
normal service usage:

```bash
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy python -m tools.smoke_conversation --live
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

The generated white-background sources, initial walk candidate, and prompts are
preserved next to the prepared assets. White-background generation avoided
colored fringe in the model's transparent output. The local preparation tool
removes only the connected exterior background, keeps enclosed coat whites,
extracts isolated figures in row order, and repacks a strict transparent grid:

```bash
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy python -m tools.prepare_atlas data/sprites/players/dr_ash_walk_source.png data/sprites/players/dr_ash_walk_prepared.png --columns 6
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy python -m tools.prepare_atlas data/sprites/players/dr_ash_idle_source.png data/sprites/players/dr_ash_idle_atlas.png --columns 4
```

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
  animation_assets.py        Strict transparent atlas loader
tests/                       Offline regression tests
tools/                       Preview, atlas preparation, and opt-in live checks
```
