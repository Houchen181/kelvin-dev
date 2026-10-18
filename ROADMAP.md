# Kelvin roadmap: every VISION.md requirement, and where it stands

Updated as work lands. "Done" means built, tested and on `main`; "partial" says what is missing.

| # | VISION requirement | Status | What exists / what is next |
|---|---|---|---|
| 1 | Integrated instruments, faster than a generic harness | partial | Cards, safety gate, plans run in the engine (not one LLM turn per point), virtual lab; VISA/serial adapters (GPIB, USB, LAN, RS-232), read-only discovery (`kelvin-scan`, `scan_instruments`), 18 cards for common source-meters, lock-ins, temperature controllers, magnet supplies, lasers, preamps, meters and generators. **Next: add detected instruments from the portal and open the desktop app on the user's own lab; vendor-SDK adapters (Zurich Instruments, attocube, Quantum Design, Oxford Mercury)** |
| 2 | Keep projects confidential | partial | Local-first app, data in `~/Kelvin`, nothing phones home, local OpenAI-compatible models, incognito chats (never saved, no memory). **Next: per-project choice of allowed model providers** |
| 3 | Caution; simulated experiment for trust | done | Measure/drive gate, limits, interlocks, guard readings, ramps, dry run of every plan on twins, one approval per plan, auto mode for low-risk runs, Stop |
| 4 | Customized instruments | partial | YAML cards, lab config, raw commands checked against cards. **Next: add/edit an instrument from the portal; draft a card from a manual** |
| 5 | Literature research to propose an experiment plan | todo | **Next: literature search tools (arXiv, Crossref/Semantic Scholar) and a plan-proposal flow** |
| 6 | Understand and test each instrument before use | partial | Health checks, describe_instrument, preflight before runs. **Next: per-instrument self-test routines, run after detection** |
| 7 | Track all intermediate data and conditions | done | Event log, run folders (plan, data, snapshots before/after, analysis), live view |
| 8 | Experiment summary/log as PDF or PPT | todo | **Next: report generator (run/day/project → PDF and PPTX)** |
| 9 | Concise, accurate responses | done | Short tool answers, assessments with uncertainties, prompt rules, plots instead of numbers |
| 10 | Mobile access, remote control | todo | **Next: phone-friendly remote access to the desktop app (paired device, token, LAN/tunnel)** |
| 11 | Better literature review | todo | **Next: read and summarize papers with citations; project reading list** |
| 12 | Virtual reviewer | todo | **Next: a second model critiques a plan or a result before it is acted on** |
| 13 | Monitor sensitive instruments, report errors in time | partial | Guard readings during ramps, health checks. **Next: background watches with thresholds, alerts** |
| 14 | Memory: keep important steps, not random questions; global/project/conversation scopes that do not leak | done | Global instructions and learned preferences; project instructions and typed notes; `remember` tool with scope and kind, de-duplication, corrections (`replaces`); notes recalled per message, by relevance in big projects, never into the system prompt; "Recalled N notes" in each reply; learning switch; incognito chats |
| 15 | Notifications | partial | Browser notifications on the approval page. **Next: desktop/portal notifications for approvals, finished runs, monitor alerts; optional webhook (Slack/Teams/email)** |
| 16 | Parallel conversation with several models | partial | Claude, GPT, Gemini and local models selectable per chat. **Next: ask several models at once and compare side by side** |

Also asked for (2026-10-17): native detection and control of the usual instruments once the app is installed (cryostats and temperature controllers, lasers, lock-ins, current/voltage sources, amplifiers, ...). Status: adapters, discovery and cards done; portal onboarding next.
