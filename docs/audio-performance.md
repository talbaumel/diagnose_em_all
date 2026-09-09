# NPC audio performance

All eleven personas now have explicit audio profiles. The common-cold kid
retains its original enabled performance. The ten new profiles are packaged
together behind a full-roster listening-review gate. Local game launches enable
their unreviewed audition mode by default, including F5 and the start tasks.
Voice processing and bodily cues remain independently configurable.
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
| `max_spontaneous_cues` | Optional nonnegative per-visit cap; counts audible starts, not reservations; requested actions bypass this cap |
| `event_cues` | Local event names mapped to arrays of catalog choices; never advertised as model tools |
| `event_limits` | Nonnegative per-event visit limits; defaults to one for each configured event |

`cues: []` disables conversational cues and their tools without disabling
configured event cues or the voice filter. `voice.enabled: false` bypasses voice processing without disabling cues.
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

### Full-roster candidate release

The new [source pack](../data/audio/roster_candidates/SOURCES.json) contains
21 hash-pinned FSD50K distributed recordings and 19 prepared candidate cues.
Official metadata identifies clip-level CC0 rights; the dataset is separately
attributed under CC BY 4.0. Three clipped originals were rejected and are not
used by any prepared cue. Some exhale recordings are reused explicitly across
profiles; distinct character IDs do not claim distinct performers.

| Patient | Cues | Initial schedule |
| --- | --- | --- |
| Cold kid | Existing cough, sniffle, sneeze, throat-clear | Unchanged: cycle, 20 seconds |
| Stomachache teen | Stomach gurgle, quiet exhale | 45 seconds, 4 turns, at most 2 |
| Migraine | Quiet exhale | 45 seconds, 4 turns, at most 1 |
| Allergies | Two sniffles, two sneezes | 25 seconds, 2 turns; sniffles can be requested |
| Ankle athlete | Two effort exhales | Confirmed relevant examination, 30 seconds, at most 2 |
| Anxiety | Quiet sigh | 45 seconds, 4 turns, at most 1 |
| Influenza | Two coughs | 25 seconds, 2 turns; cough can be requested |
| Rash | Two fabric rustles | Visible idle fidget, 45 seconds, at most 2 |
| Older back-pain patient | Two effort exhales | Gait assessment/posture reaction, 45 seconds, at most 1 per event |
| Sleep-deprived worker | Two yawns | 35 seconds, 3 turns, at most 3 |
| Neighbor | Throat-clear | 45 seconds, 4 turns, at most 1 |

Turn spacing is measured from the last audible cue; the first response may
already be eligible. A cooldown is a minimum, not a guaranteed interval.
New profiles omit WORLD processing and always specify `cues`, including
an empty list for event-only patients, to avoid the legacy cough-only path.

Local events are `ankle_examination`, `back_examination`, `rash_fidget`, and
`back_posture`. Examinations trigger only after successful confirmed execution,
before opening results, not on cancellation, repeated results, or mere mentions.
The athlete reacts to ankle/joint examination, range of motion, weight-bearing,
or gait assessment; the back-pain patient's examination event is gait assessment,
not a normal neurological examination or an X-ray.
Idle fidget/posture opportunities are checked every 45 seconds. Events never
wait behind speech or queued clinician requests: blocked or stale opportunities
are skipped. Audible event playback starts a small visible lean/fidget and
caption through the same output controller as speech. New events share cue
cooldown and turn budgets; they are not separate audio channels.

Press **F9** or click **EFFECTS** in the consultation header to cycle
100% -> 50% -> 25% -> off. This setting persists separately from campaign
progress in `audio-settings.json` beside the progress save. Invalid settings
are reported and preserved rather than overwritten. Volume is sampled when
an audio segment is prepared for device playback; an already-started segment
finishes at its prepared level. Muting subsequent effects never changes speech
volume. A muted queued inline cue restores the original speech, including its
original pause. Effects are illustrative; authored symptoms remain in dialogue
and test results when effects are muted.

