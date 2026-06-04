#!/usr/bin/env python3
"""Build a smooth native CAD/NURBS remodel for the Prismatica lens.

This fits a bounded tensor-product B-spline surface to the Prismatica height
field, then closes it with a flat back and four ruled sides. Unlike the
triangular BRep fallback, the visible lens surface is one smooth CAD surface.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import numpy as np
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeSolid, BRepBuilderAPI_Sewing
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.Geom import Geom_BSplineSurface
from OCP.IFSelect import IFSelect_RetDone
from OCP.IGESControl import IGESControl_Controller, IGESControl_Writer
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
from OCP.TColStd import TColStd_Array1OfInteger, TColStd_Array1OfReal
from OCP.TColgp import TColgp_Array2OfPnt
from OCP.TopoDS import TopoDS
from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer
from OCP.gp import gp_Pnt


ROOT = Path(__file__).resolve().parents[1]
SAVED = ROOT / "exports" / "saved"
SOURCE = SAVED / "prismatica_morph_rings_500x500.step"
OUT_DIR = SAVED / "cad_smooth_nurbs"

DEGREE = 3
CTRL = 45
SAMPLES = 121
BOTTOM_Z = -20.0
TOLERANCE = 1e-4

POINT_RE = re.compile(
    r"CARTESIAN_POINT\('',\(([-+0-9.Ee]+),([-+0-9.Ee]+),([-+0-9.Ee]+)\)\)"
)


def read_points(path: Path) -> list[tuple[float, float, float]]:
    points: list[tuple[float, float, float]] = []
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            match = POINT_RE.search(line)
            if match:
                points.append(tuple(float(match.group(i)) for i in range(1, 4)))
    if not points:
        raise RuntimeError(f"No CARTESIAN_POINT entries found in {path}")
    return points


def infer_interleaved_top_grid(points: list[tuple[float, float, float]]):
    n = int(round(math.sqrt(len(points) / 2)))
    if 2 * n * n != len(points):
        raise RuntimeError(f"Could not infer interleaved square grid from {len(points)} points")
    return [points[i * 2 * n : i * 2 * n + n] for i in range(n)]


def bilinear(grid, x: float, y: float) -> float:
    n = len(grid)
    min_x, max_x = grid[0][0][0], grid[0][-1][0]
    min_y, max_y = grid[0][0][1], grid[-1][0][1]
    fx = (x - min_x) / (max_x - min_x) * (n - 1)
    fy = (y - min_y) / (max_y - min_y) * (n - 1)
    ix = max(0, min(n - 2, int(math.floor(fx))))
    iy = max(0, min(n - 2, int(math.floor(fy))))
    tx = fx - ix
    ty = fy - iy
    z00 = grid[iy][ix][2]
    z10 = grid[iy][ix + 1][2]
    z01 = grid[iy + 1][ix][2]
    z11 = grid[iy + 1][ix + 1][2]
    return (z00 * (1 - tx) + z10 * tx) * (1 - ty) + (z01 * (1 - tx) + z11 * tx) * ty


def full_open_uniform_knots(n_ctrl: int, degree: int) -> np.ndarray:
    interior = n_ctrl - degree - 1
    if interior <= 0:
        return np.array([0.0] * (degree + 1) + [1.0] * (degree + 1))
    values = [0.0] * (degree + 1)
    values += [i / (interior + 1) for i in range(1, interior + 1)]
    values += [1.0] * (degree + 1)
    return np.array(values, dtype=float)


def unique_knots_and_mults(n_ctrl: int, degree: int):
    full = full_open_uniform_knots(n_ctrl, degree)
    unique = []
    mults = []
    for value in full:
        if unique and abs(unique[-1] - value) < 1e-12:
            mults[-1] += 1
        else:
            unique.append(float(value))
            mults.append(1)
    k = TColStd_Array1OfReal(1, len(unique))
    m = TColStd_Array1OfInteger(1, len(unique))
    for i, value in enumerate(unique, start=1):
        k.SetValue(i, value)
        m.SetValue(i, mults[i - 1])
    return k, m


def basis_matrix(params: np.ndarray, n_ctrl: int, degree: int) -> np.ndarray:
    knots = full_open_uniform_knots(n_ctrl, degree)
    basis = np.zeros((len(params), n_ctrl), dtype=float)

    def basis_one(i: int, p: int, u: float) -> float:
        if p == 0:
            if (knots[i] <= u < knots[i + 1]) or (u == 1.0 and knots[i] <= u <= knots[i + 1] and i == n_ctrl - 1):
                return 1.0
            return 0.0
        left = 0.0
        denom = knots[i + p] - knots[i]
        if denom:
            left = (u - knots[i]) / denom * basis_one(i, p - 1, u)
        right = 0.0
        denom = knots[i + p + 1] - knots[i + 1]
        if denom:
            right = (knots[i + p + 1] - u) / denom * basis_one(i + 1, p - 1, u)
        return left + right

    for r, u in enumerate(params):
        for i in range(n_ctrl):
            basis[r, i] = basis_one(i, degree, float(u))
    return basis


def fit_z_control(source_grid):
    xs = np.linspace(-250.0, 250.0, SAMPLES)
    ys = np.linspace(-250.0, 250.0, SAMPLES)
    z = np.array([[bilinear(source_grid, x, y) for x in xs] for y in ys], dtype=float)
    params = np.linspace(0.0, 1.0, SAMPLES)
    b = basis_matrix(params, CTRL, DEGREE)

    # Weighted least-squares fit: z ~= Bv @ C @ Bu.T.
    # The edge of the acrylic should return to zero height. Pinning the border
    # strongly prevents the fitted B-spline from creating boundary waves.
    rows = []
    targets = []
    weights = []
    for iy in range(SAMPLES):
        for ix in range(SAMPLES):
            rows.append(np.outer(b[iy], b[ix]).reshape(-1))
            targets.append(z[iy, ix])
            edge_dist = min(ix, iy, SAMPLES - 1 - ix, SAMPLES - 1 - iy)
            weights.append(120.0 if edge_dist < 5 else 1.0)

    # Light Tikhonov damping keeps the fit from using extreme control poles.
    a = np.vstack(rows)
    y = np.array(targets)
    w = np.sqrt(np.array(weights))
    aw = a * w[:, None]
    yw = y * w
    damping = 0.01
    aw = np.vstack([aw, damping * np.eye(CTRL * CTRL)])
    yw = np.concatenate([yw, np.zeros(CTRL * CTRL)])
    coeffs, *_ = np.linalg.lstsq(aw, yw, rcond=None)
    c = coeffs.reshape((CTRL, CTRL))

    fitted = b @ c @ b.T
    err = fitted - z
    return c, {
        "source_min": float(z.min()),
        "source_max": float(z.max()),
        "fit_min": float(fitted.min()),
        "fit_max": float(fitted.max()),
        "rms_error": float(np.sqrt(np.mean(err * err))),
        "max_abs_error": float(np.max(np.abs(err))),
    }


def surface_from_control(z_ctrl: np.ndarray, bottom: bool = False):
    arr = TColgp_Array2OfPnt(1, CTRL, 1, CTRL)
    for i in range(CTRL):
        y = -250.0 + 500.0 * i / (CTRL - 1)
        for j in range(CTRL):
            x = -250.0 + 500.0 * j / (CTRL - 1)
            z = BOTTOM_Z if bottom else float(z_ctrl[i, j])
            arr.SetValue(i + 1, j + 1, gp_Pnt(x, y, z))
    uk, um = unique_knots_and_mults(CTRL, DEGREE)
    vk, vm = unique_knots_and_mults(CTRL, DEGREE)
    surf = Geom_BSplineSurface(arr, uk, vk, um, vm, DEGREE, DEGREE)
    u1, u2, v1, v2 = surf.Bounds()
    return BRepBuilderAPI_MakeFace(surf, u1, u2, v1, v2, TOLERANCE).Face()


def side_surface(z_ctrl: np.ndarray, side: str):
    rows = []
    if side == "south":
        top = [(x, -250.0, float(z_ctrl[0, j])) for j, x in enumerate(np.linspace(-250.0, 250.0, CTRL))]
        bottom = [(x, -250.0, BOTTOM_Z) for x in np.linspace(-250.0, 250.0, CTRL)]
    elif side == "north":
        top = [(x, 250.0, float(z_ctrl[-1, j])) for j, x in enumerate(np.linspace(-250.0, 250.0, CTRL))]
        bottom = [(x, 250.0, BOTTOM_Z) for x in np.linspace(-250.0, 250.0, CTRL)]
    elif side == "west":
        top = [(-250.0, y, float(z_ctrl[i, 0])) for i, y in enumerate(np.linspace(-250.0, 250.0, CTRL))]
        bottom = [(-250.0, y, BOTTOM_Z) for y in np.linspace(-250.0, 250.0, CTRL)]
    elif side == "east":
        top = [(250.0, y, float(z_ctrl[i, -1])) for i, y in enumerate(np.linspace(-250.0, 250.0, CTRL))]
        bottom = [(250.0, y, BOTTOM_Z) for y in np.linspace(-250.0, 250.0, CTRL)]
    else:
        raise ValueError(side)
    rows = [bottom, top]
    arr = TColgp_Array2OfPnt(1, 2, 1, CTRL)
    for i, row in enumerate(rows, start=1):
        for j, (x, y, z) in enumerate(row, start=1):
            arr.SetValue(i, j, gp_Pnt(x, y, z))
    uk, um = unique_knots_and_mults(2, 1)
    vk, vm = unique_knots_and_mults(CTRL, DEGREE)
    surf = Geom_BSplineSurface(arr, uk, vk, um, vm, 1, DEGREE)
    u1, u2, v1, v2 = surf.Bounds()
    return BRepBuilderAPI_MakeFace(surf, u1, u2, v1, v2, TOLERANCE).Face()


def count(shape, kind) -> int:
    exp = TopExp_Explorer(shape, kind)
    total = 0
    while exp.More():
        total += 1
        exp.Next()
    return total


def build_solid(z_ctrl: np.ndarray):
    sewing = BRepBuilderAPI_Sewing(TOLERANCE)
    for face in [
        surface_from_control(z_ctrl, bottom=False),
        surface_from_control(z_ctrl, bottom=True),
        side_surface(z_ctrl, "south"),
        side_surface(z_ctrl, "north"),
        side_surface(z_ctrl, "west"),
        side_surface(z_ctrl, "east"),
    ]:
        sewing.Add(face)
    sewing.Perform()
    shell = TopoDS.Shell_s(sewing.SewedShape())
    return BRepBuilderAPI_MakeSolid(shell).Solid()


def export_step(shape, path: Path) -> None:
    writer = STEPControl_Writer()
    writer.Transfer(shape, STEPControl_AsIs)
    if writer.Write(str(path)) != IFSelect_RetDone:
        raise RuntimeError(f"STEP export failed: {path}")


def export_iges(shape, path: Path) -> None:
    IGESControl_Controller.Init_s()
    writer = IGESControl_Writer()
    writer.AddShape(shape)
    if writer.Write(str(path)) != IFSelect_RetDone:
        raise RuntimeError(f"IGES export failed: {path}")


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    source_grid = infer_interleaved_top_grid(read_points(SOURCE))
    z_ctrl, stats = fit_z_control(source_grid)
    shape = build_solid(z_ctrl)
    valid = BRepCheck_Analyzer(shape).IsValid()
    solid_count = count(shape, TopAbs_SOLID)
    face_count = count(shape, TopAbs_FACE)

    step_path = OUT_DIR / "prismatica_morph_rings_500x500_smooth_nurbs_solid.step"
    iges_path = OUT_DIR / "prismatica_morph_rings_500x500_smooth_nurbs_solid.igs"
    export_step(shape, step_path)
    export_iges(shape, iges_path)

    readme = OUT_DIR / "README_for_fabricator.txt"
    readme.write_text(
        "Prismatica V2 smooth NURBS/native CAD remodel\n"
        "=============================================\n\n"
        "This package contains a smooth B-spline/NURBS-style CAD rebuild of the\n"
        "Prismatica lens. The visible front face is one fitted B-spline surface,\n"
        "closed with a flat back and four ruled side faces to form a solid.\n\n"
        "Files:\n"
        "- prismatica_morph_rings_500x500_smooth_nurbs_solid.step\n"
        "- prismatica_morph_rings_500x500_smooth_nurbs_solid.igs\n\n"
        "Basic dimensions:\n"
        "- Footprint: 500 x 500 mm\n"
        f"- Base thickness: {abs(BOTTOM_Z):.1f} mm\n"
        f"- Source top surface range: {stats['source_min']:.2f} to {stats['source_max']:.2f} mm\n"
        f"- Fitted top surface range: {stats['fit_min']:.2f} to {stats['fit_max']:.2f} mm\n"
        f"- Fit RMS error: {stats['rms_error']:.3f} mm\n"
        f"- Fit max absolute error: {stats['max_abs_error']:.3f} mm\n"
        f"- B-spline control grid: {CTRL} x {CTRL}, degree {DEGREE}\n\n"
        "Local validation:\n"
        f"- BRepCheck valid before export: {valid}\n"
        f"- Solid count before export: {solid_count}\n"
        f"- Face count before export: {face_count}\n",
        encoding="utf-8",
    )
    print(step_path)
    print(iges_path)
    print(readme)
    print(f"valid={valid} solids={solid_count} faces={face_count} rms={stats['rms_error']:.3f} max={stats['max_abs_error']:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
