# Drawer Instruments

The four native-size transparent PNGs replace the geometric equipment drawings
in the drawer, skill details, and instrument activities. They use nearest-neighbor
scaling, blank displays, and the consultation room's shaded pixel-art style.

`drawer_tools_source.png` is the unmodified full-resolution Azure gpt-image-2
output. `drawer_tools_prompt.txt` records the prompt; the style reference was
`data/sprites/world/consultation_room.png`. Keep the source when revising assets.

Rebuild the runtime PNGs and their contact-point manifest from the project root:

```sh
.venv/bin/python -m tools.prepare_instruments
```

The crop regions account for the generated objects extending beyond the requested
grid. The manifest's contact coordinates refer to the prepared PNGs, not the
source sheet. Drawer thumbnails are centered; held tools align their contact
pixel with the cursor. No patient values are baked into these images.

Export drawer previews at 480, 720, and 960 pixels, plus resting and held tools:

```sh
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy .venv/bin/python -m tools.preview_instruments
```