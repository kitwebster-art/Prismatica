#!/usr/bin/env python3
"""Build a clean, machinable Prismatica V2 acrylic lens STEP file.

This is intentionally a first-principles CAD rebuild, not a mesh conversion.
For square lenses it creates a closed OpenCascade solid from six bounded CAD surfaces:

- one smooth front B-spline lens surface
- one flat rear face
- four ruled side faces

The dedicated Round Sine Lens is generated as a true revolved CAD solid from
its centre cross-section. Other circular footprints retain their full 2D lens
equation on a trimmed B-spline face with a flat rear face and cylindrical wall.

The surface is generated from a small analytic control grid, so the STEP is a
native CAD solid instead of thousands of STL triangles or loose untrimmed faces.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.pdfgen import canvas
except ModuleNotFoundError as exc:
    if exc.name != "reportlab":
        raise
    raise ModuleNotFoundError(
        "reportlab is missing. Install it with: "
        f"{sys.executable} -m pip install reportlab"
    ) from exc

try:
    from OCP.BRepBuilderAPI import (
        BRepBuilderAPI_MakeEdge,
        BRepBuilderAPI_MakeFace,
        BRepBuilderAPI_MakeSolid,
        BRepBuilderAPI_Sewing,
        BRepBuilderAPI_MakeWire,
    )
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakeRevol
    from OCP.Geom import Geom_BSplineCurve, Geom_BSplineSurface
    from OCP.GeomAPI import GeomAPI_PointsToBSpline, GeomAPI_PointsToBSplineSurface
    from OCP.GProp import GProp_GProps
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Reader, STEPControl_Writer
    from OCP.TColStd import TColStd_Array1OfInteger, TColStd_Array1OfReal
    from OCP.TColgp import TColgp_Array1OfPnt, TColgp_Array2OfPnt
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS
    from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt
except ModuleNotFoundError as exc:
    if exc.name != "OCP":
        raise
    bundled_python = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
    if bundled_python.exists() and Path(sys.executable).resolve() != bundled_python.resolve():
        os.execv(str(bundled_python), [str(bundled_python), __file__, *sys.argv[1:]])
    raise ModuleNotFoundError(
        "OCP is missing. Install it with: "
        f"{sys.executable} -m pip install cadquery-ocp==7.7.2"
    ) from exc


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "exports" / "saved" / "fabricator_clean_step"
VERSION_FILE = OUT_DIR / ".next_step_version"

DEFAULT_STATE = {
    "lensType": "morph_rings",
    "lensShape": "standard",
    "panelWidth": 500.0,
    "panelHeight": 500.0,
    "resolution": 0.5,
    "smoothing": 16,
    "shapeScale": 0.86,
    "mirrorLens": False,
    "thickness": 20.0,
    "params": {
        "wavelength": 35.0,
        "amplitude": 35.0,
        "morph_period": 5.0,
        "bias": 0.38,
        "phase_shift": 0.19,
    },
}

SIZE_MM = 500.0
WIDTH_MM = 500.0
HEIGHT_MM = 500.0
HALF_W = WIDTH_MM / 2.0
HALF_H = HEIGHT_MM / 2.0
BASE_THICKNESS_MM = 20.0
DEFAULT_SAMPLES = 61
MAX_CAD_SAMPLES = 81
MIN_CAD_SPACING_MM = 2.0
STEP_QUALITY_PROFILES = {
    # Smaller quote/review file. Keeps the same analytic visualizer shape but
    # uses a lighter B-spline control grid so suppliers can open it quickly.
    "compact": {"min_samples": 51, "max_samples": 61, "resolution_multiplier": 6.0},
    # Everyday fabrication handoff. This is the optimized default: far smaller
    # than the old 121 x 121 export while still close to the visualizer field.
    "standard": {"min_samples": 61, "max_samples": 81, "resolution_multiplier": 4.0},
    # Dense fallback if a fabricator explicitly asks for a finer surface.
    "high": {"min_samples": 91, "max_samples": 121, "resolution_multiplier": 3.0},
}
DEGREE = 3
TOLERANCE = 1e-5
LAST_REFERENCE_GRID: list[list[float]] | None = None
LAST_REFERENCE_SPACING_MM = 0.0
LAST_CORNER_CLEANUP_REPORT: dict[str, float] | None = None
LAST_OPTICAL_CLEANUP_REPORT: dict[str, float | bool | str] | None = None
FABRICATOR_CORNER_FLAT_MM = 42.0
FABRICATOR_CORNER_BLEND_MM = 86.0
VERIFIED_EQUATION_LENSES = {
    "round_sine_lens",
    "sine_wave_lens",
    "ripple_field",
    "gravitational_wave",
    "stepped_ripple",
    "torus_field",
    "hex_cells",
    "organic_bulbs",
    "compound_bulbs",
    "pyramid_grid",
    "dome",
    "morph_rings",
    "interlocking_teardrops",
    "wave_lattice",
    "voronoi_bulbs",
}
QA_TOLERANCES = {
    "top_surface_rms_mm": 1.5,
    "top_surface_max_mm": 8.0,
    "flat_border_max_abs_mm": 0.25,
}


def slugify(value: object) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "_", str(value or "lens")).strip("_").lower()
    return text or "lens"


def next_export_version() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        version = int(VERSION_FILE.read_text(encoding="utf-8").strip() or "1")
    except (OSError, ValueError):
        version = 1
    VERSION_FILE.write_text(str(version + 1), encoding="utf-8")
    return version


def versioned_export_names(version: int) -> dict[str, str]:
    base = f"PV2-step-v{version:03d}"
    return export_names(base)


def export_names(base: str) -> dict[str, str]:
    return {
        "base": base,
        "step": f"{base}.step",
        "zip": f"{base}.zip",
        "readme": f"{base}-readme.txt",
        "qa": f"{base}-qa.json",
        "pdf": f"{base}-validation.pdf",
        "preview": f"{base}-preview.obj",
    }


def is_round_footprint(state: dict) -> bool:
    return state.get("lensType") == "round_sine_lens" or state.get("lensShape") == "round"


def round_footprint_diameter(state: dict) -> float:
    if state.get("lensType") == "round_sine_lens":
        params = params_for(state)
        return max(
            1.0,
            min(
                float(params.get("diameter", min(WIDTH_MM, HEIGHT_MM)) or min(WIDTH_MM, HEIGHT_MM)),
                min(WIDTH_MM, HEIGHT_MM),
            ),
        )
    return max(1.0, min(WIDTH_MM, HEIGHT_MM))


def round_footprint_radius(state: dict) -> float:
    return round_footprint_diameter(state) * 0.5


def footprint_description(state: dict) -> str:
    if is_round_footprint(state):
        return f"{round_footprint_diameter(state):.0f} mm diameter circle"
    return f"{WIDTH_MM:.0f} x {HEIGHT_MM:.0f} mm"


def cad_construction_description(state: dict) -> str:
    if state.get("lensType") == "round_sine_lens":
        return (
            "The STEP file contains one closed circular solid generated by revolving "
            "the centre lens profile 360 degrees. This creates a true round acrylic disc "
            "with a flat rear face and a cylindrical outer wall."
        )
    if is_round_footprint(state):
        return (
            "The STEP file contains one closed circular solid with the selected lens equation "
            "preserved across a trimmed B-spline front surface, plus a flat rear face and a "
            "true cylindrical outer wall."
        )
    return (
        "The STEP file contains one closed solid with a smooth controlled B-spline "
        "front lens surface, flat rear face, and four bounded side faces. The square perimeter "
        "is explicitly locked to the requested footprint so the corners remain flat "
        "and do not grow small spline artifacts."
    )


def curve_profile01(value: float, curvature: float = 1.0) -> float:
    c = max(0.25, float(curvature if curvature is not None else 1.0))
    v = max(0.0, min(1.0, float(value)))
    return math.pow(v, 1.0 / c)


def smoothstep01(value: float) -> float:
    v = max(0.0, min(1.0, float(value)))
    return v * v * (3.0 - 2.0 * v)


def smootherstep01(value: float) -> float:
    v = max(0.0, min(1.0, float(value)))
    return v * v * v * (v * (v * 6.0 - 15.0) + 10.0)


def ripple_profile01(value: float, curvature: float = 1.0, profile: str = "current") -> float:
    v = max(0.0, min(1.0, float(value)))
    if profile == "perfect_s_curve":
        return curve_profile01(smootherstep01(v), curvature)
    return curve_profile01(v, curvature)


def continuous_s_ripple01(phase: float, curvature: float = 1.0) -> float:
    p = float(phase) % 1.0
    half = p * 2.0 if p < 0.5 else (1.0 - p) * 2.0
    return curve_profile01(smootherstep01(half), curvature)


def center_bulge_height(radius_from_center: float, params: dict) -> float:
    height = max(0.0, float(params.get("center_bulge_height", 0.0) or 0.0))
    radius = max(0.0, float(params.get("center_bulge_radius", 0.0) or 0.0))
    if height <= 0.0 or radius <= 0.0:
        return 0.0
    curve = max(0.25, float(params.get("center_bulge_curve", 1.0) or 1.0))
    shoulder = max(0.0, min(1.0, float(params.get("center_bulge_shoulder", 0.0) or 0.0)))
    t = max(0.0, min(1.0, float(radius_from_center) / radius))
    shoulder_t = math.pow(t, 1.0 + shoulder * 4.0)
    dome = 0.5 * (1.0 + math.cos(math.pi * shoulder_t))
    return height * curve_profile01(dome, curve)


def shape_ripple_value01(value: float, ridge_width: float = 0.5, contrast: float = 1.0, valley_lift: float = 0.0) -> float:
    x = max(0.0, min(1.0, float(value)))
    width = max(0.0, min(1.0, float(ridge_width if ridge_width is not None else 0.5)))
    if width > 0.5:
        x = math.pow(x, 1.0 + (width - 0.5) * 8.0)
    elif width < 0.5:
        x = 1.0 - math.pow(1.0 - x, 1.0 + (0.5 - width) * 8.0)
    c = max(0.1, float(contrast if contrast is not None else 1.0))
    x = max(0.0, min(1.0, 0.5 + (x - 0.5) * c))
    lift = max(0.0, min(0.95, float(valley_lift if valley_lift is not None else 0.0)))
    return lift + (1.0 - lift) * x


LENS_DEFAULT_PARAMS = {
    "round_sine_lens": {
        "diameter": 500.0,
        "wavelength": 70.0,
        "amplitude": 14.0,
        "visible_rings": 4,
        "outer_fade_width": 18.0,
        "edge_taper_width": 50.0,
        "ripple_phase": 0.0,
    },
    "sine_wave_lens": {
        "wavelength": 70.0,
        "amplitude": 14.0,
        "visible_rings": 4,
        "outer_fade_width": 18.0,
        "edge_taper_width": 50.0,
        "ripple_phase": 0.0,
    },
    "ripple_field": {
        "wavelength": 70.0,
        "amplitude": 8.0,
        "ripple_profile": "current",
        "ripple_curvature": 1.0,
        "ripple_phase": 0.0,
        "ridge_width": 0.5,
        "ripple_contrast": 1.0,
        "valley_lift": 0.0,
        "ripple_stretch": 1.0,
        "ripple_angle": 0.0,
        "radial_falloff": 0.0,
        "center_bulge_height": 0.0,
        "center_bulge_radius": 90.0,
        "center_bulge_curve": 1.0,
        "center_bulge_shoulder": 0.0,
        "edge_taper_width": 70.0,
        "bulb_roundness": 1.0,
        "curve_strength": 1.0,
    },
    "gravitational_wave": {
        "wavelength": 58.0,
        "amplitude": 28.0,
        "source_separation": 120.0,
        "source_balance": 0.5,
        "interference_strength": 0.65,
        "spiral_twist": 2.2,
        "chirp": 0.38,
        "ellipticity": 1.0,
        "falloff": 0.25,
        "ripple_profile": "continuous_s_ripple",
        "ripple_curvature": 1.25,
        "ripple_phase": 0.0,
        "source_phase_offset": 0.41,
        "ridge_width": 0.5,
        "ripple_contrast": 1.0,
        "valley_lift": 0.0,
        "edge_taper_width": 70.0,
        "center_bulge_height": 12.0,
        "center_bulge_radius": 130.0,
        "center_bulge_curve": 1.2,
        "center_bulge_shoulder": 0.35,
        "orbit_angle": 0.0,
    },
    "stepped_ripple": {
        "wavelength": 42.0,
        "amplitude": 100.0,
        "step": 10.0,
        "ripple_profile": "continuous_s_ripple",
        "ripple_curvature": 1.0,
        "ripple_phase": 0.0,
        "ridge_width": 0.5,
        "ripple_contrast": 1.0,
        "valley_lift": 0.0,
        "ripple_stretch": 1.0,
        "ripple_angle": 0.0,
        "radial_falloff": 0.0,
        "center_bulge_height": 0.0,
        "center_bulge_radius": 90.0,
        "center_bulge_curve": 1.0,
        "center_bulge_shoulder": 0.0,
        "edge_taper_width": 50.0,
        "edgeFade": 0.18,
        "bumpSharp": 0.5,
    },
    "torus_field": {
        "cols": 3,
        "rows": 3,
        "torus_outer_r": 50.0,
        "torus_inner_r": 18.0,
        "torus_height": 14.0,
        "ripple_curvature": 1.0,
    },
    "hex_cells": {
        "cell_size": 40.0,
        "cell_depth": 6.0,
        "ripple_curvature": 1.0,
        "invert": False,
    },
    "organic_bulbs": {
        "n_bulbs": 24,
        "seed": 7,
        "min_r": 30.0,
        "max_r": 75.0,
        "min_h": 8.0,
        "max_h": 22.0,
        "ripple_curvature": 1.0,
    },
    "compound_bulbs": {
        "feature_count": 13,
        "seed": 41,
        "min_radius": 55.0,
        "max_radius": 125.0,
        "relief": 32.0,
        "anisotropy": 0.55,
        "well_mix": 0.23,
        "saddle_strength": 0.65,
        "profile_power": 1.35,
        "edge_taper_width": 55.0,
    },
    "pyramid_grid": {
        "cols": 8,
        "rows": 8,
        "pyramid_h": 20.0,
        "ripple_curvature": 1.0,
    },
    "dome": {
        "radius": 280.0,
        "height": 40.0,
        "ripple_curvature": 1.0,
    },
    "morph_rings": {
        "wavelength": 40.0,
        "amplitude": 12.0,
        "ripple_profile": "current",
        "ripple_curvature": 1.0,
        "morph_period": 2.0,
        "bias": 0.5,
        "phase_shift": 0.0,
    },
    "interlocking_teardrops": {
        "cols": 4,
        "rows": 4,
        "size": 70.0,
        "height": 16.0,
        "ripple_curvature": 1.0,
        "asymmetry": 0.5,
        "rot_variance": 0.8,
        "jitter": 0.3,
        "seed": 3,
    },
    "wave_lattice": {
        "wavelength_x": 80.0,
        "wavelength_y": 80.0,
        "amplitude": 10.0,
        "ripple_profile": "current",
        "ripple_curvature": 1.0,
        "diagonal_mix": 0.0,
        "rotation": 0.0,
    },
    "logo": {
        "height": 30.0,
        "ripple_curvature": 1.0,
        "falloff": 0.15,
        "invert": False,
        "threshold": 0.5,
    },
    "voronoi_bulbs": {
        "n_seeds": 20,
        "seed": 5,
        "height": 18.0,
        "ripple_curvature": 1.0,
        "fill": 0.85,
    },
}


def params_for(state: dict) -> dict:
    params = dict(LENS_DEFAULT_PARAMS.get(state.get("lensType"), {}))
    params.update(state.get("params") or {})
    return params


def mulberry32(seed: float):
    """Deterministic PRNG matching the visualizer's mulberry32 helper."""

    a = int(seed) & 0xFFFFFFFF

    def rng() -> float:
        nonlocal a
        a = (a + 0x6D2B79F5) & 0xFFFFFFFF
        t = a
        t = ((t ^ (t >> 15)) * (t | 1)) & 0xFFFFFFFF
        t ^= (t + (((t ^ (t >> 7)) * (t | 61)) & 0xFFFFFFFF)) & 0xFFFFFFFF
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 4294967296.0

    return rng


