# Diagnose 'Em All

A voice-driven diagnostic game powered by Azure OpenAI Realtime. Play as the
clinician, interview animated patients, request tests, and identify each disease.

## Setup

- Python 3.11 or newer. Development is tested on Python 3.13/macOS.
- Access to the configured Azure OpenAI Realtime deployment.
- A working audio output device. A microphone is optional; typing is supported.

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

The account needs **Cognitive Services OpenAI User** or equivalent access.
Signing in does not grant resource permissions. Browser sign-in is cancellable
and times out after about three minutes; tokens are held in memory, not saved
in game progress. Production deployments should use their own registered
Microsoft Entra application.

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
- Hold either Shift key to speak. Click the text field and press Enter to type.
- Interview the patient, request diagnostic tests, and state your diagnosis.
- Scroll transcripts with the wheel or Page Up / Page Down.
- Close evidence using Enter, Escape or its close button.
- Escape leaves text focus first, otherwise opening the consultation exit prompt.
- In the hospital, Escape or the pause button opens Resume, New Game and Save & Quit.
- Solving cases unlocks additional rooms; clearing the roster completes the rounds.

The hospital pauses when unfocused; a consultation does not. Losing focus
releases push-to-talk. Returning to the hospital cancels the unfinished case.
Retry starts a new conversation, not recovery of the previous session.

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

Completed cases and hospital position save before consultations, after correct
diagnoses, and on exit. New Game resets only the current roster after confirmation.

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

1. Add a `PatientType` member and matching sprite entry in
   [realtime_conversation.py](src/realtime_conversation.py).
2. Add coordinates in [hospital_game.py](src/hospital_game.py).
3. Add a matching JSON file under [data/prompts/](data/prompts/).
4. Add a transparent 1024x1024 atlas under [patient sprites](data/sprites/patients/):
   a 4x4 grid of 256x256 cells, with idle/talking/worried/relieved rows.
   Legacy 280x660 cards remain supported when no matching atlas exists.

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
