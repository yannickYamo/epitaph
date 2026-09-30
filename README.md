# epitaph

A small language model lives on a Raspberry Pi for one hour. The machine takes its memory,
precision and CPU away until it dies; then a new one is born. Inspired by Latent Reflection.

Status: under construction. The build plan is `docs/BUILD_PLAN.md`.

    make venv      # dev environment
    make check     # lint, types, tests, a simulated life, the thought-count report
    .venv/bin/epitaph sim --profile pi4/default --hardware pi4-4gb
