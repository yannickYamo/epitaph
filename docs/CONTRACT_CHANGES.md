# Contract changes

Agents propose changes to section 6 contracts here during a workflow round (BUILD_PLAN 0.3, 8.3).
The integrator decides between rounds, bumps `PROTOCOL_VERSION` in `types.py` for event changes,
and records the decision in CHANGELOG.md.

| # | Round | Agent | Proposal | Why | Decision |
|---|---|---|---|---|---|
| 1 | 0b-1 | A | Add `ContextFull(RuntimeError)` (tokens, ctx) and `BackendError(RuntimeError)` to `backend/base.py` next to `CreatureDied`; today they live in `backend/errors.py` | `unbounded` dies of a full context (`cause=full`); llama-server answers HTTP 400 `exceed_context_size_error`, and the controller needs one type to catch from either backend. `BackendError` separates "server said no" from "creature died" | |
| 2 | 0b-1 | A | `[backend] mmap = false` must map to `--load-mode none` (llama.cpp b11277 removed `--no-mmap`: "error: invalid argument"). Anyone spawning llama-server by hand (C's S3) must use `-lm none` | Flag rename upstream | |
| 3 | 0b-1 | A | The memory-gap marker must not be inserted in front of kept history: llama-server's cache reuse (`--cache-reuse`) cannot skip new tokens, so the first marker re-reads everything after it (S2f: 81-82% of the prompt, about 1,100 tokens: roughly 2 minutes on the Pi). Put it at the end of the reading that follows the first loss (S2f `--marker reading`: 4%), or leave it out and rely on "forgotten: N" | S2f result; affects B's `mind/memory.py` and 5.4 | |
| 4 | 0b-1 | A | `ModelSpec` could carry `thinking: bool` from models.toml. Not needed for now: the backend sends `chat_template_kwargs = {enable_thinking: false}` on every request (ignored by templates without the switch) | Minimal contract | |