#### Review and activation

The conversation runtime leaves the ten new profiles off unless audition mode
is enabled or the hash-bound
[review form](../data/audio/roster_review.json) contains real listening,
character-fit and clinical-content signoff for every new cue, plus live and
muted playtest signoff for all eleven patients. The catalog's `gameplay_trial`
status is not human approval. The review must not be filled automatically.
The original kid remains enabled regardless of this gate.

The local game entry point (`src.debug_example`) defaults audition mode to on
without changing an explicitly configured value. F5 and the start tasks use
this same entry point. To enforce the review gate during local play:

```bash
DIAGNOSE_AUDITION_ROSTER_CUES=0 uv run python -m src.debug_example
```

Set `DIAGNOSE_AUDITION_ROSTER_CUES=1` to explicitly enable audition mode, or use
the **Audition Full Roster Audio (Unreviewed)** VS Code task. While auditioning,
the consultation displays an explicit unreviewed-candidate notice.
Restart after changing profiles, review records, or launch settings.

Verify the packaged sources and reproduce a cue-only listening playlist:

```bash
uv run python -m tools.prepare_roster_cues verify
uv run python -m tools.prepare_roster_cues rebuild --output .artifacts/roster-audition
```

Both operations show completed/total items and elapsed time. Rebuilding is
offline, refuses an existing output directory, bounds each ffmpeg invocation,
and verifies reproduced WAV hashes against the source manifest. `fetch` restores
missing originals only from pinned URLs with exact hashes; it does not approve
or overwrite recordings. The historical preparation/tool hashes are retained:
the recovered reproduction tool does not pretend to be that original revision.

The generated playlist includes captions and cue offsets in `playlist.json`.
Use real character speech with the existing inline audition tool for listening
review; a tone/fake-sink automated test is not a human voice-fit evaluation.

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

The [runtime catalog](../data/audio/cue_catalog.json) retains four cold-kid
trial assets and registers 19 new default-gated roster candidates.
The catalog records IDs, hashes, kind, caption, permitted contexts,
prepared playback level, provenance and license. Profiles cannot specify
arbitrary cue paths. Modified hashes and redirected paths fail explicitly.

- [COUGHVID sources](../data/audio/coughvid/SOURCES.json) and
  [license](../data/audio/coughvid/LICENSE.txt): CC BY 4.0. The short cough is
  a trimmed/attenuated derivative; source annotations do not verify age or diagnosis.
- [Candidate source manifest](../data/audio/cue_candidates/SOURCES.json),
  [clip terms](../data/audio/cue_candidates/LICENSE-CLIPS.txt) and
  [dataset terms](../data/audio/cue_candidates/LICENSE-DATASET.txt): selected
  recordings are CC0, with separate FSD50K dataset attribution under CC BY 4.0.
  WAVs came from a pinned Hugging Face mirror, with official clip metadata.

Original recordings, prepared clips and license/provenance records are
versioned under `data/audio/`. Rejected sources are retained for provenance
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
The call now reuses one voice worker between completed utterances, avoiding
repeated Python and scientific-library startup without changing the audio
algorithm. Interrupted processing discards its worker; the next reply starts a
fresh one, and leaving the call closes it. On a one-second synthetic voiced
sample, warm processing measured about 82 ms versus 212 ms with a fresh worker.
These are local processing timings, not end-to-end call latency; full-utterance
buffering, model generation, and network delays still apply.
Acoustic quiet gaps are not verified word/phoneme boundaries and may miss
opportunities or misclassify very quiet speech. Clinical realism, speaker
identity and subjective naturalness require listening, not merely passing tests.

For another persona, configure its delivery and select compatible catalog IDs.
Review new recordings before promotion. The current engine supports cough,
sniffle, sneeze, throat-clear and sigh kinds without diagnosis-specific code;
the sigh is not enabled for the kid.
