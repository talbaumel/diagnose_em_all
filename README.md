# Diagnose 'Em All

A voice-driven diagnostic game powered by the Azure OpenAI Realtime API. Play as the clinician, interview an animated patient, request diagnostic tests, and identify the correct disease.

## Requirements

- Python 3.10 or newer
- An Azure account with access to the configured Azure OpenAI Realtime deployment
- Azure CLI authentication
- A working microphone and audio output device

## Setup

Create and activate a virtual environment, then install the dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Authenticate with Azure:

```bash
az login
```

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

- Move Dr. Ash through the hospital with the arrow keys.
- Enter Pediatrics and the Diagnostics Lab through their corridor doors.
- Approach a patient and press Enter when the patient is highlighted.
- Interview the patient in the diagnosis battle and request configured tests.
- Make the correct diagnosis to mark that patient as complete.
- Help patient 01 to unlock the Patient Ward.
- Help patient 02 to unlock the Pharmacy Lounge.
- Return to the same hospital position and find the next patient.
- Press Escape in the hospital to quit.

During a diagnosis battle:

- Hold either Shift key to speak.
- Dr. Ash switches from idle to talking animation while push-to-talk is active.
- Click the text field to type, then press Enter or click Send.
- Ask the patient to perform one of the configured diagnostic tests.
- Diagnose the disease to win.

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
```