def knot_arrays(n_ctrl: int, degree: int):
    interior_count = n_ctrl - degree - 1
    unique = [0.0]
    if interior_count > 0:
        unique.extend(i / (interior_count + 1) for i in range(1, interior_count + 1))
    unique.append(1.0)
    mults = [degree + 1] + [1] * max(0, len(unique) - 2) + [degree + 1]

    knots = TColStd_Array1OfReal(1, len(unique))
    multiplicities = TColStd_Array1OfInteger(1, len(unique))
    for i, (k, m) in enumerate(zip(unique, mults), start=1):
        knots.SetValue(i, float(k))
        multiplicities.SetValue(i, int(m))
    return knots, multiplicities


def load_state(path: str | None) -> dict:
    state = json.loads(json.dumps(DEFAULT_STATE))
    incoming = {}
    if path:
        incoming = json.loads(Path(path).read_text(encoding="utf-8"))
        for key, value in incoming.items():
            if key != "params":
                state[key] = value
    lens_type = state.get("lensType")
    state["params"] = dict(LENS_DEFAULT_PARAMS.get(lens_type, DEFAULT_STATE.get("params", {})))
    state["params"].update((incoming.get("params") if isinstance(incoming, dict) else None) or {})
    return state


def require_verified_equation_lens(state: dict) -> None:
    lens_type = state.get("lensType")
    if lens_type == "logo":
        raise RuntimeError(
            "Fabricator verified STEP export cannot use the Custom Logo lens yet: "
            "that shape depends on browser-side uploaded image pixels, not a saved deterministic equation."
        )
    if lens_type not in VERIFIED_EQUATION_LENSES:
        supported = ", ".join(sorted(VERIFIED_EQUATION_LENSES))
        raise RuntimeError(
            f"Fabricator verified STEP export does not have a deterministic equation for lensType={lens_type!r}. "
            f"Supported equation-defined lenses: {supported}."
        )


def morph_rings_height(x: float, y: float, state: dict) -> float:
    """Exact Python port of the visualizer's Morph Rings height function."""

    width = float(state.get("panelWidth", WIDTH_MM))
    height = float(state.get("panelHeight", HEIGHT_MM))
    params = params_for(state)
    scale = float(state.get("shapeScale") or 1.0)
    if state.get("mirrorLens"):
        x, y = abs(x), abs(y)
    x *= scale
    y *= scale

    wavelength = max(1e-6, float(params.get("wavelength", 35.0)))
    amplitude = float(params.get("amplitude", 35.0))
    ripple_curvature = max(0.25, float(params.get("ripple_curvature", 1.0)))
    ripple_profile = str(params.get("ripple_profile", "current"))
    period = max(1.0, float(params.get("morph_period", 5.0)))
    bias = float(params.get("bias", 0.38))
    phase_shift = float(params.get("phase_shift", 0.19))

    tau = math.pi * 2.0
    max_d = min(width, height) * 0.5
    ax, ay = abs(x), abs(y)
    d_sq = max(ax, ay)
    d_cr = math.hypot(x, y)
    d_mid = 0.5 * (d_sq + d_cr)
    cycle_len = wavelength * period
    osc = 0.5 - 0.5 * math.cos(tau * d_mid / cycle_len)
    w = min(1.0, max(0.0, osc + (bias - 0.5)))
    d = d_sq * (1.0 - w) + d_cr * w
    if d >= max_d:
        return 0.0
    fade = 0.5 * (1.0 + math.cos(math.pi * d / max_d))
    ridge_base = 0.5 * (1.0 + math.sin(tau * (d / wavelength + phase_shift)))
    ridge = ripple_profile01(ridge_base, ripple_curvature, ripple_profile)
    return amplitude * ridge * fade


def sine_wave_lens_height(x: float, y: float, state: dict) -> float:
    """Low-profile concentric sine lens with optional partial-ring cutoff."""

    width = float(state.get("panelWidth", WIDTH_MM))
    height = float(state.get("panelHeight", HEIGHT_MM))
    params = params_for(state)
    scale = float(state.get("shapeScale") or 1.0)
    if state.get("mirrorLens"):
        x, y = abs(x), abs(y)
    x *= scale
    y *= scale

    half_w = width * 0.5
    half_h = height * 0.5
    wavelength = max(1.0, float(params.get("wavelength", 70.0)))
    amplitude = max(0.0, float(params.get("amplitude", 14.0)))
    visible_rings = max(1, int(round(float(params.get("visible_rings", 4) or 4))))
    outer_fade = max(0.0, float(params.get("outer_fade_width", 18.0) or 0.0))
    edge_taper = max(0.0, float(params.get("edge_taper_width", 50.0) or 0.0))
    ripple_phase = float(params.get("ripple_phase", 0.0) or 0.0)

    d_edge = min(half_w - abs(x), half_h - abs(y))
    if d_edge <= 0.0:
        return 0.0

    radius = math.hypot(x, y)
    cutoff_radius = max(0.0, (visible_rings - 0.5) * wavelength)
    if cutoff_radius > 0.0 and outer_fade <= 0.0 and radius >= cutoff_radius:
        return 0.0

    wave = 0.5 * (1.0 + math.cos(math.pi * 2.0 * (radius / wavelength + ripple_phase)))
    edge_env = 1.0 if edge_taper <= 0.0 else smootherstep01(d_edge / edge_taper)
    ring_env = 1.0
    if cutoff_radius > 0.0:
        if outer_fade <= 0.0:
            ring_env = 1.0 if radius < cutoff_radius else 0.0
        else:
            ring_env = 1.0 - smootherstep01((radius - (cutoff_radius - outer_fade)) / outer_fade)
    return amplitude * wave * edge_env * max(0.0, min(1.0, ring_env))


def round_sine_lens_height(x: float, y: float, state: dict) -> float:
    """Circular-footprint low-profile sine lens."""

    width = float(state.get("panelWidth", WIDTH_MM))
    height = float(state.get("panelHeight", HEIGHT_MM))
    params = params_for(state)
    scale = float(state.get("shapeScale") or 1.0)
    if state.get("mirrorLens"):
        x, y = abs(x), abs(y)
    x *= scale
    y *= scale

    diameter = max(1.0, min(float(params.get("diameter", min(width, height)) or min(width, height)), min(width, height)))
    radius_limit = diameter * 0.5
    wavelength = max(1.0, float(params.get("wavelength", 70.0)))
    amplitude = max(0.0, float(params.get("amplitude", 14.0)))
    visible_rings = max(1, int(round(float(params.get("visible_rings", 4) or 4))))
    outer_fade = max(0.0, float(params.get("outer_fade_width", 18.0) or 0.0))
    edge_taper = max(0.0, float(params.get("edge_taper_width", 50.0) or 0.0))
    ripple_phase = float(params.get("ripple_phase", 0.0) or 0.0)

    radius = math.hypot(x, y)
    if radius >= radius_limit:
        return 0.0

    cutoff_radius = min(radius_limit, max(0.0, (visible_rings - 0.5) * wavelength))
    if cutoff_radius > 0.0 and outer_fade <= 0.0 and radius >= cutoff_radius:
        return 0.0

    wave = 0.5 * (1.0 + math.cos(math.pi * 2.0 * (radius / wavelength + ripple_phase)))
    d_edge = radius_limit - radius
    edge_env = 1.0 if edge_taper <= 0.0 else smootherstep01(d_edge / edge_taper)
    ring_env = 1.0
    if cutoff_radius > 0.0:
        if outer_fade <= 0.0:
            ring_env = 1.0 if radius < cutoff_radius else 0.0
        else:
            ring_env = 1.0 - smootherstep01((radius - (cutoff_radius - outer_fade)) / outer_fade)
    return amplitude * wave * edge_env * max(0.0, min(1.0, ring_env))


def stepped_ripple_height(x: float, y: float, state: dict) -> float:
    """Exact Python port of the visualizer's Stepped Ripple height function."""

    width = float(state.get("panelWidth", WIDTH_MM))
    height = float(state.get("panelHeight", HEIGHT_MM))
    params = params_for(state)
    scale = float(state.get("shapeScale") or 1.0)
    if state.get("mirrorLens"):
        x, y = abs(x), abs(y)
    x *= scale
    y *= scale

    wavelength = max(1.0, float(params.get("wavelength", 42.0)))
    amplitude = max(0.0, float(params.get("amplitude", 100.0)))
    amplitude_scale = amplitude / 100.0
    step = float(params.get("step", 10.0))
    ripple_curvature = max(0.25, float(params.get("ripple_curvature", 1.0)))
    ripple_profile = str(params.get("ripple_profile", "current"))
    ripple_phase = float(params.get("ripple_phase", 0.0) or 0.0)
    ridge_width = max(0.0, min(1.0, float(params.get("ridge_width", 0.5) if params.get("ridge_width", 0.5) is not None else 0.5)))
    ripple_contrast = max(0.1, float(params.get("ripple_contrast", 1.0) or 1.0))
    valley_lift = max(0.0, min(0.8, float(params.get("valley_lift", 0.0) or 0.0)))
    ripple_stretch = max(0.1, float(params.get("ripple_stretch", 1.0) or 1.0))
    ripple_angle = math.radians(float(params.get("ripple_angle", 0.0) or 0.0))
    radial_falloff = max(0.0, min(1.0, float(params.get("radial_falloff", 0.0) or 0.0)))
    fade_ratio = float(params.get("edgeFade", 0.18))
    edge_taper_param = params.get("edge_taper_width", None)
    sharp = float(params.get("bumpSharp", 0.5))

    half_w = width * 0.5
    half_h = height * 0.5
    max_r = math.hypot(half_w, half_h)
    ca = math.cos(ripple_angle)
    sa = math.sin(ripple_angle)
    edge_taper = max(0.0, float(edge_taper_param)) if edge_taper_param is not None else min(width, height) * fade_ratio
    margin = max(0.0001, edge_taper)
    dx = half_w - abs(x)
    dy = half_h - abs(y)
    d = min(dx, dy)
    if d <= 0:
        return 0.0

    env = 1.0 if d >= margin else 0.5 * (1.0 - math.cos(math.pi * (d / margin)))
    xr = x * ca + y * sa
    yr = (-x * sa + y * ca) / ripple_stretch
    r = math.hypot(xr, yr)
    bulge = center_bulge_height(r, params) * amplitude_scale
    amp_local = max(0.0, amplitude - step * (r / wavelength))
    radial = min(1.0, r / max_r)
    falloff_env = 1.0 if radial_falloff <= 0.0 else math.pow(1.0 - smootherstep01(radial), radial_falloff * 1.8)
    wave_raw = 0.5 * (1.0 + math.cos(math.pi * 2.0 * (r / wavelength + ripple_phase)))
    if ripple_profile == "continuous_s_ripple":
        wave = continuous_s_ripple01(r / wavelength + ripple_phase, ripple_curvature)
        if sharp == 0.5:
            pass
        elif sharp > 0.5:
            n = 1.0 + (sharp - 0.5) * 8.0
            wave = math.pow(wave, n)
        else:
            n = 1.0 + (0.5 - sharp) * 8.0
            wave = 1.0 - math.pow(1.0 - wave, n)
        wave = shape_ripple_value01(wave, ridge_width, ripple_contrast, valley_lift)
        return (amp_local * wave * falloff_env + bulge) * env
    if sharp == 0.5:
        wave = wave_raw
    elif sharp > 0.5:
        n = 1.0 + (sharp - 0.5) * 8.0
        wave = math.pow(wave_raw, n)
    else:
        n = 1.0 + (0.5 - sharp) * 8.0
        wave = 1.0 - math.pow(1.0 - wave_raw, n)
    shaped_wave = wave_raw if ripple_profile == "perfect_s_curve" else wave
    wave = shape_ripple_value01(
        ripple_profile01(shaped_wave, ripple_curvature, ripple_profile),
        ridge_width,
        ripple_contrast,
        valley_lift,
    )
    return (amp_local * wave * falloff_env + bulge) * env


