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

Start with the default common-cold patient:

```bash
python -m src.debug_example
```

Pass a prompt file to select another patient:

```bash
python -m src.debug_example data/prompts/11_eccentric_neighbor.json
```

During the conversation:

- Hold either Shift key to speak.
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
3. Place the `280x660` PNG sprite sheet in `data/sprites/patients`.
4. Add a matching JSON configuration in `data/prompts`.

Patient sheets contain four animation frames for each state used by the game: idle, talking, worried, and relieved.

## Project Layout

```text
data/
  prompts/           Patient conversation configurations
  sprites/
    patients/        One animation sheet per patient
    tests/           Images displayed as diagnostic results
src/
  debug_example.py           Prompt loader and executable example
  realtime_conversation.py   Realtime client, tools, and animation UI
```
