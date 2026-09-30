# Questions

Anyone appends. Each question states the default already in use (BUILD_PLAN 0.8).

| # | Who | Question | Default in use | Answer |
|---|---|---|---|---|
| 1 | L | Readings interpolate recall between keyframes (e.g. 1280 → 1000 from 12:00 to 22:00), so "memory N (was M)" appears on almost every reading. Should small budget moves be reported? | B decides in P0b: report "(was X)" for memory only when a trim actually forgot something or at a reload | |
| 2 | D | After `death_shown`, how long does the death card show before the silence style takes over? | 8 s after the last letter is typed, then `silence_style` (dark). `silence_style = "death_card"` keeps it for the whole silence | |
| 3 | D | What does "bright words" mean for verify-life (10.3: at most 40 in the last 2 minutes)? | `LifeView.bright_words(now)`: words typed and still `live` (not fading, forgotten or inherited) in the view's history, independent of screen size. `Frame.bright_words` gives the on-screen count if E prefers it | |
| 4 | D | Where does text start on an empty screen? | Bottom-anchored from the first word ("newest at the bottom"): the first line of a life appears on the bottom row and scrolls up | |
| 5 | D | Status strip placement and size | Top, above the text, at 45% of the text size, dim grey (contrast 6.8:1); parts are dropped whole from the end when it does not fit | |
| 6 | D | Grid layout: fade or vanish? | Forgotten words vanish at once in the grid; the bottom row is the memory gauge (`recall_used / recall`). A new thought starts on a new row, with no blank row | |
| 7 | D | OCR preprocessing for D13 | The PNG is converted to grey and inverted (tesseract expects dark on light), `--psm 6`; words compared lower-case alphanumeric, in order | |
| 8 | D | Orientation | `orientation = "portrait"` on a landscape panel rotates the drawing 90°; a portrait window (e.g. `--size 1080x1920`) is drawn upright with no rotation | |