def ripple_field_height(x: float, y: float, state: dict) -> float:
    width = float(state.get("panelWidth", WIDTH_MM))
    height = float(state.get("panelHeight", HEIGHT_MM))
    params = params_for(state)
    scale = float(state.get("shapeScale") or 1.0)
    if state.get("mirrorLens"):
        x, y = abs(x), abs(y)
    x *= scale
    y *= scale

    half_w = width * 0.5
    half_h = height * 0.5
    ripple_curvature = max(
        0.25,
        float(params.get("ripple_curvature", params.get("bulb_roundness", 1.0))),
    )
    ripple_profile = str(params.get("ripple_profile", "current"))
    ripple_phase = float(params.get("ripple_phase", 0.0) or 0.0)
    ridge_width = max(0.0, min(1.0, float(params.get("ridge_width", 0.5) if params.get("ridge_width", 0.5) is not None else 0.5)))
    ripple_contrast = max(0.1, float(params.get("ripple_contrast", 1.0) or 1.0))
    valley_lift = max(0.0, min(0.8, float(params.get("valley_lift", 0.0) or 0.0)))
    ripple_stretch = max(0.1, float(params.get("ripple_stretch", 1.0) or 1.0))
    ripple_angle = math.radians(float(params.get("ripple_angle", 0.0) or 0.0))
    radial_falloff = max(0.0, min(1.0, float(params.get("radial_falloff", 0.0) or 0.0)))
    curve_strength = max(0.25, float(params.get("curve_strength", 1.0)))
    wavelength = max(1e-6, float(params.get("wavelength", 70.0)))
    amplitude = float(params.get("amplitude", 8.0))
    edge_taper = max(0.0, float(params.get("edge_taper_width", params.get("edge_taper", 0.0)) or 0.0))
    dx = half_w - abs(x)
    dy = half_h - abs(y)
    d = min(dx, dy)
    if d <= 0:
        return 0.0
    edge_env = 1.0 if edge_taper <= 0.0 else smootherstep01(d / edge_taper)
    ca = math.cos(ripple_angle)
    sa = math.sin(ripple_angle)
    xr = x * ca + y * sa
    yr = (-x * sa + y * ca) / ripple_stretch
    r = math.hypot(xr, yr)
    wave = 0.5 * (1.0 + math.cos(math.pi * 2.0 * (r / wavelength + ripple_phase)))
    rounded = shape_ripple_value01(
        ripple_profile01(wave, ripple_curvature, ripple_profile),
        ridge_width,
        ripple_contrast,
        valley_lift,
    )
    wave_out = math.pow(rounded, curve_strength)
    radial = min(1.0, r / math.hypot(half_w, half_h))
    falloff_env = 1.0 if radial_falloff <= 0.0 else math.pow(1.0 - smootherstep01(radial), radial_falloff * 1.8)
    return amplitude * wave_out * edge_env * falloff_env + center_bulge_height(r, params)


def gravitational_wave_height(x: float, y: float, state: dict) -> float:
    width = float(state.get("panelWidth", WIDTH_MM))
    height = float(state.get("panelHeight", HEIGHT_MM))
    params = params_for(state)
    scale = float(state.get("shapeScale") or 1.0)
    if state.get("mirrorLens"):
        x, y = abs(x), abs(y)
    x *= scale
    y *= scale

    half_w = width * 0.5
    half_h = height * 0.5
    max_r = math.hypot(half_w, half_h)
    wavelength = max(1.0, float(params.get("wavelength", 58.0)))
    amplitude = max(0.0, float(params.get("amplitude", 28.0)))
    separation = max(0.0, float(params.get("source_separation", 120.0)))
    balance = max(0.0, min(1.0, float(params.get("source_balance", 0.5))))
    interference = max(0.0, min(1.0, float(params.get("interference_strength", 0.65))))
    twist = float(params.get("spiral_twist", 2.2))
    chirp = float(params.get("chirp", 0.38))
    ellipticity = max(0.1, float(params.get("ellipticity", 1.0)))
    falloff = max(0.0, min(1.0, float(params.get("falloff", 0.25))))
    edge_taper = max(0.0, float(params.get("edge_taper_width", 70.0) or 0.0))
    ripple_curvature = max(0.25, float(params.get("ripple_curvature", 1.25)))
    ripple_profile = str(params.get("ripple_profile", "continuous_s_ripple"))
    ripple_phase = math.pi * 2.0 * float(params.get("ripple_phase", 0.0) or 0.0)
    source_phase = math.pi * 2.0 * max(0.0, min(1.0, float(params.get("source_phase_offset", 0.41) or 0.41)))
    ridge_width = max(0.0, min(1.0, float(params.get("ridge_width", 0.5) or 0.5)))
    ripple_contrast = max(0.1, float(params.get("ripple_contrast", 1.0) or 1.0))
    valley_lift = max(0.0, min(0.8, float(params.get("valley_lift", 0.0) or 0.0)))
    angle = math.radians(float(params.get("orbit_angle", 0.0)))
    ca = math.cos(angle)
    sa = math.sin(angle)

    xr = x * ca + y * sa
    yr = (-x * sa + y * ca) / ellipticity
    r_mid = math.hypot(xr, yr)
    theta = math.atan2(yr, xr)
    r1 = math.hypot(xr + separation * 0.5, yr)
    r2 = math.hypot(xr - separation * 0.5, yr)
    d_edge = min(half_w - abs(x), half_h - abs(y))
    if d_edge <= 0.0:
        return 0.0

    tau = math.pi * 2.0
    bend = twist * theta + tau * chirp * (r_mid * r_mid) / max(1.0, max_r * wavelength)

    def wave_at(radius: float, phase: float) -> float:
        raw = 0.5 + 0.5 * math.cos(tau * (radius / wavelength) + bend + ripple_phase + phase)
        return shape_ripple_value01(
            ripple_profile01(raw, ripple_curvature, ripple_profile),
            ridge_width,
            ripple_contrast,
            valley_lift,
        )

    w1 = wave_at(r1, 0.0)
    w2 = wave_at(r2, source_phase)
    binary = w1 * (1.0 - balance) + w2 * balance
    interference_bands = shape_ripple_value01(
        ripple_profile01(abs(w1 - w2), ripple_curvature, ripple_profile),
        ridge_width,
        ripple_contrast,
        valley_lift,
    )
    spiral = shape_ripple_value01(
        ripple_profile01(
            0.5 + 0.5 * math.cos(tau * (r_mid / wavelength) + bend + ripple_phase),
            ripple_curvature,
            ripple_profile,
        ),
        ridge_width,
        ripple_contrast,
        valley_lift,
    )
    wave = binary * (1.0 - interference) + ((interference_bands + spiral) * 0.5) * interference

    edge_env = 1.0 if edge_taper <= 0.0 else smootherstep01(d_edge / edge_taper)
    radial = min(1.0, r_mid / max_r)
    falloff_env = 1.0 if falloff <= 0.0 else math.pow(1.0 - smootherstep01(radial), falloff * 1.8)
    return amplitude * wave * edge_env * falloff_env + center_bulge_height(r_mid, params)


def torus_field_height(x: float, y: float, state: dict) -> float:
    width = float(state.get("panelWidth", WIDTH_MM))
    height = float(state.get("panelHeight", HEIGHT_MM))
    params = params_for(state)
    scale = float(state.get("shapeScale") or 1.0)
    if state.get("mirrorLens"):
        x, y = abs(x), abs(y)
    x *= scale
    y *= scale

    cols = max(1, int(params.get("cols", 3)))
    rows = max(1, int(params.get("rows", 3)))
    outer_r = float(params.get("torus_outer_r", 50.0))
    inner_r = float(params.get("torus_inner_r", 18.0))
    torus_height = float(params.get("torus_height", 14.0))
    ripple_curvature = max(0.25, float(params.get("ripple_curvature", 1.0)))
    centres_x = [-width / 2.0 + (i + 1) * width / (cols + 1) for i in range(cols)]
    centres_y = [-height / 2.0 + (j + 1) * height / (rows + 1) for j in range(rows)]
    mid_r = (outer_r + inner_r) / 2.0
    wall_r = max(0.1, (outer_r - inner_r) / 2.0)
    z = 0.0
    for a in centres_x:
        for b in centres_y:
            r = math.hypot(x - a, y - b)
            diff = r - mid_r
            inside = wall_r * wall_r - diff * diff
            if inside > 0:
                z = max(z, torus_height * curve_profile01(math.sqrt(inside) / wall_r, ripple_curvature))
    return z


def hex_cells_height(x: float, y: float, state: dict) -> float:
    width = float(state.get("panelWidth", WIDTH_MM))
    height = float(state.get("panelHeight", HEIGHT_MM))
    params = params_for(state)
    scale = float(state.get("shapeScale") or 1.0)
    if state.get("mirrorLens"):
        x, y = abs(x), abs(y)
    x *= scale
    y *= scale

    cell_size = max(0.1, float(params.get("cell_size", 40.0)))
    cell_depth = float(params.get("cell_depth", 6.0))
    ripple_curvature = max(0.25, float(params.get("ripple_curvature", 1.0)))
    dx = cell_size * 1.5
    dy = cell_size * math.sqrt(3.0)
    radius = cell_size * 0.85
    ncols = math.ceil(width / dx) + 2
    nrows = math.ceil(height / dy) + 2
    z = cell_depth
    for row in range(-nrows, nrows + 1):
        for col in range(-ncols, ncols + 1):
            ax = col * dx
            ay = row * dy + (dy / 2.0 if col % 2 else 0.0)
            r = math.hypot(x - ax, y - ay)
            if r < radius:
                bowl = curve_profile01(0.5 * (1.0 - math.cos(math.pi * r / radius)), ripple_curvature) * cell_depth
                z = min(z, bowl)
    return cell_depth - z if bool(params.get("invert", False)) else z


def organic_bulbs_height(x: float, y: float, state: dict) -> float:
    width = float(state.get("panelWidth", WIDTH_MM))
    height = float(state.get("panelHeight", HEIGHT_MM))
    params = params_for(state)
    scale = float(state.get("shapeScale") or 1.0)
    if state.get("mirrorLens"):
        x, y = abs(x), abs(y)
    x *= scale
    y *= scale

    rng = mulberry32(params.get("seed", 7))
    min_r = float(params.get("min_r", 30.0))
    max_r = float(params.get("max_r", 75.0))
    min_h = float(params.get("min_h", 8.0))
    max_h = float(params.get("max_h", 22.0))
    ripple_curvature = max(0.25, float(params.get("ripple_curvature", 1.0)))
    margin = max(min_r, max_r)
    z = 0.0
    for _ in range(max(1, int(params.get("n_bulbs", 24)))):
        cx = rng() * (width - 2.0 * margin) - (width / 2.0 - margin)
        cy = rng() * (height - 2.0 * margin) - (height / 2.0 - margin)
        radius = min_r + rng() * max(0.0, max_r - min_r)
        hpk = min_h + rng() * max(0.0, max_h - min_h)
        r = math.hypot(x - cx, y - cy)
        if r < radius:
            z = max(z, hpk * curve_profile01(0.5 * (1.0 + math.cos(math.pi * r / radius)), ripple_curvature))
    return z


def compound_bulbs_height(x: float, y: float, state: dict) -> float:
    """Irregular large-scale convex and concave field for varied pixel displacement."""

    width = float(state.get("panelWidth", WIDTH_MM))
    height = float(state.get("panelHeight", HEIGHT_MM))
    params = params_for(state)
    scale = float(state.get("shapeScale") or 1.0)
    if state.get("mirrorLens"):
        x, y = abs(x), abs(y)
    x *= scale
    y *= scale

    count = max(3, int(params.get("feature_count", 13)))
    rng = mulberry32(params.get("seed", 41))
    min_radius = max(5.0, float(params.get("min_radius", 55.0)))
    max_radius = max(min_radius, float(params.get("max_radius", 125.0)))
    relief = max(0.1, float(params.get("relief", 32.0)))
    anisotropy = max(0.0, min(0.9, float(params.get("anisotropy", 0.55))))
    well_mix = max(0.0, min(0.49, float(params.get("well_mix", 0.23))))
    saddle_strength = max(0.0, float(params.get("saddle_strength", 0.65)))
    profile_power = max(0.2, float(params.get("profile_power", 1.35)))
    edge_taper = max(1.0, float(params.get("edge_taper_width", 55.0)))
    margin = min(max_radius * 0.55, min(width, height) * 0.22)
    well_every = max(3, round(1.0 / well_mix)) if well_mix > 0.0 else None

    field = 0.0
    for i in range(count):
        radius = max_radius if i == 0 else min_radius + rng() * (max_radius - min_radius)
        minor_ratio = 1.0 - anisotropy * (0.35 + 0.65 * rng())
        angle = rng() * math.pi * 2.0
        cx = 0.0 if i == 0 else rng() * max(1.0, width - 2.0 * margin) - (width * 0.5 - margin)
        cy = 0.0 if i == 0 else rng() * max(1.0, height - 2.0 * margin) - (height * 0.5 - margin)
        is_well = i > 0 and well_every is not None and i % well_every == well_every - 1
        strength = (
            -relief * saddle_strength * (0.55 + 0.35 * rng())
            if is_well
            else relief * (0.65 + 0.35 * rng())
        )
        dx = x - cx
        dy = y - cy
        ca = math.cos(angle)
        sa = math.sin(angle)
        ex = (dx * ca + dy * sa) / radius
        ey = (-dx * sa + dy * ca) / (radius * minor_ratio)
        q = math.hypot(ex, ey)
        if q < 1.0:
            cap = 0.5 * (1.0 + math.cos(math.pi * q))
            field += strength * math.pow(cap, profile_power)

    shaped = max(0.0, math.tanh(field / (relief * 0.72)))
    d_edge = min(width * 0.5 - abs(x), height * 0.5 - abs(y))
    edge_env = 0.0 if d_edge <= 0.0 else smootherstep01(d_edge / edge_taper)
    return relief * shaped * edge_env


