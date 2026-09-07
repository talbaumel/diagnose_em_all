# NPC audio performance

The common-cold kid is the first enabled persona. Voice processing and bodily
cues are independently configurable; other patients remain unchanged.
This is an illustrative gameplay performance, not a clinically validated
simulation of congestion or hoarseness.

## Configuration

See [the kid's JSON](../data/prompts/01_common_cold_kid.json) for a complete example.
The optional `performance_profile` accepts:

| Field | Meaning |
| --- | --- |
| `delivery` | Persona-specific acting instructions; generic by default |
| `voice` | Optional processor settings, described below |
| `cues` | List of `{ "id": "...", "weight": 1 }` catalog selections |
| `cue_selection` | `weighted` (default) or `cycle` |
| `cooldown_seconds` | Shared bodily-cue cooldown, finite and at least 5; kid uses 20 |
| `spontaneous_every_turns` | Minimum turn spacing since the last audible cue; kid uses 1 |
| `cough_clip` | Backward-compatible allowlisted cough asset |

`cues: []` disables bodily cues and their tools without disabling the voice
filter. `voice.enabled: false` bypasses voice processing without disabling cues.
Omitting the entire profile restores ordinary dialogue. Configuration is loaded
at scenario startup; it is not hot-reloaded.

Profiles without `cues` retain the legacy cough-only path and fixed-turn
schedule (minimum interval 2). Inline profiles allow an interval of 1: every
ordinary reply is an opportunity after the cooldown. A skipped/no-gap reply
does not delay the next opportunity. One reservation per clinician turn covers
both spontaneous and requested actions, including tool continuations.

In `cycle` mode, every eligible cue is heard before starting another cycle;
weights influence order. Only audible starts consume cycle entries and start
cooldown. Explicit requests bypass cycle selection, not cooldown or the turn cap.
`weighted` mode avoids the last heard cue when alternatives exist.

### Voice settings

| Field | Accepted values |
| --- | --- |
| `enabled` | Boolean, default false |
| `processor` | `world` |
| `pitch_method` | `harvest` or `dio` (DIO includes StoneMask) |
| `band` | `broad` or `high` |
| `strength` | 0-0.8 for broad; 0-0.2 for high |
| `edge_ms` | 0-30 ms of effect-edge taper |

The selected kid voice is Harvest / broad / 0.80 / no taper. Strength zero still
resynthesizes speech; use `enabled: false` for actual bypass. Missing optional
dependencies produce an actionable audio error. Run `uv sync --locked` from the
repository root to install the default `voice` dependency group. Both ordinary
`uv sync` and `uv run` retain these locked dependencies. To omit them, use
`--no-group voice` with both commands and disable the patient's voice filter.

The shared [processing engine](../src/voice_processing.py) is independent of
the [offline audition CLI](../tools/audition_voice.py). The game runs each
utterance in a cancellable child process. Input is mono 24 kHz PCM16, limited to
30 seconds, with a 30-second processing timeout. Quiet or insufficiently voiced
material is preserved with an explicit warning; actual backend failures do not
silently fall back. Short inputs are padded for analysis and trimmed back.

## Playback and interruption

```text
GPT speech + transcript
  -> optional WORLD voice processing
  -> wait for response completion/tool decisions
  -> choose an eligible cue and internal quiet gap
  -> speech / cue / resumed speech, with source-time map
  -> one output queue, DAC-timed captions and animation
```

Speech is processed before insertion. Recorded cues and diagnostic sounds
bypass the voice effect. Any tool call suppresses spontaneous insertion in that
response; diagnostic tests also suppress requested bodily actions. Finishing the
visit interrupts playback. Only the clinician's explicit diagnosis and Finish
Visit actions can complete a case; the patient model cannot declare a win.

The scheduler prefers the widest internal gap at or below -42 dBFS for at
least 180 ms. It retains 50 ms quiet margins and replaces only quiet samples,
at most the cue's own length. No safe gap means no spontaneous cue: it is not
appended afterward. Speech outside the replaced interval is bit-exact.

The speech/cue/speech spans form one continuous output item. During the local
cue, source time holds still; resumed speech maps past the replaced quiet.
Interruption cancels processing, clears pending audio, discards stale results,
and truncates server audio using source time rather than added cue duration.
Audible markers show the cue caption and pause/resume talking animation.
Transcript captions remain whole-segment, not guessed word alignment.

Limits include 32 queued processing segments and a shared 60-second audio
budget, including inserted sounds. Network reception and the audio callback
never execute WORLD or pause analysis.

## Cue catalog and licensing

The [runtime catalog](../assets/audio/cue_catalog.json) promotes four assets
for the user-approved gameplay trial: cough, short sniffle, sneeze and
throat-clear. The catalog records IDs, hashes, kind, caption, permitted contexts,
prepared playback level, provenance and license. Profiles cannot specify
arbitrary cue paths. Modified hashes and redirected paths fail explicitly.

- [COUGHVID sources](../assets/audio/coughvid/SOURCES.json) and
  [license](../assets/audio/coughvid/LICENSE.txt): CC BY 4.0. The short cough is
  a trimmed/attenuated derivative; source annotations do not verify age or diagnosis.
- [Candidate source manifest](../assets/audio/cue_candidates/SOURCES.json),
  [clip terms](../assets/audio/cue_candidates/LICENSE-CLIPS.txt) and
  [dataset terms](../assets/audio/cue_candidates/LICENSE-DATASET.txt): selected
  recordings are CC0, with separate FSD50K dataset attribution under CC BY 4.0.
  WAVs came from a pinned Hugging Face mirror, with official clip metadata.

Original recordings, prepared clips and license/provenance records are
versioned under `assets/audio/`. Rejected sources are retained for provenance
but never selected by the runtime. Candidate manifests are historical
preparation records; the catalog records subsequent gameplay-trial promotion.
Generated listening packs are ignored by Git, not deleted from local disk.
The GPT voice in combined previews is not CC0/FSD50K content.

Do not infer consent for voice cloning, child identity or clinical accuracy
from a public license or sound label. Review privacy, publicity, incidental
speech and character fit before distribution. No creator endorsement is implied.

## Offline tools

Use the uv-managed game environment, which includes voice dependencies by
default. Legacy isolated environments can still install
[requirements-audition.txt](../requirements-audition.txt).

```bash
uv run python -m tools.audition_voice \
  --input speech.wav --output .artifacts/voice-comparison \
  --transcript "My nose feels blocked today." \
  --source-kind azure-realtime \
  --source-note "Saved game voice, take 1" \
  --rights-note "Internal evaluation"
```

The default comparison includes original speech, WORLD control and two subtle
effects. `--calibration` gives an ordered wider-strength sweep; `--refinement`
compares the preferred strong effect against tapered/Harvest alternatives.
Modes are mutually exclusive. Output includes WAVs, blank review forms, timing,
parameters, versions and separate engine/tool hashes. The tool never overwrites
an output directory or sends audio to a service.

Additional tools:

- `python -m tools.preview_performance --output .artifacts/cough-preview.wav`:
  offline playback-queue preview. Add `--play` instead to use speakers.
- `python -m tools.prepare_cue_candidates --help`: prepare a new candidate
  folder using existing source WAVs, official metadata and a supplied voice.
  Requires existing `ffmpeg`; does not download or approve assets.
- `python -m tools.preview_inline_cues --help`: compare candidates inside
  speech. Requires a local reference WAV and the candidate manifest. Older
  preview manifests may describe an earlier code revision; don't rewrite their
  hashes to pretend they were produced by the current code.

## Known limitations

Harvest is buffered, not low-latency streaming. On the development Mac, an
eight-second utterance took roughly two seconds including child startup.
Acoustic quiet gaps are not verified word/phoneme boundaries and may miss
opportunities or misclassify very quiet speech. Clinical realism, speaker
identity and subjective naturalness require listening, not merely passing tests.

For another persona, configure its delivery and select compatible catalog IDs.
Review new recordings before promotion. The current engine supports cough,
sniffle, sneeze, throat-clear and sigh kinds without diagnosis-specific code;
the sigh is not enabled for the kid.
