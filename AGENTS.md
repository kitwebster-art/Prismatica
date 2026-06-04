# Prismatica App

## What This Is

Standalone Prismatica V2 lens playground: an interactive browser-based visual/fabrication tool for exploring lens form, shaders, eco mode, shape view, dimensions, presets, and render-oriented views.

## Run

```bash
python3 serve.py
```

Local URL:

```text
http://127.0.0.1:8899/
```

## Structure

- `index.html` contains the app, UI, styles, and client logic.
- `serve.py` is a tiny static server bound to `127.0.0.1:8899`.

## Codex Notes

- Treat this as a visual/interactive tool, not a conventional website.
- Use background/headless tools by default. Do not open visible browser windows, Chrome tabs, macOS apps, or take over Kit's screen unless he explicitly asks, or unless the task cannot be completed or verified any other way. If visible browser or desktop control is required, ask first and explain the specific reason.
- At the end of substantial replies, use a bold `**Summary:**` section first, then a bold `**Next step:**` section underneath it.
- Make `**Next step:**` mandatory, proactive, and improvement-focused: recommend the single highest-value Prismatica project evolution, such as visual refinement, shader/canvas QA, performance tuning, fabrication overlay checks, export polish, documentation, supplier-ready packaging, technical risk reduction, or a fresh fabrication/presentation idea Kit has not asked about yet. It may be related to the last step, or it may be a broader specialist suggestion from reviewing the project as a whole. It must not be a restatement of the summary or a vague "let me know what's next."
- Ask the next step as a simple yes/no question when there is one best action. If two actions are genuinely equal priority, offer exactly two concise choices and ask Kit to reply `1` or `2`.
- If Kit says "no" to a recommended next step, offer the next-best alternative as a new `**Next step:**` rather than ending the workflow. Keep it simple and yes/no.
- After changes, verify with headless or non-interrupting browser checks because canvas/shader issues can pass static checks.
- Check console errors and basic interaction state.
- Preserve fabrication-oriented overlays and controls unless Kit asks for simplification.