def pyramid_grid_height(x: float, y: float, state: dict) -> float:
    width = float(state.get("panelWidth", WIDTH_MM))
    height = float(state.get("panelHeight", HEIGHT_MM))
    params = params_for(state)
    scale = float(state.get("shapeScale") or 1.0)
    if state.get("mirrorLens"):
        x, y = abs(x), abs(y)
    x *= scale
    y *= scale

    cols = max(1, int(params.get("cols", 8)))
    rows = max(1, int(params.get("rows", 8)))
    pyramid_h = float(params.get("pyramid_h", 20.0))
    ripple_curvature = max(0.25, float(params.get("ripple_curvature", 1.0)))
    cell_w = width / cols
    cell_h = height / rows
    u = ((x + width / 2.0) % cell_w) / cell_w - 0.5
    v = ((y + height / 2.0) % cell_h) / cell_h - 0.5
    d = max(abs(u), abs(v)) * 2.0
    return pyramid_h * curve_profile01(1.0 - d, ripple_curvature)


def dome_height(x: float, y: float, state: dict) -> float:
    params = params_for(state)
    scale = float(state.get("shapeScale") or 1.0)
    if state.get("mirrorLens"):
        x, y = abs(x), abs(y)
    x *= scale
    y *= scale

    radius = max(0.0001, float(params.get("radius", 280.0)))
    height = max(0.0001, float(params.get("height", 40.0)))
    ripple_curvature = max(0.25, float(params.get("ripple_curvature", 1.0)))
    sphere_r = (radius * radius + height * height) / (2.0 * height)
    center_z = height - sphere_r
    r2 = x * x + y * y
    if r2 >= radius * radius:
        return 0.0
    z = center_z + math.sqrt(sphere_r * sphere_r - r2)
    return height * curve_profile01(z / height, ripple_curvature)


def interlocking_teardrops_height(x: float, y: float, state: dict) -> float:
    width = float(state.get("panelWidth", WIDTH_MM))
    height = float(state.get("panelHeight", HEIGHT_MM))
    params = params_for(state)
    scale = float(state.get("shapeScale") or 1.0)
    if state.get("mirrorLens"):
        x, y = abs(x), abs(y)
    x *= scale
    y *= scale

    cols = max(1, int(params.get("cols", 4)))
    rows = max(1, int(params.get("rows", 4)))
    size = float(params.get("size", 70.0))
    drop_height = float(params.get("height", 16.0))
    ripple_curvature = max(0.25, float(params.get("ripple_curvature", 1.0)))
    asymmetry = float(params.get("asymmetry", 0.5))
    rot_variance = float(params.get("rot_variance", 0.8))
    jitter = float(params.get("jitter", 0.3))
    rng = mulberry32(params.get("seed", 3))
    dx_c = width / cols
    dy_c = height / rows
    z = 0.0
    for i in range(cols):
        for j in range(rows):
            cx = -width / 2.0 + (i + 0.5) * dx_c + (rng() - 0.5) * dx_c * jitter
            cy = -height / 2.0 + (j + 0.5) * dy_c + (rng() - 0.5) * dy_c * jitter
            angle = rng() * math.pi * 2.0 * rot_variance
            ca = math.cos(angle)
            sa = math.sin(angle)
            ex = x - cx
            ey = y - cy
            lx = ex * ca + ey * sa
            ly = -ex * sa + ey * ca
            r = math.hypot(lx, ly)
            if r > size:
                continue
            theta = math.atan2(ly, lx)
            r_max = size * (1.0 + asymmetry * math.cos(theta))
            if r >= r_max:
                continue
            t = r / r_max
            z = max(z, drop_height * curve_profile01(0.5 * (1.0 + math.cos(math.pi * t)), ripple_curvature))
    return z


def wave_lattice_height(x: float, y: float, state: dict) -> float:
    params = params_for(state)
    scale = float(state.get("shapeScale") or 1.0)
    if state.get("mirrorLens"):
        x, y = abs(x), abs(y)
    x *= scale
    y *= scale

    wavelength_x = max(1e-6, float(params.get("wavelength_x", 80.0)))
    wavelength_y = max(1e-6, float(params.get("wavelength_y", 80.0)))
    amplitude = float(params.get("amplitude", 10.0))
    ripple_curvature = max(0.25, float(params.get("ripple_curvature", 1.0)))
    ripple_profile = str(params.get("ripple_profile", "current"))
    diagonal_mix = float(params.get("diagonal_mix", 0.0))
    rotation = float(params.get("rotation", 0.0))
    rad = rotation * math.pi / 180.0
    ca = math.cos(rad)
    sa = math.sin(rad)
    avg = (wavelength_x + wavelength_y) / 2.0
    rx = x * ca + y * sa
    ry = -x * sa + y * ca
    wx = math.sin(2.0 * math.pi * rx / wavelength_x)
    wy = math.sin(2.0 * math.pi * ry / wavelength_y)
    d1 = math.sin(2.0 * math.pi * (rx + ry) / avg)
    d2 = math.sin(2.0 * math.pi * (rx - ry) / avg)
    val = (wx + wy) * (1.0 - diagonal_mix) + (d1 + d2) * diagonal_mix
    return amplitude * ripple_profile01(0.25 * (2.0 + val), ripple_curvature, ripple_profile)


def logo_height(x: float, y: float, state: dict) -> float:
    # Logo geometry depends on browser-side raster image data that is not part of
    # the saved numeric state. Return a valid flat solid rather than failing.
    return 0.0


def voronoi_bulbs_height(x: float, y: float, state: dict) -> float:
    width = float(state.get("panelWidth", WIDTH_MM))
    height = float(state.get("panelHeight", HEIGHT_MM))
    params = params_for(state)
    scale = float(state.get("shapeScale") or 1.0)
    if state.get("mirrorLens"):
        x, y = abs(x), abs(y)
    x *= scale
    y *= scale

    rng = mulberry32(params.get("seed", 5))
    ripple_curvature = max(0.25, float(params.get("ripple_curvature", 1.0)))
    margin = 20.0
    seeds = [
        (
            rng() * (width - 2.0 * margin) - (width / 2.0 - margin),
            rng() * (height - 2.0 * margin) - (height / 2.0 - margin),
        )
        for _ in range(max(3, int(params.get("n_seeds", 20))))
    ]
    d1 = float("inf")
    d2 = float("inf")
    for sx, sy in seeds:
        d = math.hypot(x - sx, y - sy)
        if d < d1:
            d2 = d1
            d1 = d
        elif d < d2:
            d2 = d
    r_cell = (d1 + d2) * 0.5 * float(params.get("fill", 0.85))
    if d1 > r_cell:
        return 0.0
    t = d1 / r_cell
    return float(params.get("height", 18.0)) * curve_profile01(0.5 * (1.0 + math.cos(math.pi * t)), ripple_curvature)


LENS_HEIGHT_FUNCTIONS = {
    "round_sine_lens": round_sine_lens_height,
    "sine_wave_lens": sine_wave_lens_height,
    "ripple_field": ripple_field_height,
    "gravitational_wave": gravitational_wave_height,
    "stepped_ripple": stepped_ripple_height,
    "torus_field": torus_field_height,
    "hex_cells": hex_cells_height,
    "organic_bulbs": organic_bulbs_height,
    "compound_bulbs": compound_bulbs_height,
    "pyramid_grid": pyramid_grid_height,
    "dome": dome_height,
    "morph_rings": morph_rings_height,
    "interlocking_teardrops": interlocking_teardrops_height,
    "wave_lattice": wave_lattice_height,
    "logo": logo_height,
    "voronoi_bulbs": voronoi_bulbs_height,
}


def lens_height(x: float, y: float, state: dict) -> float:
    lens_type = state.get("lensType")
    fn = LENS_HEIGHT_FUNCTIONS.get(lens_type)
    if fn:
        return apply_flat_edge(fn(x, y, state), x, y, state)
    # Last-resort no-fail fallback for future procedural lens types. This keeps
    # the app exportable, but the README will still name the unrecognised type.
    return apply_flat_edge(0.0, x, y, state)


def apply_flat_edge(z: float, x: float, y: float, state: dict) -> float:
    flat_edge = max(0.0, float(state.get("flatEdgeWidth", state.get("flat_edge_width", 10.0)) or 0.0))
    if flat_edge <= 0.0:
        return z
    d = footprint_inset(x, y, state)
    if d <= flat_edge:
        return 0.0
    edge_blend = min(6.0, max(2.0, flat_edge * 0.5))
    return z * smootherstep01((d - flat_edge) / edge_blend)


def footprint_inset(x: float, y: float, state: dict) -> float:
    if is_round_footprint(state):
        return round_footprint_radius(state) - math.hypot(x, y)
    width = float(state.get("panelWidth", WIDTH_MM))
    height = float(state.get("panelHeight", HEIGHT_MM))
    return min(width * 0.5 - abs(x), height * 0.5 - abs(y))


def round_export_border_width(state: dict, cad_samples: int) -> float:
    """Return a safe planar annulus for trimming the B-spline to a circle.

    A cubic control-pole surface needs several flat control rows around the trim
    curve so the exact circular wire lies on the front surface. The user's flat
    edge is retained when it is wider; otherwise only the minimum CAD support
    band is flattened for a reliable sewn solid.
    """

    requested = max(0.0, float(state.get("flatEdgeWidth", state.get("flat_edge_width", 10.0)) or 0.0))
    spacing_x = WIDTH_MM / max(1, cad_samples - 1)
    spacing_y = HEIGHT_MM / max(1, cad_samples - 1)
    support_band = DEGREE * math.sqrt(2.0) * max(spacing_x, spacing_y)
    return max(requested, support_band)


def cad_samples_for_state(state: dict) -> int:
    """Choose a CAD grid that follows the visualizer without making unusable files."""

    resolution = max(0.1, float(state.get("resolution") or 1.0))
    width = float(state.get("panelWidth", WIDTH_MM))
    height = float(state.get("panelHeight", HEIGHT_MM))
    quality = str(state.get("stepExportQuality", state.get("cadQuality", "standard")) or "standard")
    profile = STEP_QUALITY_PROFILES.get(quality, STEP_QUALITY_PROFILES["standard"])

    # The visualizer may use 0.5 mm mesh spacing, which would create an enormous
    # CAD file if copied 1:1. Use explicit quality profiles so the normal export
    # stays small enough for fabricators to open while high precision remains
    # available for a final machining handoff if requested.
    target_spacing = max(MIN_CAD_SPACING_MM, resolution * profile["resolution_multiplier"])
    samples = int(math.floor(max(width, height) / target_spacing)) + 1
    samples = max(profile["min_samples"], min(profile["max_samples"], samples))
    if samples % 2 == 0:
        samples += 1
    return samples


def visualizer_reference_samples_for_state(state: dict, minimum_samples: int) -> int:
    visualizer_resolution = max(0.1, float(state.get("resolution") or 1.0))
    ref_spacing = max(0.5, visualizer_resolution)
    ref_samples = int(math.floor(max(WIDTH_MM, HEIGHT_MM) / ref_spacing)) + 1
    ref_samples = min(1001, max(minimum_samples, ref_samples))
    if ref_samples % 2 == 0:
        ref_samples += 1
    return ref_samples


def sample_visualizer_grid(state: dict):
    global WIDTH_MM, HEIGHT_MM, HALF_W, HALF_H, BASE_THICKNESS_MM
    global LAST_REFERENCE_GRID, LAST_REFERENCE_SPACING_MM, LAST_CORNER_CLEANUP_REPORT, LAST_OPTICAL_CLEANUP_REPORT
    WIDTH_MM = float(state.get("panelWidth", WIDTH_MM))
    HEIGHT_MM = float(state.get("panelHeight", HEIGHT_MM))
    HALF_W = WIDTH_MM / 2.0
    HALF_H = HEIGHT_MM / 2.0
    BASE_THICKNESS_MM = float(state.get("thickness", BASE_THICKNESS_MM))
    samples = cad_samples_for_state(state)
    round_boundary_lock = round_export_border_width(state, samples) if is_round_footprint(state) else None

    ref_samples = visualizer_reference_samples_for_state(state, samples)
    reference = sample_height_grid(state, ref_samples)
    ref_spacing = max(WIDTH_MM, HEIGHT_MM) / (ref_samples - 1)
    LAST_REFERENCE_SPACING_MM = ref_spacing
    smooth_height_grid(reference, max(0, int(state.get("smoothing", 0) or 0)), ref_spacing)
    LAST_OPTICAL_CLEANUP_REPORT = apply_optical_surface_cleanup(reference, state)
    if is_round_footprint(state):
        LAST_CORNER_CLEANUP_REPORT = {
            "mode": "not_applicable_round_footprint",
            "corner_flat_mm": 0.0,
            "corner_blend_mm": 0.0,
        }
    else:
        LAST_CORNER_CLEANUP_REPORT = apply_fabricator_corner_cleanup(reference, state)
    enforce_flat_footprint_border(reference, state, round_boundary_lock)
    LAST_REFERENCE_GRID = reference

    if ref_samples == samples:
        return reference

    values = []
    for i in range(samples):
        y = -HALF_H + HEIGHT_MM * i / (samples - 1)
        row = []
        for j in range(samples):
            x = -HALF_W + WIDTH_MM * j / (samples - 1)
            row.append(bilinear_grid_height(reference, x, y))
        values.append(row)
    enforce_flat_footprint_border(values, state, round_boundary_lock)
    return values


