---
title: PatchPilot
emoji: 🩹
colorFrom: blue
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: An autonomous open-source contribution agent
---

# PatchPilot

An autonomous open-source contribution agent. It ranks a repository's open
issues, retrieves the relevant code, diagnoses a root cause, writes a minimal
patch, runs the project's own tests against it in a sandbox, repairs it when
they fail — and stops for a human before anything is pushed.

**Everything on the first four tabs runs without a language model.** Repository
analysis, issue ranking, code search and the entire guardrail layer are
deterministic, and the embedding model runs locally on CPU. That is a design
decision rather than a limitation: the model is used for judgement, and code is
used for facts that are already written down.

Patch *validation* needs a Docker sandbox with no network, which free hosting
cannot provide — so it runs in GitHub Actions instead. Control plane and
execution plane, separated.

[Source, 26 concept explainers, 20 decision records and 9 documented failures →](https://github.com/raunakpandey1/PatchPilot)
