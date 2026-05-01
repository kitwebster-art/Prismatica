# Prismatica Ripple Lens — Fabrication Brief

## Part summary
- **Material**: Cast PMMA (acrylic), clear, optical grade preferred
- **Footprint**: 600 × 600 mm square
- **Overall thickness**: 38 mm (30 mm base + 8 mm shaped top)
- **Top face**: Concentric ripple pattern, peaked at centre, fades to flat at the perimeter
- **Bottom face**: Machined flat
- **Surface finish (top)**: Diamond or vapour polished for optical clarity
- **Surface finish (bottom + edges)**: Standard machined finish acceptable

## Geometry
- Wave: cosine ripple, 70 mm wavelength, 8 mm peak amplitude
- Radial envelope: smooth cosine taper from full amplitude at centre to zero at panel edge
- Centre of the panel = peak of innermost ripple (outward bulge)
- Surface terminates flush with the flat 30 mm base at the perimeter (no sharp lop-off)

## Files supplied
- `prismatica_ripple_field_600x600_30mm_HIGHRES.stl` — 69 MB, 1.44 M triangles, 1.0 mm grid pitch
- `prismatica_ripple_field_600x600_30mm_MEDRES.stl` — 31 MB, 0.64 M triangles, 1.5 mm grid pitch (for previewing)
- Source script: `/exports/build-stl.mjs` — re-runs the STL build if specs change

Sealed solid (top + flat bottom + perimeter walls). Manifold mesh, ready for CAM.

## Use case
Sculptural light work. Lens sits over an LED panel and refracts the panel's image. Optical clarity matters for the look, but tolerances aren't telescope grade. Visual quality comes from the macro shape and the polish on the curved face.

## Production
- One prototype piece first
- Likely 5–20 piece run after prototype validation
- Open to casting from a master if that's more economical at low volume

## Contact
Kit Webster
kit.webster@gmail.com
+61 400 000 000

---

# Manufacturers contacted

## Plasticut Melbourne (primary, local)
- Email: ask@plasticut.com.au
- Phone: (03) 9357 6688
- Address: 15 Adrian Road, Campbellfield VIC 3061
- Hours: 8:30am–5pm Mon–Thu, 8:30am–2:30pm Fri
- Status: Gmail draft created (sender: kit.webster@gmail.com)

## Allplastics Engineering (Sydney, backup)
- Web form: https://www.allplastics.com.au/contacts/
- Phone: (02) 8038 2000
- Address: Unit 20/380 Eastern Valley Way, Chatswood NSW 2067
- Note: They want PDF drawings for quoting (mentioned in their materials)
- Status: Gmail draft created with body to paste into their contact form

## Xometry / Hubs (instant online quote, sanity check)
- Upload STL at https://www.xometry.com or https://www.hubs.com
- Choose: PMMA cast acrylic, 5-axis CNC, polished finish
- No email needed — quote returns within minutes
- Use this to benchmark Plasticut/Allplastics pricing

## If you want a higher-end optical finish (overkill for art use)
- Optimax Systems, Rochester NY (custom optical shop)
- Plastic Optics Corp / Lenz Inc (diamond-turning specialists)
- Edmund Optics custom shop

# Glass alternative
Not recommended at this scale. Acrylic is lighter, cheaper, and the slightly higher dispersion (~30 vs glass ~58 Abbe) actually helps the refraction effect. Glass at 600 mm is heavy, fragile, and would need a kiln-slumped art-glass approach via somewhere like JamFactory (Adelaide) or Canberra Glassworks rather than an optical shop.
