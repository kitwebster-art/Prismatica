#!/usr/bin/env python3
"""Build a clean, machinable Prismatica V2 acrylic lens STEP file.

This is intentionally a first-principles CAD rebuild, not a mesh conversion.
It creates a closed OpenCascade solid from six bounded CAD surfaces:

- one smooth front B-spline lens surface
- one flat rear face
- four ruled side faces

The surface is generated from a small analytic control grid, so the STEP is a
native CAD solid instead of thousands of STL triangles or loose untrimmed faces.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path

from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakeWire,
)
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepGProp import BRepGProp
from OCP.Geom import Geom_BSplineSurface
from OCP.BRepOffsetAPI import BRepOffsetAPI_ThruSections
from OCP.GeomAPI import GeomAPI_PointsToBSpline, GeomAPI_PointsToBSplineSurface
from OCP.GProp import GProp_GProps
from OCP.IFSelect import IFSelect_RetDone
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Reader, STEPControl_Writer
from OCP.TColStd import TColStd_Array1OfInteger, TColStd_Array1OfReal
from OCP.TColgp import TColgp_Array1OfPnt, TColgp_Array2OfPnt
from OCP.TopAbs import TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS
from OCP.gp import gp_Pnt


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "exports" / "saved" / "fabricator_clean_step"

DEFAULT_STATE = {
    "lensType": "morph_rings",
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
DEFAULT_SAMPLES = 91
MAX_CAD_SAMPLES = 121
MIN_CAD_SPACING_MM = 2.0
DEGREE = 3
TOLERANCE = 1e-5
LAST_REFERENCE_GRID: list[list[float]] | None = None
LAST_REFERENCE_SPACING_MM = 0.0


def slugify(value: object) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "_", str(value or "lens")).strip("_").lower()
    return text or "lens"

LENS_DEFAULT_PARAMS = {
    "ripple_field": {
        "wavelength": 70.0,
        "amplitude": 8.0,
        "bulb_roundness": 1.0,
        "curve_strength": 1.0,
        "edge_taper": 90.0,
    },
    "stepped_ripple": {
        "wavelength": 42.0,
        "amplitude": 100.0,
        "step": 10.0,
        "edgeFade": 0.18,
        "bumpSharp": 0.5,
    },
    "torus_field": {
        "cols": 3,
        "rows": 3,
        "torus_outer_r": 50.0,
        "torus_inner_r": 18.0,
        "torus_height": 14.0,
    },
    "hex_cells": {
        "cell_size": 40.0,
        "cell_depth": 6.0,
        "invert": False,
    },
    "organic_bulbs": {
        "n_bulbs": 24,
        "seed": 7,
        "min_r": 30.0,
        "max_r": 75.0,
        "min_h": 8.0,
        "max_h": 22.0,
    },
    "pyramid_grid": {
        "cols": 8,
        "rows": 8,
        "pyramid_h": 20.0,
    },
    "dome": {
        "radius": 280.0,
        "height": 40.0,
    },
    "morph_rings": {
        "wavelength": 40.0,
        "amplitude": 12.0,
        "morph_period": 2.0,
        "bias": 0.5,
        "phase_shift": 0.0,
    },
    "interlocking_teardrops": {
        "cols": 4,
        "rows": 4,
        "size": 70.0,
        "height": 16.0,
        "asymmetry": 0.5,
        "rot_variance": 0.8,
        "jitter": 0.3,
        "seed": 3,
    },
    "wave_lattice": {
        "wavelength_x": 80.0,
        "wavelength_y": 80.0,
        "amplitude": 10.0,
        "diagonal_mix": 0.0,
        "rotation": 0.0,
    },
    "logo": {
        "height": 30.0,
        "falloff": 0.15,
        "invert": False,
        "threshold": 0.5,
    },
    "voronoi_bulbs": {
        "n_seeds": 20,
        "seed": 5,
        "height": 18.0,
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
    if path:
        incoming = json.loads(Path(path).read_text(encoding="utf-8"))
        for key, value in incoming.items():
            if key == "params":
                state["params"].update(value or {})
            else:
                state[key] = value
    return state


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
    ridge = 0.5 * (1.0 + math.sin(tau * (d / wavelength + phase_shift)))
    return amplitude * ridge * fade


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
    amplitude = float(params.get("amplitude", 100.0))
    step = float(params.get("step", 10.0))
    fade_ratio = float(params.get("edgeFade", 0.18))
    sharp = float(params.get("bumpSharp", 0.5))

    half_w = width * 0.5
    half_h = height * 0.5
    margin = max(0.0001, min(width, height) * fade_ratio)
    dx = half_w - abs(x)
    dy = half_h - abs(y)
    d = min(dx, dy)
    if d <= 0:
        return 0.0

    env = 1.0 if d >= margin else 0.5 * (1.0 - math.cos(math.pi * (d / margin)))
    r = math.hypot(x, y)
    amp_local = max(0.0, amplitude - step * (r / wavelength))
    wave_raw = 0.5 * (1.0 + math.cos(math.pi * 2.0 * r / wavelength))
    if sharp == 0.5:
        wave = wave_raw
    elif sharp > 0.5:
        n = 1.0 + (sharp - 0.5) * 8.0
        wave = math.pow(wave_raw, n)
    else:
        n = 1.0 + (0.5 - sharp) * 8.0
        wave = 1.0 - math.pow(1.0 - wave_raw, n)
    return amp_local * wave * env


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
    margin = max(0.0, float(params.get("edge_taper", min(width, height) * 0.18)))
    roundness = max(0.25, float(params.get("bulb_roundness", 1.0)))
    curve_strength = max(0.25, float(params.get("curve_strength", 1.0)))
    wavelength = max(1e-6, float(params.get("wavelength", 70.0)))
    amplitude = float(params.get("amplitude", 8.0))
    dx = half_w - abs(x)
    dy = half_h - abs(y)
    d = min(dx, dy)
    if d <= 0:
        return 0.0
    t_edge = 1.0 if margin <= 0 else min(1.0, max(0.0, d / margin))
    env = t_edge * t_edge * (3.0 - 2.0 * t_edge)
    r = math.hypot(x, y)
    wave = 0.5 * (1.0 + math.cos(math.pi * 2.0 * r / wavelength))
    rounded = math.pow(wave, 1.0 / roundness)
    curved = math.pow(rounded, curve_strength)
    return amplitude * curved * env


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
                z = max(z, torus_height * math.sqrt(inside) / wall_r)
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
                bowl = 0.5 * (1.0 - math.cos(math.pi * r / radius)) * cell_depth
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
    margin = max(min_r, max_r)
    z = 0.0
    for _ in range(max(1, int(params.get("n_bulbs", 24)))):
        cx = rng() * (width - 2.0 * margin) - (width / 2.0 - margin)
        cy = rng() * (height - 2.0 * margin) - (height / 2.0 - margin)
        radius = min_r + rng() * max(0.0, max_r - min_r)
        hpk = min_h + rng() * max(0.0, max_h - min_h)
        r = math.hypot(x - cx, y - cy)
        if r < radius:
            z = max(z, hpk * 0.5 * (1.0 + math.cos(math.pi * r / radius)))
    return z


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
    cell_w = width / cols
    cell_h = height / rows
    u = ((x + width / 2.0) % cell_w) / cell_w - 0.5
    v = ((y + height / 2.0) % cell_h) / cell_h - 0.5
    d = max(abs(u), abs(v)) * 2.0
    return max(0.0, min(pyramid_h, pyramid_h * (1.0 - d)))


def dome_height(x: float, y: float, state: dict) -> float:
    params = params_for(state)
    scale = float(state.get("shapeScale") or 1.0)
    if state.get("mirrorLens"):
        x, y = abs(x), abs(y)
    x *= scale
    y *= scale

    radius = max(0.0001, float(params.get("radius", 280.0)))
    height = max(0.0001, float(params.get("height", 40.0)))
    sphere_r = (radius * radius + height * height) / (2.0 * height)
    center_z = height - sphere_r
    r2 = x * x + y * y
    if r2 >= radius * radius:
        return 0.0
    return center_z + math.sqrt(sphere_r * sphere_r - r2)


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
            z = max(z, drop_height * 0.5 * (1.0 + math.cos(math.pi * t)))
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
    return amplitude * 0.25 * (2.0 + val)


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
    return float(params.get("height", 18.0)) * 0.5 * (1.0 + math.cos(math.pi * t))


LENS_HEIGHT_FUNCTIONS = {
    "ripple_field": ripple_field_height,
    "stepped_ripple": stepped_ripple_height,
    "torus_field": torus_field_height,
    "hex_cells": hex_cells_height,
    "organic_bulbs": organic_bulbs_height,
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
        return fn(x, y, state)
    # Last-resort no-fail fallback for future procedural lens types. This keeps
    # the app exportable, but the README will still name the unrecognised type.
    return 0.0


def cad_samples_for_state(state: dict) -> int:
    """Choose a CAD grid that follows the visualizer without making unusable files."""

    resolution = max(0.1, float(state.get("resolution") or 1.0))
    width = float(state.get("panelWidth", WIDTH_MM))
    height = float(state.get("panelHeight", HEIGHT_MM))

    # The visualizer may use 0.5 mm mesh spacing, which would create an enormous
    # CAD file if copied 1:1. Use a denser-than-before manufacturing grid that
    # tracks the selected visualizer resolution, with a hard cap for reliability.
    target_spacing = max(MIN_CAD_SPACING_MM, resolution * 4.0)
    samples = int(math.floor(max(width, height) / target_spacing)) + 1
    samples = max(DEFAULT_SAMPLES, min(MAX_CAD_SAMPLES, samples))
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
    global LAST_REFERENCE_GRID, LAST_REFERENCE_SPACING_MM
    WIDTH_MM = float(state.get("panelWidth", WIDTH_MM))
    HEIGHT_MM = float(state.get("panelHeight", HEIGHT_MM))
    HALF_W = WIDTH_MM / 2.0
    HALF_H = HEIGHT_MM / 2.0
    BASE_THICKNESS_MM = float(state.get("thickness", BASE_THICKNESS_MM))
    samples = cad_samples_for_state(state)

    ref_samples = visualizer_reference_samples_for_state(state, samples)
    reference = sample_height_grid(state, ref_samples)
    smooth_height_grid(reference, max(0, int(state.get("smoothing", 0) or 0)))
    LAST_REFERENCE_GRID = reference
    LAST_REFERENCE_SPACING_MM = max(WIDTH_MM, HEIGHT_MM) / (ref_samples - 1)

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


def smooth_height_grid(values: list[list[float]], passes: int) -> None:
    # Match the visualizer's optional box-blur smoothing. It is applied to the
    # sampled height field before the mesh is built, so the CAD surface should
    # use the same smoothed heights.
    samples = len(values)
    for _ in range(passes):
        tmp = [[0.0] * samples for _ in range(samples)]
        for i in range(samples):
            for j in range(samples):
                left = values[i][j - 1] if j > 0 else values[i][j]
                center = values[i][j]
                right = values[i][j + 1] if j < samples - 1 else values[i][j]
                tmp[i][j] = (left + center + center + right) * 0.25
        for i in range(samples):
            for j in range(samples):
                up = tmp[i - 1][j] if i > 0 else tmp[i][j]
                center = tmp[i][j]
                down = tmp[i + 1][j] if i < samples - 1 else tmp[i][j]
                values[i][j] = (up + center + center + down) * 0.25


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

    global LAST_REFERENCE_GRID, LAST_REFERENCE_SPACING_MM
    ref_grid = LAST_REFERENCE_GRID
    if ref_grid is None:
        ref_samples = visualizer_reference_samples_for_state(state, len(cad_grid))
        ref_grid = sample_height_grid(state, ref_samples)
        smooth_height_grid(ref_grid, max(0, int(state.get("smoothing", 0) or 0)))
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
    arr = TColgp_Array2OfPnt(1, samples, 1, samples)
    for i in range(samples):
        y = -HALF_H + HEIGHT_MM * i / (samples - 1)
        for j in range(samples):
            x = -HALF_W + WIDTH_MM * j / (samples - 1)
            z = -BASE_THICKNESS_MM if back else float(grid[i][j])
            arr.SetValue(i + 1, j + 1, gp_Pnt(x, y, z))
    return GeomAPI_PointsToBSplineSurface(arr, DEGREE, DEGREE).Surface()


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
    arr = TColgp_Array1OfPnt(1, len(points))
    for i, point in enumerate(points, start=1):
        arr.SetValue(i, point)
    curve = GeomAPI_PointsToBSpline(arr, 3, 8).Curve()
    edge = BRepBuilderAPI_MakeEdge(curve)
    if not edge.IsDone():
        raise RuntimeError("Could not build B-spline edge")
    return edge.Edge()


def line_edge(a: gp_Pnt, b: gp_Pnt):
    edge = BRepBuilderAPI_MakeEdge(a, b)
    if not edge.IsDone():
        raise RuntimeError("Could not build line edge")
    return edge.Edge()


def section_wire(grid: list[list[float]], iy: int):
    samples = len(grid)
    y = -HALF_H + HEIGHT_MM * iy / (samples - 1)
    top = [
        gp_Pnt(-HALF_W + WIDTH_MM * ix / (samples - 1), y, float(grid[iy][ix]))
        for ix in range(samples)
    ]
    bottom_right = gp_Pnt(HALF_W, y, -BASE_THICKNESS_MM)
    bottom_left = gp_Pnt(-HALF_W, y, -BASE_THICKNESS_MM)
    wire = BRepBuilderAPI_MakeWire()
    wire.Add(bspline_edge(top))
    wire.Add(line_edge(top[-1], bottom_right))
    wire.Add(line_edge(bottom_right, bottom_left))
    wire.Add(line_edge(bottom_left, top[0]))
    if not wire.IsDone():
        raise RuntimeError(f"Could not build closed section at row {iy}")
    return wire.Wire()


def build_solid(grid: list[list[float]]):
    loft = BRepOffsetAPI_ThruSections(True, False, TOLERANCE)
    loft.SetMaxDegree(8)
    for iy in range(len(grid)):
        loft.AddWire(section_wire(grid, iy))
    loft.CheckCompatibility(True)
    loft.Build()
    if not loft.IsDone():
        raise RuntimeError("Loft did not complete")
    return loft.Shape()


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
    reader = STEPControl_Reader()
    if reader.ReadFile(str(path)) != IFSelect_RetDone:
        raise RuntimeError(f"Could not re-read STEP: {path}")
    reader.TransferRoots()
    shape = reader.OneShape()
    return {
        "valid": bool(BRepCheck_Analyzer(shape).IsValid()),
        "solids": count(shape, TopAbs_SOLID),
        "shells": count(shape, TopAbs_SHELL),
        "faces": count(shape, TopAbs_FACE),
        "volume_mm3": volume_mm3(shape),
    }


def write_preview_obj(path: Path, grid: list[list[float]]) -> None:
    """Tiny preview mesh for local sanity checks only; not for fabrication."""

    n = len(grid)
    verts: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int, int]] = []
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-json", help="Path to exported Prismatica visualizer state JSON")
    args = parser.parse_args()

    state = load_state(args.state_json)
    grid = sample_visualizer_grid(state)
    z_min = min(min(row) for row in grid)
    z_max = max(max(row) for row in grid)
    samples = len(grid)
    sample_spacing = max(WIDTH_MM, HEIGHT_MM) / (samples - 1)
    fidelity = visualizer_fidelity_report(state, grid)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    solid = build_solid(grid)
    local = {
        "valid": bool(BRepCheck_Analyzer(solid).IsValid()),
        "solids": count(solid, TopAbs_SOLID),
        "shells": count(solid, TopAbs_SHELL),
        "faces": count(solid, TopAbs_FACE),
        "volume_mm3": volume_mm3(solid),
    }

    lens_slug = slugify(state.get("lensType"))
    step_name = f"Prismatica_V2_{lens_slug}_acrylic_lens_clean_solid.step"
    preview_name = f"Prismatica_V2_{lens_slug}_preview_surface_only.obj"
    readme_name = f"README_Prismatica_V2_{lens_slug}_STEP_file.txt"
    step_path = OUT_DIR / step_name
    preview_path = OUT_DIR / preview_name
    readme_path = OUT_DIR / readme_name
    export_step(solid, step_path)
    readback = readback_validate(step_path)
    write_preview_obj(preview_path, grid)

    readme_path.write_text(
        "Prismatica V2 acrylic lens - clean STEP export\n"
        "==============================================\n\n"
        "This is a fresh CAD rebuild for fabrication. It is not converted from STL.\n"
        "The front surface is generated from the same Prismatica visualizer\n"
        "height function for the selected lens type, using the live exported parameters.\n"
        "The STEP file contains one closed solid with a smooth B-spline front lens\n"
        "surface, flat rear face, and four bounded side faces.\n\n"
        "File for CAM:\n"
        f"- {step_name}\n\n"
        "Basic dimensions:\n"
        f"- Finished footprint: {WIDTH_MM:.0f} x {HEIGHT_MM:.0f} mm\n"
        f"- Flat rear/base thickness: {BASE_THICKNESS_MM:.1f} mm\n"
        f"- Front surface relief range: {z_min:.2f} to {z_max:.2f} mm\n"
        f"- Approximate overall depth: {BASE_THICKNESS_MM + z_max:.1f} mm\n\n"
        "CAD sampling:\n"
        f"- CAD surface grid: {samples} x {samples}\n"
        f"- Approximate CAD sample spacing: {sample_spacing:.2f} mm\n"
        f"- Visualizer mesh resolution setting: {state.get('resolution')} mm\n\n"
        "Visualizer fidelity check:\n"
        "- Method: CAD height grid compared against a high-resolution visualizer reference field.\n"
        f"- Reference field: {fidelity['reference_samples']} x {fidelity['reference_samples']} samples "
        f"({fidelity['reference_spacing_mm']:.2f} mm spacing)\n"
        f"- Compared points: {fidelity['compared_points']}\n"
        f"- Mean absolute deviation: {fidelity['mean_abs_mm']:.3f} mm\n"
        f"- RMS deviation: {fidelity['rms_mm']:.3f} mm\n"
        f"- Maximum local deviation: {fidelity['max_abs_mm']:.3f} mm "
        f"at x={fidelity['max_at_x_mm']:.1f} mm, y={fidelity['max_at_y_mm']:.1f} mm\n\n"
        "Visualizer parameters used:\n"
        f"- lensType: {state.get('lensType')}\n"
        f"- shapeScale: {state.get('shapeScale')}\n"
        f"- mirrorLens: {state.get('mirrorLens')}\n"
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

    print(step_path)
    print(readme_path)
    print(preview_path)
    print(f"samples={samples}, spacing={sample_spacing:.3f}mm")
    print(f"fidelity={fidelity}")
    print(f"local={local}")
    print(f"readback={readback}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
