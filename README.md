# Prismatica V2

Prismatica is a browser-based visualiser for a wall-mounted light sculpture: a video screen sits behind a shaped acrylic lens, bending and refracting the pixels into warped, space-time-like fields of colour.

## Live App

Use the Railway version for the full app with backend STEP export:

https://prismatica-production.up.railway.app/

The older GitHub Pages version is static and does not include the fabricator-ready STEP backend:

https://kitwebster-art.github.io/Prismatica/

## Fabricator Export

The Railway app includes the clean STEP exporter used for fabrication review. It generates a native CAD approximation of the current lens settings and packages it with a short README for the fabricator.

Critical requirement: the visualiser is the artistic source of truth. Kit uses the visualiser to choose the final lens shape, so the exported STEP file must match the selected visualiser lens as closely as technically possible. The STEP is not a generic smoothed version or an interpretation; its purpose is to give an acrylic manufacturer a usable CAD file for machining, mould-making, or whatever fabrication workflow they choose, while preserving the visualiser lens geometry.

When improving the STEP exporter, prioritise shape fidelity to the visualiser first, then manufacturability and surface quality. Any extra smoothing must be intentional and should not noticeably change the chosen optical form.

## Short Demo Copy

Prismatica is a visualiser for a physical wall sculpture where a video panel and shaped acrylic lens collide, turning ordinary pixels into refracted waves, gravitational smears, and space-time-like optical distortion.