def sample_height_grid(state: dict, samples: int) -> list[list[float]]:
    values = []
    for i in range(samples):
        y = -HALF_H + HEIGHT_MM * i / (samples - 1)
        row = []
        for j in range(samples):
            x = -HALF_W + WIDTH_MM * j / (samples - 1)
            row.append(lens_height(x, y, state))
        values.append(row)
    return values


def smooth_height_grid(values: list[list[float]], passes: int, spacing_mm: float | None = None) -> None:
    # Match the visualizer's optional box-blur smoothing. The UI value is a
    # radius in millimetres, converted into control-grid cells here so smoothing
    # stays visible and roughly consistent at different export qualities.
    samples = len(values)
    if samples <= 1 or passes <= 0:
        return
    spacing = spacing_mm or LAST_REFERENCE_SPACING_MM or (max(WIDTH_MM, HEIGHT_MM) / max(1, samples - 1))
    radius = max(1, int(round(float(passes) / max(0.001, spacing))))
    tmp = [[0.0] * samples for _ in range(samples)]

    for i in range(samples):
        prefix = [0.0] * (samples + 1)
        for j in range(samples):
            prefix[j + 1] = prefix[j] + values[i][j]
        for j in range(samples):
            a = max(0, j - radius)
            b = min(samples - 1, j + radius)
            tmp[i][j] = (prefix[b + 1] - prefix[a]) / (b - a + 1)

    for j in range(samples):
        prefix = [0.0] * (samples + 1)
        for i in range(samples):
            prefix[i + 1] = prefix[i] + tmp[i][j]
        for i in range(samples):
            a = max(0, i - radius)
            b = min(samples - 1, i + radius)
            values[i][j] = (prefix[b + 1] - prefix[a]) / (b - a + 1)


def apply_optical_surface_cleanup(values: list[list[float]], state: dict) -> dict[str, float | bool | str]:
    """Regularise stepped-ripple surfaces for clear acrylic fabrication.

    Ali's review showed small local distortions in the top surface that would be
    optically magnified in polished clear acrylic. The stepped ripple is meant to
    be a clean radial wave, so average the sampled surface into a smooth radial
    profile before the B-spline solid is built. This removes accidental local
    wobble while keeping the broad lens amplitude and ring spacing selected in
    the visualizer.
    """

    enabled = bool(state.get("opticalCleanSurface", True))
    if not enabled or state.get("lensType") != "stepped_ripple":
        return {"enabled": enabled, "mode": "off"}

    samples = len(values)
    if samples < 3:
        return {"enabled": enabled, "mode": "skipped_small_grid"}

    flat_edge = max(0.0, float(state.get("flatEdgeWidth", state.get("flat_edge_width", 10.0)) or 0.0))
    max_radius = max(1e-6, math.hypot(HALF_W, HALF_H))
    bins = max(64, samples * 2)
    sums = [0.0] * bins
    weights = [0.0] * bins

    for i in range(samples):
        y = -HALF_H + HEIGHT_MM * i / (samples - 1)
        for j in range(samples):
            x = -HALF_W + WIDTH_MM * j / (samples - 1)
            inset = footprint_inset(x, y, state)
            if inset <= flat_edge:
                continue
            f = min(bins - 1.0, max(0.0, (math.hypot(x, y) / max_radius) * (bins - 1)))
            b0 = int(math.floor(f))
            b1 = min(bins - 1, b0 + 1)
            t = f - b0
            z = float(values[i][j])
            w0 = 1.0 - t
            w1 = t
            sums[b0] += z * w0
            weights[b0] += w0
            sums[b1] += z * w1
            weights[b1] += w1

    profile = [0.0] * bins
    last = 0.0
    for b in range(bins):
        if weights[b] > 0.0:
            last = sums[b] / weights[b]
        profile[b] = last
    for b in range(bins - 2, -1, -1):
        if weights[b] <= 0.0:
            profile[b] = profile[b + 1]

    for _ in range(3):
        prev = profile[:]
        for b in range(1, bins - 1):
            profile[b] = prev[b - 1] * 0.2 + prev[b] * 0.6 + prev[b + 1] * 0.2

    count_values = 0
    sum_sq = 0.0
    max_adjustment = 0.0
    for i in range(samples):
        y = -HALF_H + HEIGHT_MM * i / (samples - 1)
        for j in range(samples):
            x = -HALF_W + WIDTH_MM * j / (samples - 1)
            inset = footprint_inset(x, y, state)
            before = float(values[i][j])
            if inset <= flat_edge:
                after = 0.0
            else:
                f = min(bins - 1.0, max(0.0, (math.hypot(x, y) / max_radius) * (bins - 1)))
                b0 = int(math.floor(f))
                b1 = min(bins - 1, b0 + 1)
                t = f - b0
                after = profile[b0] * (1.0 - t) + profile[b1] * t
            values[i][j] = after
            adjustment = abs(after - before)
            max_adjustment = max(max_adjustment, adjustment)
            sum_sq += adjustment * adjustment
            count_values += 1

    return {
        "enabled": True,
        "mode": "radial_profile",
        "bins": float(bins),
        "max_adjustment_mm": max_adjustment,
        "rms_adjustment_mm": math.sqrt(sum_sq / count_values) if count_values else 0.0,
    }


def enforce_flat_footprint_border(
    values: list[list[float]],
    state: dict,
    round_boundary_lock: float | None = None,
) -> None:
    """Keep the machining border perfectly flat after smoothing/resampling.

    The analytic lens already tapers to zero at the edge, but smoothing and
    B-spline fitting can reintroduce tiny relief near the perimeter. The
    fabricator model needs a clean registration border, so lock that physical
    flat-edge band to z=0 immediately before the CAD surfaces are built.
    """

    samples = len(values)
    if samples < 2:
        return
    flat_edge = max(0.0, float(state.get("flatEdgeWidth", state.get("flat_edge_width", 10.0)) or 0.0))
    if is_round_footprint(state):
        boundary_lock = round_boundary_lock if round_boundary_lock is not None else round_export_border_width(state, samples)
        radius = round_footprint_radius(state)
    else:
        boundary_lock = flat_edge
        radius = 0.0
    for i in range(samples):
        y = -HALF_H + HEIGHT_MM * i / (samples - 1)
        for j in range(samples):
            x = -HALF_W + WIDTH_MM * j / (samples - 1)
            if is_round_footprint(state):
                d = radius - math.hypot(x, y)
            else:
                d = min(HALF_W - abs(x), HALF_H - abs(y))
            if d <= boundary_lock:
                values[i][j] = 0.0


def apply_fabricator_corner_cleanup(values: list[list[float]], state: dict) -> dict[str, float]:
    """Remove corner pinch/waviness before building the machinable CAD solid.

    Rick's CAM review showed small waves in all four square corners. The lens
    relief is still generated from the visualizer, but the manufacturing export
    needs clean flat corners so the B-spline/loft does not pinch at the boundary.
    """

    samples = len(values)
    params = params_for(state)
    flat_mm = max(
        0.0,
        float(
            state.get(
                "fabricatorCornerFlatMm",
                params.get("fabricator_corner_flat_mm", FABRICATOR_CORNER_FLAT_MM),
            )
        ),
    )
    blend_mm = max(
        1.0,
        float(
            state.get(
                "fabricatorCornerBlendMm",
                params.get("fabricator_corner_blend_mm", FABRICATOR_CORNER_BLEND_MM),
            )
        ),
    )
    active_radius = flat_mm + blend_mm
    affected_points = 0
    flat_points = 0
    max_before = 0.0
    max_after = 0.0
    max_removed = 0.0

    for i in range(samples):
        y = -HALF_H + HEIGHT_MM * i / (samples - 1)
        for j in range(samples):
            x = -HALF_W + WIDTH_MM * j / (samples - 1)
            dx = HALF_W - abs(x)
            dy = HALF_H - abs(y)
            corner_inset = math.hypot(max(0.0, dx), max(0.0, dy))
            if corner_inset >= active_radius:
                continue

            before = float(values[i][j])
            weight = smoothstep01((corner_inset - flat_mm) / blend_mm)
            after = before * weight
            values[i][j] = after
            affected_points += 1
            if corner_inset <= flat_mm:
                flat_points += 1
            max_before = max(max_before, before)
            max_after = max(max_after, after)
            max_removed = max(max_removed, abs(before - after))

    return {
        "corner_flat_mm": flat_mm,
        "corner_blend_mm": blend_mm,
        "corner_active_radius_mm": active_radius,
        "affected_points": affected_points,
        "flat_points": flat_points,
        "max_corner_relief_before_mm": max_before,
        "max_corner_relief_after_mm": max_after,
        "max_corner_relief_removed_mm": max_removed,
    }


def bilinear_grid_height(grid: list[list[float]], x: float, y: float) -> float:
    samples = len(grid)
    gx = (x + HALF_W) / WIDTH_MM * (samples - 1)
    gy = (y + HALF_H) / HEIGHT_MM * (samples - 1)
    x0 = max(0, min(samples - 1, int(math.floor(gx))))
    y0 = max(0, min(samples - 1, int(math.floor(gy))))
    x1 = min(samples - 1, x0 + 1)
    y1 = min(samples - 1, y0 + 1)
    tx = gx - x0
    ty = gy - y0
    a = grid[y0][x0] * (1.0 - tx) + grid[y0][x1] * tx
    b = grid[y1][x0] * (1.0 - tx) + grid[y1][x1] * tx
    return a * (1.0 - ty) + b * ty


def visualizer_fidelity_report(state: dict, cad_grid: list[list[float]]) -> dict:
    """Compare the CAD sampling grid against a high-resolution visualizer field."""

    global LAST_REFERENCE_GRID, LAST_REFERENCE_SPACING_MM, LAST_CORNER_CLEANUP_REPORT, LAST_OPTICAL_CLEANUP_REPORT
    ref_grid = LAST_REFERENCE_GRID
    if ref_grid is None:
        ref_samples = visualizer_reference_samples_for_state(state, len(cad_grid))
        ref_grid = sample_height_grid(state, ref_samples)
        ref_spacing = max(WIDTH_MM, HEIGHT_MM) / (ref_samples - 1)
        smooth_height_grid(ref_grid, max(0, int(state.get("smoothing", 0) or 0)), ref_spacing)
        LAST_OPTICAL_CLEANUP_REPORT = apply_optical_surface_cleanup(ref_grid, state)
        if is_round_footprint(state):
            LAST_CORNER_CLEANUP_REPORT = {
                "mode": "not_applicable_round_footprint",
                "corner_flat_mm": 0.0,
                "corner_blend_mm": 0.0,
            }
        else:
            LAST_CORNER_CLEANUP_REPORT = apply_fabricator_corner_cleanup(ref_grid, state)
        round_boundary_lock = round_export_border_width(state, len(cad_grid)) if is_round_footprint(state) else None
        enforce_flat_footprint_border(ref_grid, state, round_boundary_lock)
        actual_ref_spacing = max(WIDTH_MM, HEIGHT_MM) / (ref_samples - 1)
    else:
        ref_samples = len(ref_grid)
        actual_ref_spacing = LAST_REFERENCE_SPACING_MM

    cad_samples = len(cad_grid)
    count_values = 0
    sum_abs = 0.0
    sum_sq = 0.0
    max_abs = 0.0
    max_at = (0.0, 0.0)

    for iy in range(ref_samples):
        y = -HALF_H + HEIGHT_MM * iy / (ref_samples - 1)
        for ix in range(ref_samples):
            x = -HALF_W + WIDTH_MM * ix / (ref_samples - 1)
            if is_round_footprint(state) and footprint_inset(x, y, state) < -1e-6:
                continue
            diff = bilinear_grid_height(cad_grid, x, y) - float(ref_grid[iy][ix])
            ad = abs(diff)
            count_values += 1
            sum_abs += ad
            sum_sq += diff * diff
            if ad > max_abs:
                max_abs = ad
                max_at = (x, y)

    return {
        "reference_samples": ref_samples,
        "reference_spacing_mm": actual_ref_spacing,
        "compared_points": count_values,
        "mean_abs_mm": sum_abs / count_values if count_values else 0.0,
        "rms_mm": math.sqrt(sum_sq / count_values) if count_values else 0.0,
        "max_abs_mm": max_abs,
        "max_at_x_mm": max_at[0],
        "max_at_y_mm": max_at[1],
    }


