# Prismatica V2

Prismatica is a browser-based visualiser for a wall-mounted light sculpture: a video screen sits behind a shaped acrylic lens, bending and refracting the pixels into warped, space-time-like fields of colour.

## Live App

Use the Railway version for the full app with backend STEP export:

https://prismatica-production.up.railway.app/

The older GitHub Pages version is static and does not include the fabricator-ready STEP backend:

https://kitwebster-art.github.io/Prismatica/

## Scene direction

The active visualizer presents the sculpture without a human scale figure.
Legacy visitor code and its licensed source asset remain archived in the
repository, but the model is not added to the scene, loaded by the browser or
exposed as a visualizer control.

## Fabricator Export

The Railway app includes two related fabrication exports:

- **Verified lens STEP** generates the equation-derived acrylic lens with topology and re-import QA.
- **Fabricator 3D model** lets the user select the lens, polished reflective edge, outer casing, LED panel space claim, and wall mount. It exports each selected part separately, a combined multi-solid STEP, and a machine-readable assembly manifest.

The lens is verified manufacturing geometry. The reflective edge, casing, LED panel, and wall mount are intentionally labelled as concept CAD until the panel supplier, material gauges, fasteners, electronics, ventilation, cable exits, tolerances, wall type, and load engineering are confirmed.

Critical requirement: the visualiser is the artistic source of truth. Kit uses the visualiser to choose the final lens shape, so the exported STEP file must match the selected visualiser lens as closely as technically possible. The STEP is not a generic smoothed version or an interpretation; its purpose is to give an acrylic manufacturer a usable CAD file for machining, mould-making, or whatever fabrication workflow they choose, while preserving the visualiser lens geometry.

When improving the STEP exporter, prioritise shape fidelity to the visualiser first, then manufacturability and surface quality. Any extra smoothing must be intentional and should not noticeably change the chosen optical form.

## Short Demo Copy

Prismatica is a visualiser for a physical wall sculpture where a video panel and shaped acrylic lens collide, turning ordinary pixels into refracted waves, gravitational smears, and space-time-like optical distortion.