def surface_from_grid(grid: list[list[float]], back: bool = False):
    samples = len(grid)
    poles = TColgp_Array2OfPnt(1, samples, 1, samples)
    for i in range(samples):
        y = -HALF_H + HEIGHT_MM * i / (samples - 1)
        for j in range(samples):
            x = -HALF_W + WIDTH_MM * j / (samples - 1)
            z = -BASE_THICKNESS_MM if back else float(grid[i][j])
            poles.SetValue(i + 1, j + 1, gp_Pnt(x, y, z))
    uk, um = knot_arrays(samples, DEGREE)
    vk, vm = knot_arrays(samples, DEGREE)
    # Use the grid as B-spline control poles rather than interpolation targets.
    # Interpolation looks smooth locally but can overshoot badly in hidden CAD
    # extrema. Control-pole surfaces stay within the intended square/height
    # envelope while preserving a continuous machinable profile.
    return Geom_BSplineSurface(poles, uk, vk, um, vm, DEGREE, DEGREE)


def make_side_surface(side: str, grid: list[list[float]]) -> Geom_BSplineSurface:
    samples = len(grid)
    poles = TColgp_Array2OfPnt(1, 2, 1, samples)
    for j in range(samples):
        t = j / (samples - 1)
        if side == "south":
            x, y, z = -HALF_W + WIDTH_MM * t, -HALF_H, grid[0][j]
        elif side == "north":
            x, y, z = -HALF_W + WIDTH_MM * t, HALF_H, grid[-1][j]
        elif side == "west":
            x, y, z = -HALF_W, -HALF_H + HEIGHT_MM * t, grid[j][0]
        elif side == "east":
            x, y, z = HALF_W, -HALF_H + HEIGHT_MM * t, grid[j][-1]
        else:
            raise ValueError(side)
        poles.SetValue(1, j + 1, gp_Pnt(x, y, -BASE_THICKNESS_MM))
        poles.SetValue(2, j + 1, gp_Pnt(x, y, z))

    uk, um = knot_arrays(2, 1)
    vk, vm = knot_arrays(samples, DEGREE)
    return Geom_BSplineSurface(poles, uk, vk, um, vm, 1, DEGREE)


def face_from_surface(surface: Geom_BSplineSurface):
    u1, u2, v1, v2 = surface.Bounds()
    maker = BRepBuilderAPI_MakeFace(surface, u1, u2, v1, v2, TOLERANCE)
    if not maker.IsDone():
        raise RuntimeError("Could not build face")
    return maker.Face()


def bspline_edge(points: list[gp_Pnt]):
    degree = min(DEGREE, len(points) - 1)
    poles = TColgp_Array1OfPnt(1, len(points))
    for i, point in enumerate(points, start=1):
        poles.SetValue(i, point)
    knots, multiplicities = knot_arrays(len(points), degree)
    # Use the sampled lens points as B-spline control poles, not interpolation
    # targets. Interpolating curves can swing past the square perimeter near
    # hard flat corners; a clamped control curve stays inside its control hull.
    curve = Geom_BSplineCurve(poles, knots, multiplicities, degree, False)
    edge = BRepBuilderAPI_MakeEdge(curve)
    if not edge.IsDone():
        raise RuntimeError("Could not build B-spline edge")
    return edge.Edge()


def interpolated_bspline_edge(points: list[gp_Pnt]):
    if len(points) < 2:
        raise RuntimeError("B-spline edge needs at least two points")
    if len(points) == 2:
        return line_edge(points[0], points[1])
    arr = TColgp_Array1OfPnt(1, len(points))
    for i, point in enumerate(points, start=1):
        arr.SetValue(i, point)
    curve_builder = GeomAPI_PointsToBSpline(arr)
    if not curve_builder.IsDone():
        raise RuntimeError("Could not interpolate B-spline profile")
    edge = BRepBuilderAPI_MakeEdge(curve_builder.Curve())
    if not edge.IsDone():
        raise RuntimeError("Could not build interpolated B-spline edge")
    return edge.Edge()


def line_edge(a: gp_Pnt, b: gp_Pnt):
    edge = BRepBuilderAPI_MakeEdge(a, b)
    if not edge.IsDone():
        raise RuntimeError("Could not build line edge")
    return edge.Edge()


def add_top_edges_to_wire(wire: BRepBuilderAPI_MakeWire, top: list[gp_Pnt], state: dict) -> None:
    """Add the top lens profile while preserving square, flat perimeter corners."""

    samples = len(top)
    if samples < 2:
        raise RuntimeError("Top section needs at least two points")
    wire.Add(bspline_edge(top))


def section_wire(grid: list[list[float]], iy: int, state: dict):
    samples = len(grid)
    y = -HALF_H + HEIGHT_MM * iy / (samples - 1)
    top = [
        gp_Pnt(-HALF_W + WIDTH_MM * ix / (samples - 1), y, float(grid[iy][ix]))
        for ix in range(samples)
    ]
    bottom_right = gp_Pnt(HALF_W, y, -BASE_THICKNESS_MM)
    bottom_left = gp_Pnt(-HALF_W, y, -BASE_THICKNESS_MM)
    wire = BRepBuilderAPI_MakeWire()
    add_top_edges_to_wire(wire, top, state)
    wire.Add(line_edge(top[-1], bottom_right))
    wire.Add(line_edge(bottom_right, bottom_left))
    wire.Add(line_edge(bottom_left, top[0]))
    if not wire.IsDone():
        raise RuntimeError(f"Could not build closed section at row {iy}")
    return wire.Wire()


def build_solid(grid: list[list[float]], state: dict):
    """Build a closed smooth CAD solid from bounded surfaces.

    Rick flagged that the ruled loft fixed the corner artifacts but made the
    profile look coarse. This construction keeps the smooth B-spline front
    surface and sews it to a flat rear face plus four side faces, while the
    sampled grid is still locked flat around the square perimeter before this
    step runs.
    """
    if state.get("lensType") == "round_sine_lens":
        return build_round_solid(grid, state)
    if is_round_footprint(state):
        return build_trimmed_round_solid(grid, state)

    return build_square_solid(grid)


def build_square_solid(grid: list[list[float]]):
    """Build the six-face carrier solid used by square and circular exports."""

    faces = [
        face_from_surface(surface_from_grid(grid)),
        face_from_surface(surface_from_grid(grid, back=True)),
        face_from_surface(make_side_surface("south", grid)),
        face_from_surface(make_side_surface("north", grid)),
        face_from_surface(make_side_surface("west", grid)),
        face_from_surface(make_side_surface("east", grid)),
    ]

    sewing = BRepBuilderAPI_Sewing(TOLERANCE)
    for face in faces:
        sewing.Add(face)
    sewing.Perform()
    sewed = sewing.SewedShape()

    if count(sewed, TopAbs_SHELL) != 1:
        raise RuntimeError(f"Could not sew one closed shell; shells={count(sewed, TopAbs_SHELL)}")

    shell_explorer = TopExp_Explorer(sewed, TopAbs_SHELL)
    shell = TopoDS.Shell_s(shell_explorer.Current())
    solid_maker = BRepBuilderAPI_MakeSolid(shell)
    if not solid_maker.IsDone():
        raise RuntimeError("Could not build solid from sewn shell")
    solid = solid_maker.Solid()
    if not BRepCheck_Analyzer(solid).IsValid():
        raise RuntimeError("Sewn smooth surface solid is invalid")
    return solid


def build_trimmed_round_solid(grid: list[list[float]], state: dict):
    """Build a circular solid while preserving a non-radial 2D lens equation."""

    radius = round_footprint_radius(state)
    carrier = build_square_solid(grid)
    z_max = max(max(row) for row in grid)
    cylinder_height = BASE_THICKNESS_MM + max(1.0, z_max) + 1.0
    cylinder = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(0, 0, -BASE_THICKNESS_MM), gp_Dir(0, 0, 1)),
        radius,
        cylinder_height,
    ).Shape()
    common = BRepAlgoAPI_Common(carrier, cylinder)
    common.Build()
    if not common.IsDone():
        raise RuntimeError("Could not trim the smooth lens solid to its circular footprint")
    solid = common.Shape()
    if count(solid, TopAbs_SOLID) != 1 or not BRepCheck_Analyzer(solid).IsValid():
        raise RuntimeError("Circularly trimmed smooth lens solid is invalid")
    return solid


def build_round_solid(grid: list[list[float]], state: dict):
    """Build a true circular acrylic disc by revolving the centre profile."""

    params = params_for(state)
    diameter = max(
        1.0,
        min(
            float(params.get("diameter", min(WIDTH_MM, HEIGHT_MM)) or min(WIDTH_MM, HEIGHT_MM)),
            min(WIDTH_MM, HEIGHT_MM),
        ),
    )
    radius = diameter * 0.5
    profile_samples = max(17, min(121, len(grid)))
    top_points = []
    for i in range(profile_samples):
        r = radius * i / (profile_samples - 1)
        z = bilinear_grid_height(grid, r, 0.0)
        if i == profile_samples - 1:
            z = 0.0
        top_points.append(gp_Pnt(r, 0, float(z)))

    outer_top = top_points[-1]
    outer_bottom = gp_Pnt(radius, 0, -BASE_THICKNESS_MM)
    axis_bottom = gp_Pnt(0, 0, -BASE_THICKNESS_MM)
    axis_top = top_points[0]

    wire = BRepBuilderAPI_MakeWire()
    wire.Add(interpolated_bspline_edge(top_points))
    wire.Add(line_edge(outer_top, outer_bottom))
    wire.Add(line_edge(outer_bottom, axis_bottom))
    wire.Add(line_edge(axis_bottom, axis_top))
    if not wire.IsDone():
        raise RuntimeError("Could not build circular revolve profile wire")

    face_maker = BRepBuilderAPI_MakeFace(wire.Wire(), True)
    if not face_maker.IsDone():
        raise RuntimeError("Could not build circular revolve profile face")

    revol = BRepPrimAPI_MakeRevol(
        face_maker.Face(),
        gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)),
        math.pi * 2.0,
        True,
    )
    revol.Build()
    if not revol.IsDone():
        raise RuntimeError("Could not revolve circular profile into solid")
    shape = revol.Shape()
    if not BRepCheck_Analyzer(shape).IsValid():
        raise RuntimeError("Revolved circular solid is invalid")
    return shape


def count(shape, kind) -> int:
    explorer = TopExp_Explorer(shape, kind)
    total = 0
    while explorer.More():
        total += 1
        explorer.Next()
    return total


def volume_mm3(shape) -> float:
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return float(props.Mass())


def export_step(shape, path: Path) -> None:
    writer = STEPControl_Writer()
    writer.Transfer(shape, STEPControl_AsIs)
    if writer.Write(str(path)) != IFSelect_RetDone:
        raise RuntimeError(f"STEP export failed: {path}")


def readback_validate(path: Path):
    shape = readback_shape(path)
    return topology_report(shape)


def readback_shape(path: Path):
    reader = STEPControl_Reader()
    if reader.ReadFile(str(path)) != IFSelect_RetDone:
        raise RuntimeError(f"Could not re-read STEP: {path}")
    reader.TransferRoots()
    return reader.OneShape()


def topology_report(shape) -> dict[str, float | bool | int]:
    return {
        "valid": bool(BRepCheck_Analyzer(shape).IsValid()),
        "solids": count(shape, TopAbs_SOLID),
        "shells": count(shape, TopAbs_SHELL),
        "faces": count(shape, TopAbs_FACE),
        "volume_mm3": volume_mm3(shape),
    }


def _face_sample_stats(face, samples: int = 7) -> dict:
    surface = BRepAdaptor_Surface(face)
    u1, u2 = surface.FirstUParameter(), surface.LastUParameter()
    v1, v2 = surface.FirstVParameter(), surface.LastVParameter()
    xs: list[float] = []
    ys: list[float] = []
    zs: list[float] = []
    for iu in range(samples):
        u = u1 + (u2 - u1) * (iu / (samples - 1))
        for iv in range(samples):
            v = v1 + (v2 - v1) * (iv / (samples - 1))
            point = surface.Value(u, v)
            xs.append(float(point.X()))
            ys.append(float(point.Y()))
            zs.append(float(point.Z()))
    return {
        "face": face,
        "surface": surface,
        "u_bounds": (u1, u2),
        "v_bounds": (v1, v2),
        "x_span_mm": max(xs) - min(xs),
        "y_span_mm": max(ys) - min(ys),
        "z_min_mm": min(zs),
        "z_max_mm": max(zs),
        "z_avg_mm": sum(zs) / len(zs),
    }


def front_surface_from_shape(shape, state: dict):
    faces = []
    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        face = TopoDS.Face_s(explorer.Current())
        faces.append(_face_sample_stats(face))
        explorer.Next()
    if not faces:
        raise RuntimeError("Re-imported STEP has no faces to validate")

    if is_round_footprint(state):
        span_floor = round_footprint_diameter(state) * 0.7
        wide_faces = [
            f for f in faces if f["x_span_mm"] >= span_floor and f["y_span_mm"] >= span_floor
        ]
    else:
        wide_faces = [
            f
            for f in faces
            if f["x_span_mm"] >= WIDTH_MM * 0.8 and f["y_span_mm"] >= HEIGHT_MM * 0.8
        ]
    candidates = wide_faces or faces
    return max(candidates, key=lambda f: (f["z_avg_mm"], f["z_max_mm"]))


def imported_front_surface_report(shape, reference_grid: list[list[float]], state: dict) -> dict:
    """Sample the actual re-imported CAD face and compare it to the equation field."""

    front = front_surface_from_shape(shape, state)
    surface = front["surface"]
    u1, u2 = front["u_bounds"]
    v1, v2 = front["v_bounds"]
    samples = 31
    flat_edge = max(0.0, float(state.get("flatEdgeWidth", state.get("flat_edge_width", 10.0)) or 0.0))
    if is_round_footprint(state):
        flat_edge = round_export_border_width(state, len(reference_grid))
    count_values = 0
    sum_abs = 0.0
    sum_sq = 0.0
    max_abs = 0.0
    max_at = (0.0, 0.0)
    flat_count = 0
    flat_max_abs = 0.0
    outside_count = 0

    for iu in range(samples):
        u = u1 + (u2 - u1) * (iu / (samples - 1))
        for iv in range(samples):
            v = v1 + (v2 - v1) * (iv / (samples - 1))
            point = surface.Value(u, v)
            x = float(point.X())
            y = float(point.Y())
            z = float(point.Z())
            if x < -HALF_W - 1e-6 or x > HALF_W + 1e-6 or y < -HALF_H - 1e-6 or y > HALF_H + 1e-6:
                outside_count += 1
                continue
            if is_round_footprint(state) and footprint_inset(x, y, state) < -1e-6:
                outside_count += 1
                continue
            ref_z = bilinear_grid_height(reference_grid, x, y)
            diff = z - ref_z
            ad = abs(diff)
            count_values += 1
            sum_abs += ad
            sum_sq += diff * diff
            if ad > max_abs:
                max_abs = ad
                max_at = (x, y)
            inset = footprint_inset(x, y, state)
            if inset <= flat_edge + 1e-6:
                flat_count += 1
                flat_max_abs = max(flat_max_abs, abs(z))

    return {
        "sampled_face_x_span_mm": front["x_span_mm"],
        "sampled_face_y_span_mm": front["y_span_mm"],
        "sampled_face_z_min_mm": front["z_min_mm"],
        "sampled_face_z_max_mm": front["z_max_mm"],
        "sampled_face_z_avg_mm": front["z_avg_mm"],
        "samples_per_axis": samples,
        "compared_points": count_values,
        "outside_points": outside_count,
        "mean_abs_mm": sum_abs / count_values if count_values else float("inf"),
        "rms_mm": math.sqrt(sum_sq / count_values) if count_values else float("inf"),
        "max_abs_mm": max_abs,
        "max_at_x_mm": max_at[0],
        "max_at_y_mm": max_at[1],
        "flat_border_points": flat_count,
        "flat_border_max_abs_mm": flat_max_abs,
    }


def fabrication_qa_report(
    state: dict,
    grid: list[list[float]],
    fidelity: dict,
    local: dict,
    readback: dict,
    imported_surface: dict,
) -> dict:
    expected_faces = 3 if is_round_footprint(state) else 6
    topology_pass = (
        bool(local.get("valid"))
        and bool(readback.get("valid"))
        and int(readback.get("solids", 0)) == 1
        and int(readback.get("shells", 0)) == 1
        and int(readback.get("faces", 0)) == expected_faces
    )
    equation_pass = (
        imported_surface["rms_mm"] <= QA_TOLERANCES["top_surface_rms_mm"]
        and imported_surface["max_abs_mm"] <= QA_TOLERANCES["top_surface_max_mm"]
    )
    flat_border_pass = imported_surface["flat_border_max_abs_mm"] <= QA_TOLERANCES["flat_border_max_abs_mm"]
    finite_heights_pass = all(math.isfinite(float(z)) for row in grid for z in row)
    status = "PASS" if (topology_pass and equation_pass and flat_border_pass and finite_heights_pass) else "FAIL"
    return {
        "status": status,
        "lensType": state.get("lensType"),
        "equation_defined": state.get("lensType") in VERIFIED_EQUATION_LENSES,
        "checks": {
            "topology": topology_pass,
            "equation_match": equation_pass,
            "flat_border": flat_border_pass,
            "finite_heights": finite_heights_pass,
        },
        "tolerances": dict(QA_TOLERANCES),
        "visualizer_grid_fidelity": fidelity,
        "imported_step_front_surface": imported_surface,
        "local_solid": local,
        "reimported_step": readback,
    }


def write_preview_obj(path: Path, grid: list[list[float]], state: dict) -> None:
    """Tiny preview mesh for local sanity checks only; not for fabrication."""

    n = len(grid)
    verts: list[tuple[float, float, float]] = []
    faces: list[tuple[int, ...]] = []
    if is_round_footprint(state):
        radius = round_footprint_radius(state)
        rings = max(24, n // 2)
        segments = max(96, min(360, n * 4))
        verts.append((0.0, 0.0, bilinear_grid_height(grid, 0.0, 0.0)))
        for ring in range(1, rings + 1):
            r = radius * ring / rings
            for segment in range(segments):
                angle = math.pi * 2.0 * segment / segments
                x = math.cos(angle) * r
                y = math.sin(angle) * r
                verts.append((x, y, bilinear_grid_height(grid, x, y)))
        first_ring = 2
        for segment in range(segments):
            faces.append((1, first_ring + segment, first_ring + ((segment + 1) % segments)))
        for ring in range(1, rings):
            inner = 2 + (ring - 1) * segments
            outer = inner + segments
            for segment in range(segments):
                next_segment = (segment + 1) % segments
                faces.append(
                    (
                        inner + segment,
                        outer + segment,
                        outer + next_segment,
                        inner + next_segment,
                    )
                )
    else:
        for iy in range(n):
            y = -HALF_H + HEIGHT_MM * iy / (n - 1)
            for ix in range(n):
                x = -HALF_W + WIDTH_MM * ix / (n - 1)
                verts.append((x, y, grid[iy][ix]))
        for iy in range(n - 1):
            for ix in range(n - 1):
                a = iy * n + ix + 1
                faces.append((a, a + 1, a + n + 1, a + n))
    with path.open("w", encoding="utf-8") as f:
        f.write("# Preview mesh only. Use the STEP for fabrication.\n")
        for x, y, z in verts:
            f.write(f"v {x:.4f} {y:.4f} {z:.4f}\n")
        for face in faces:
            f.write("f " + " ".join(map(str, face)) + "\n")


def _pdf_text(c: canvas.Canvas, x: float, y: float, text: str, size: float = 8.0, color=colors.HexColor("#414650"), bold: bool = False) -> None:
    c.setFillColor(color)
    c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
    c.drawString(x, y, str(text))


def _pdf_metric(c: canvas.Canvas, x: float, y: float, label: str, value: str, accent=colors.HexColor("#111827")) -> None:
    _pdf_text(c, x, y + 12, label.upper(), 5.8, colors.HexColor("#777d88"), True)
    _pdf_text(c, x, y, value, 10.0, accent, True)


def _pdf_status_pill(c: canvas.Canvas, x: float, y: float, label: str, ok: bool) -> None:
    fill = colors.HexColor("#0aa36e") if ok else colors.HexColor("#c73535")
    c.setFillColor(fill)
    c.roundRect(x, y, 55, 17, 5, fill=1, stroke=0)
    _pdf_text(c, x + 8, y + 5, label, 7.5, colors.white, True)


def _height_color(value: float, z_min: float, z_max: float):
    if z_max <= z_min:
        t = 0.0
    else:
        t = max(0.0, min(1.0, (value - z_min) / (z_max - z_min)))
    # Dark graphite -> cyan -> warm gold. Restrained but readable in print.
    if t < 0.5:
        k = t / 0.5
        r = 0.12 + (0.05 - 0.12) * k
        g = 0.14 + (0.58 - 0.14) * k
        b = 0.18 + (0.68 - 0.18) * k
    else:
        k = (t - 0.5) / 0.5
        r = 0.05 + (0.92 - 0.05) * k
        g = 0.58 + (0.70 - 0.58) * k
        b = 0.68 + (0.25 - 0.68) * k
    return colors.Color(r, g, b)


def _draw_height_map(c: canvas.Canvas, grid: list[list[float]], x: float, y: float, size: float, z_min: float, z_max: float) -> None:
    n = len(grid)
    cells = min(46, n)
    cell = size / cells
    for iy in range(cells):
        gy = int(round(iy * (n - 1) / max(1, cells - 1)))
        for ix in range(cells):
            gx = int(round(ix * (n - 1) / max(1, cells - 1)))
            z = float(grid[gy][gx])
            c.setFillColor(_height_color(z, z_min, z_max))
            c.rect(x + ix * cell, y + (cells - 1 - iy) * cell, cell + 0.2, cell + 0.2, fill=1, stroke=0)
    c.setStrokeColor(colors.HexColor("#2f3640"))
    c.setLineWidth(0.8)
    c.rect(x, y, size, size, fill=0, stroke=1)
    _pdf_text(c, x, y - 12, "Top surface height map", 7.0, colors.HexColor("#555b66"), True)


def _draw_section(c: canvas.Canvas, values: list[float], x: float, y: float, w: float, h: float, label: str, z_min: float, z_max: float) -> None:
    c.setStrokeColor(colors.HexColor("#d0d4da"))
    c.setLineWidth(0.6)
    c.rect(x, y, w, h, fill=0, stroke=1)
    if not values:
        return
    span = max(1e-6, z_max - z_min)
    pts = []
    for i, z in enumerate(values):
        px = x + w * (i / max(1, len(values) - 1))
        py = y + h * ((float(z) - z_min) / span)
        pts.append((px, py))
    c.setStrokeColor(colors.HexColor("#00a7b7"))
    c.setLineWidth(1.4)
    path = c.beginPath()
    path.moveTo(*pts[0])
    for px, py in pts[1:]:
        path.lineTo(px, py)
    c.drawPath(path, stroke=1, fill=0)
    _pdf_text(c, x, y + h + 6, label, 7.0, colors.HexColor("#555b66"), True)
    _pdf_text(c, x + w - 54, y - 10, f"{z_min:.1f}-{z_max:.1f} mm", 6.5, colors.HexColor("#777d88"))


def write_validation_pdf(
    path: Path,
    state: dict,
    grid: list[list[float]],
    qa: dict,
    fidelity: dict,
    imported_surface: dict,
    local: dict,
    readback: dict,
    optical_cleanup: dict,
    corner_cleanup: dict,
) -> None:
    c = canvas.Canvas(str(path), pagesize=landscape(A4))
    page_w, page_h = landscape(A4)
    margin = 34
    z_min = min(min(row) for row in grid)
    z_max = max(max(row) for row in grid)
    n = len(grid)
    center = n // 2
    center_x = grid[center]
    center_y = [grid[i][center] for i in range(n)]
    checks = qa.get("checks", {})
    params = params_for(state)

    c.setFillColor(colors.HexColor("#f7f4ee"))
    c.rect(0, 0, page_w, page_h, fill=1, stroke=0)
    c.setFillColor(colors.HexColor("#111318"))
    c.rect(0, page_h - 58, page_w, 58, fill=1, stroke=0)

    _pdf_text(c, margin, page_h - 30, "PRISMATICA V2", 10, colors.HexColor("#d8d1c4"), True)
    _pdf_text(c, margin, page_h - 47, "Supplier Validation Report - Verified STEP Export", 16, colors.white, True)
    status_ok = qa.get("status") == "PASS"
    c.setFillColor(colors.HexColor("#0aa36e") if status_ok else colors.HexColor("#c73535"))
    c.roundRect(page_w - margin - 92, page_h - 43, 92, 26, 8, fill=1, stroke=0)
    _pdf_text(c, page_w - margin - 70, page_h - 35, str(qa.get("status", "UNKNOWN")), 11, colors.white, True)

    left_x = margin
    top_y = page_h - 88
    _pdf_text(c, left_x, top_y, "File Purpose", 9, colors.HexColor("#111827"), True)
    _pdf_text(c, left_x, top_y - 15, "This package verifies that the STEP solid was generated from Prismatica's saved lens equation,", 8, colors.HexColor("#414650"))
    _pdf_text(c, left_x, top_y - 27, "re-imported, and measured against the source height field before being marked supplier-ready.", 8, colors.HexColor("#414650"))

    metric_y = top_y - 62
    _pdf_metric(c, left_x, metric_y, "Lens preset", str(state.get("lensType")))
    _pdf_metric(c, left_x + 118, metric_y, "Footprint", footprint_description(state))
    _pdf_metric(c, left_x + 226, metric_y, "Base thickness", f"{BASE_THICKNESS_MM:.1f} mm")
    _pdf_metric(c, left_x + 340, metric_y, "Relief range", f"{z_min:.2f} to {z_max:.2f} mm")

    checks_y = metric_y - 52
    _pdf_text(c, left_x, checks_y + 24, "QA Checks", 9, colors.HexColor("#111827"), True)
    _pdf_status_pill(c, left_x, checks_y, "Topology", bool(checks.get("topology")))
    _pdf_status_pill(c, left_x + 64, checks_y, "Equation", bool(checks.get("equation_match")))
    _pdf_status_pill(c, left_x + 128, checks_y, "Border", bool(checks.get("flat_border")))
    _pdf_status_pill(c, left_x + 192, checks_y, "Heights", bool(checks.get("finite_heights")))

    table_x = left_x
    table_y = checks_y - 44
    _pdf_text(c, table_x, table_y + 24, "Measured STEP Re-import", 9, colors.HexColor("#111827"), True)
    rows = [
        ("Imported STEP valid", str(readback.get("valid"))),
        ("Topology", f"{readback.get('solids')} solid / {readback.get('shells')} shell / {readback.get('faces')} faces"),
        ("Equation RMS deviation", f"{imported_surface.get('rms_mm', 0):.3f} mm (limit {QA_TOLERANCES['top_surface_rms_mm']:.3f})"),
        ("Equation max deviation", f"{imported_surface.get('max_abs_mm', 0):.3f} mm (limit {QA_TOLERANCES['top_surface_max_mm']:.3f})"),
        ("Flat border max height", f"{imported_surface.get('flat_border_max_abs_mm', 0):.5f} mm (limit {QA_TOLERANCES['flat_border_max_abs_mm']:.3f})"),
        ("CAD grid fidelity RMS", f"{fidelity.get('rms_mm', 0):.3f} mm"),
    ]
    c.setStrokeColor(colors.HexColor("#d8d3c9"))
    c.setLineWidth(0.5)
    for idx, (label, value) in enumerate(rows):
        yy = table_y - idx * 18
        c.line(table_x, yy - 4, table_x + 410, yy - 4)
        _pdf_text(c, table_x, yy, label, 7.2, colors.HexColor("#68707b"))
        _pdf_text(c, table_x + 170, yy, value, 7.2, colors.HexColor("#111827"), True)

    params_x = left_x
    params_y = table_y - 132
    _pdf_text(c, params_x, params_y + 18, "Source Equation Parameters", 9, colors.HexColor("#111827"), True)
    param_items = [
        ("shapeScale", state.get("shapeScale")),
        ("flatEdgeWidth", f"{state.get('flatEdgeWidth', 10.0)} mm"),
        ("stepExportQuality", state.get("stepExportQuality", "high")),
        ("opticalCleanSurface", state.get("opticalCleanSurface", True)),
        ("smoothing", state.get("smoothing")),
    ]
    param_items.extend((key, value) for key, value in sorted(params.items())[:9])
    for idx, (key, value) in enumerate(param_items[:14]):
        col = idx // 7
        row = idx % 7
        _pdf_text(c, params_x + col * 205, params_y - row * 13, f"{key}: {value}", 6.6, colors.HexColor("#414650"))

    right_x = page_w - margin - 318
    map_size = 168
    _draw_height_map(c, grid, right_x, page_h - 86 - map_size, map_size, z_min, z_max)
    _pdf_text(c, right_x + map_size + 16, page_h - 104, "Surface Range", 8.0, colors.HexColor("#111827"), True)
    _pdf_metric(c, right_x + map_size + 16, page_h - 135, "Minimum", f"{z_min:.2f} mm")
    _pdf_metric(c, right_x + map_size + 16, page_h - 170, "Maximum", f"{z_max:.2f} mm")
    _pdf_metric(c, right_x + map_size + 16, page_h - 205, "Overall depth", f"{BASE_THICKNESS_MM + z_max:.1f} mm")

    chart_x = right_x
    chart_y = 118
    _draw_section(c, center_x, chart_x, chart_y + 82, 300, 58, "Centre horizontal section", z_min, z_max)
    _draw_section(c, center_y, chart_x, chart_y, 300, 58, "Centre vertical section", z_min, z_max)

    notes_y = 58
    _pdf_text(c, margin, notes_y + 22, "Fabrication Notes", 8.5, colors.HexColor("#111827"), True)
    _pdf_text(c, margin, notes_y + 9, "Use the STEP file for CAM. The OBJ is only a visual preview mesh.", 7.0, colors.HexColor("#414650"))
    _pdf_text(c, margin, notes_y - 3, "This report is generated automatically from the same export run as the STEP file.", 7.0, colors.HexColor("#414650"))
    _pdf_text(c, margin, 24, f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} - QA JSON contains the full machine-readable report.", 6.3, colors.HexColor("#777d88"))
    if is_round_footprint(state):
        boundary_note = f"Circular border: {round_export_border_width(state, len(grid)):.1f} mm flat"
    else:
        boundary_note = f"Corner cleanup: {corner_cleanup.get('corner_flat_mm', 0):.1f} mm flat"
    _pdf_text(c, page_w - margin - 205, 24, f"Optical cleanup: {optical_cleanup.get('mode', 'off')} | {boundary_note}", 6.3, colors.HexColor("#777d88"))

    c.showPage()
    c.save()


def main() -> int:
    global OUT_DIR, VERSION_FILE
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-json", help="Path to exported Prismatica visualizer state JSON")
    parser.add_argument(
        "--output-dir",
        help="Optional output directory. Defaults to the app's saved fabricator STEP folder.",
    )
    parser.add_argument(
        "--export-basename",
        help="Optional stable filename base instead of the next PV2-step-vNNN version.",
    )
    args = parser.parse_args()

    if args.output_dir:
        OUT_DIR = Path(args.output_dir).expanduser().resolve()
        VERSION_FILE = OUT_DIR / ".next_step_version"
    if args.export_basename and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.export_basename):
        parser.error("--export-basename may contain only letters, numbers, dots, underscores, and hyphens")

    state = load_state(args.state_json)
    require_verified_equation_lens(state)
    grid = sample_visualizer_grid(state)
    z_min = min(min(row) for row in grid)
    z_max = max(max(row) for row in grid)
    samples = len(grid)
    sample_spacing = max(WIDTH_MM, HEIGHT_MM) / (samples - 1)
    fidelity = visualizer_fidelity_report(state, grid)
    corner_cleanup = LAST_CORNER_CLEANUP_REPORT or {}
    optical_cleanup = LAST_OPTICAL_CLEANUP_REPORT or {"enabled": False, "mode": "unknown"}
    if is_round_footprint(state):
        boundary_cleanup_text = (
            "Circular boundary cleanup:\n"
            "- Purpose: gives the trimmed CAD face a clean planar registration annulus at the circular perimeter.\n"
            "- Square-corner cleanup is not applied to circular footprints.\n"
            f"- Verified planar border width: {round_export_border_width(state, samples):.1f} mm\n\n"
        )
        fidelity_cleanup_label = "the same circular perimeter cleanup"
    else:
        boundary_cleanup_text = (
            "Fabricator corner cleanup:\n"
            "- Purpose: removes the small boundary waviness Rick identified in the four square corners.\n"
            "- The central lens/ripple surface is unchanged; only the corner relief is tapered cleanly to flat.\n"
            f"- Flat corner radius: {corner_cleanup.get('corner_flat_mm', 0.0):.1f} mm\n"
            f"- Corner blend distance: {corner_cleanup.get('corner_blend_mm', 0.0):.1f} mm\n"
            f"- Max corner relief before cleanup: {corner_cleanup.get('max_corner_relief_before_mm', 0.0):.2f} mm\n"
            f"- Max corner relief after cleanup: {corner_cleanup.get('max_corner_relief_after_mm', 0.0):.2f} mm\n\n"
        )
        fidelity_cleanup_label = "the same fabricator corner cleanup"

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    solid = build_solid(grid, state)
    local = {
        "valid": bool(BRepCheck_Analyzer(solid).IsValid()),
        "solids": count(solid, TopAbs_SOLID),
        "shells": count(solid, TopAbs_SHELL),
        "faces": count(solid, TopAbs_FACE),
        "volume_mm3": volume_mm3(solid),
    }

    version = None if args.export_basename else next_export_version()
    names = export_names(args.export_basename) if args.export_basename else versioned_export_names(version)
    step_name = names["step"]
    preview_name = names["preview"]
    readme_name = names["readme"]
    qa_name = names["qa"]
    pdf_name = names["pdf"]
    step_path = OUT_DIR / step_name
    preview_path = OUT_DIR / preview_name
    readme_path = OUT_DIR / readme_name
    qa_path = OUT_DIR / qa_name
    pdf_path = OUT_DIR / pdf_name
    export_step(solid, step_path)
    readback_shape_obj = readback_shape(step_path)
    readback = topology_report(readback_shape_obj)
    imported_surface = imported_front_surface_report(readback_shape_obj, LAST_REFERENCE_GRID or grid, state)
    qa = fabrication_qa_report(state, grid, fidelity, local, readback, imported_surface)
    qa_path.write_text(json.dumps(qa, indent=2, sort_keys=True), encoding="utf-8")
    write_preview_obj(preview_path, grid, state)
    write_validation_pdf(
        pdf_path,
        state,
        grid,
        qa,
        fidelity,
        imported_surface,
        local,
        readback,
        optical_cleanup,
        corner_cleanup,
    )

    readme_path.write_text(
        "Prismatica V2 acrylic lens - verified STEP export\n"
        "=================================================\n\n"
        "This is a fresh CAD rebuild for fabrication. It is not converted from STL.\n"
        "The front surface is generated from the same Prismatica visualizer\n"
        "height function for the selected lens type, using the live exported parameters.\n"
        f"{cad_construction_description(state)}\n\n"
        "File for CAM:\n"
        f"- {step_name}\n\n"
        "Fabricator QA gate:\n"
        f"- Export version: {names['base'] if version is None else f'v{version:03d}'}\n"
        f"- Status: {qa['status']}\n"
        f"- Lens preset is equation-defined: {qa['equation_defined']}\n"
        f"- Topology check: {qa['checks']['topology']}\n"
        f"- Imported STEP vs source equation check: {qa['checks']['equation_match']}\n"
        f"- Flat border check: {qa['checks']['flat_border']}\n"
        f"- Finite height check: {qa['checks']['finite_heights']}\n"
        f"- QA JSON: {qa_name}\n\n"
        f"- Supplier validation PDF: {pdf_name}\n\n"
        "Basic dimensions:\n"
        f"- Finished footprint: {footprint_description(state)}\n"
        f"- Flat rear/base thickness: {BASE_THICKNESS_MM:.1f} mm\n"
        f"- Front surface relief range: {z_min:.2f} to {z_max:.2f} mm\n"
        f"- Approximate overall depth: {BASE_THICKNESS_MM + z_max:.1f} mm\n\n"
        "CAD sampling:\n"
        f"- STEP export quality: {state.get('stepExportQuality', 'standard')}\n"
        f"- CAD surface grid: {samples} x {samples}\n"
        f"- Approximate CAD sample spacing: {sample_spacing:.2f} mm\n"
        f"- Visualizer mesh resolution setting: {state.get('resolution')} mm\n\n"
        "Optical surface cleanup:\n"
        "- Purpose: removes non-intentional local wobble/irregularity from the clear acrylic surface.\n"
        "- This is applied to the stepped-ripple lens as a smooth radial profile before the CAD solid is built.\n"
        f"- Enabled: {optical_cleanup.get('enabled', False)}\n"
        f"- Mode: {optical_cleanup.get('mode', 'unknown')}\n"
        f"- Max local cleanup adjustment: {float(optical_cleanup.get('max_adjustment_mm', 0.0) or 0.0):.3f} mm\n"
        f"- RMS cleanup adjustment: {float(optical_cleanup.get('rms_adjustment_mm', 0.0) or 0.0):.3f} mm\n\n"
        f"{boundary_cleanup_text}"
        "Visualizer fidelity check:\n"
        f"- Method: CAD height grid compared against the high-resolution visualizer field after {fidelity_cleanup_label}.\n"
        f"- Reference field: {fidelity['reference_samples']} x {fidelity['reference_samples']} samples "
        f"({fidelity['reference_spacing_mm']:.2f} mm spacing)\n"
        f"- Compared points: {fidelity['compared_points']}\n"
        f"- Mean absolute deviation: {fidelity['mean_abs_mm']:.3f} mm\n"
        f"- RMS deviation: {fidelity['rms_mm']:.3f} mm\n"
        f"- Maximum local deviation: {fidelity['max_abs_mm']:.3f} mm "
        f"at x={fidelity['max_at_x_mm']:.1f} mm, y={fidelity['max_at_y_mm']:.1f} mm\n\n"
        "Imported STEP equation check:\n"
        "- Method: the exported STEP is re-imported, the front CAD face is sampled, and each sampled point is compared back to the source equation/reference field.\n"
        f"- Compared points: {imported_surface['compared_points']}\n"
        f"- Mean absolute deviation: {imported_surface['mean_abs_mm']:.3f} mm\n"
        f"- RMS deviation: {imported_surface['rms_mm']:.3f} mm "
        f"(tolerance {QA_TOLERANCES['top_surface_rms_mm']:.3f} mm)\n"
        f"- Maximum local deviation: {imported_surface['max_abs_mm']:.3f} mm "
        f"(tolerance {QA_TOLERANCES['top_surface_max_mm']:.3f} mm)\n"
        f"- Flat border max height: {imported_surface['flat_border_max_abs_mm']:.4f} mm "
        f"(tolerance {QA_TOLERANCES['flat_border_max_abs_mm']:.4f} mm)\n\n"
        "Visualizer parameters used:\n"
        f"- lensType: {state.get('lensType')}\n"
        f"- shapeScale: {state.get('shapeScale')}\n"
        f"- mirrorLens: {state.get('mirrorLens')}\n"
        f"- flatEdgeWidth: {state.get('flatEdgeWidth', 10.0)} mm\n"
        f"- stepExportQuality: {state.get('stepExportQuality', 'standard')}\n"
        f"- opticalCleanSurface: {state.get('opticalCleanSurface', True)}\n"
        f"- smoothing passes: {state.get('smoothing')}\n"
        f"- params: {json.dumps(state.get('params', {}), sort_keys=True)}\n\n"
        "Local validation:\n"
        f"- Before export: valid={local['valid']}, solids={local['solids']}, shells={local['shells']}, faces={local['faces']}\n"
        f"- Re-imported STEP: valid={readback['valid']}, solids={readback['solids']}, shells={readback['shells']}, faces={readback['faces']}\n"
        f"- Re-imported volume: {readback['volume_mm3'] / 1000:.1f} cm3\n\n"
        "Notes:\n"
        "- The OBJ file in this folder is only a preview mesh for visual checking.\n"
        "- Please use the STEP file for machining/CAM.\n",
        encoding="utf-8",
    )

    if qa["status"] != "PASS":
        raise RuntimeError(
            "Fabricator QA gate failed. STEP was generated for inspection but should not be sent. "
            f"See {qa_path} and {readme_path} for the failed checks."
        )

    print(step_path)
    print(readme_path)
    print(qa_path)
    print(pdf_path)
    print(preview_path)
    print(f"samples={samples}, spacing={sample_spacing:.3f}mm")
    print(f"fabricator_qa={qa}")
    print(f"optical_cleanup={optical_cleanup}")
    print(f"corner_cleanup={corner_cleanup}")
    print(f"fidelity={fidelity}")
    print(f"local={local}")
    print(f"readback={readback}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
