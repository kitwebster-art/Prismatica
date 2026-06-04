#!/usr/bin/env python3
"""Build a bounded watertight BRep solid for Prismatica V2.

This is the conservative fallback after spline/loft exports produced runaway
geometry in third-party viewers. It samples the known-good lens height field and
constructs a closed OpenCascade BRep from bounded quad faces. The result is not
the final ideal NURBS machining model, but it is visually faithful, watertight,
and deliberately avoids unbounded fitted surfaces.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon, BRepBuilderAPI_MakeSolid, BRepBuilderAPI_Sewing
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.IFSelect import IFSelect_RetDone
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
from OCP.TopoDS import TopoDS
from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer
from OCP.gp import gp_Pnt


ROOT = Path(__file__).resolve().parents[1]
SAVED = ROOT / "exports" / "saved"
SOURCE = SAVED / "prismatica_morph_rings_500x500.step"
OUT_DIR = SAVED / "cad_bounded_brep"

GRID = 81
BOTTOM_Z = -20.0
TOLERANCE = 1e-4

POINT_RE = re.compile(
    r"CARTESIAN_POINT\('',\(([-+0-9.Ee]+),([-+0-9.Ee]+),([-+0-9.Ee]+)\)\)"
)


def read_points(path: Path) -> list[tuple[float, float, float]]:
    points: list[tuple[float, float, float]] = []
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if "CARTESIAN_POINT" not in line:
                continue
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


def sample_grid(source_grid):
    min_x, max_x = source_grid[0][0][0], source_grid[0][-1][0]
    min_y, max_y = source_grid[0][0][1], source_grid[-1][0][1]
    top = []
    bottom = []
    for i in range(GRID):
        y = min_y + (max_y - min_y) * i / (GRID - 1)
        row_top = []
        row_bottom = []
        for j in range(GRID):
            x = min_x + (max_x - min_x) * j / (GRID - 1)
            row_top.append(gp_Pnt(x, y, bilinear(source_grid, x, y)))
            row_bottom.append(gp_Pnt(x, y, BOTTOM_Z))
        top.append(row_top)
        bottom.append(row_bottom)
    return top, bottom


def quad_face(a: gp_Pnt, b: gp_Pnt, c: gp_Pnt, d: gp_Pnt):
    poly = BRepBuilderAPI_MakePolygon()
    for p in (a, b, c, d):
        poly.Add(p)
    poly.Close()
    wire = poly.Wire()
    face = BRepBuilderAPI_MakeFace(wire).Face()
    return face


def count(shape, kind) -> int:
    exp = TopExp_Explorer(shape, kind)
    total = 0
    while exp.More():
        total += 1
        exp.Next()
    return total


def build_solid(top, bottom):
    sewing = BRepBuilderAPI_Sewing(TOLERANCE)
    n = len(top)
    for i in range(n - 1):
        for j in range(n - 1):
            sewing.Add(quad_face(top[i][j], top[i][j + 1], top[i + 1][j + 1], top[i + 1][j]))
            sewing.Add(quad_face(bottom[i][j], bottom[i + 1][j], bottom[i + 1][j + 1], bottom[i][j + 1]))

    for j in range(n - 1):
        sewing.Add(quad_face(bottom[0][j], bottom[0][j + 1], top[0][j + 1], top[0][j]))
        sewing.Add(quad_face(bottom[-1][j], top[-1][j], top[-1][j + 1], bottom[-1][j + 1]))
    for i in range(n - 1):
        sewing.Add(quad_face(bottom[i][0], top[i][0], top[i + 1][0], bottom[i + 1][0]))
        sewing.Add(quad_face(bottom[i][-1], bottom[i + 1][-1], top[i + 1][-1], top[i][-1]))

    sewing.Perform()
    shell = TopoDS.Shell_s(sewing.SewedShape())
    return BRepBuilderAPI_MakeSolid(shell).Solid()


def export_step(shape, path: Path) -> None:
    writer = STEPControl_Writer()
    writer.Transfer(shape, STEPControl_AsIs)
    status = writer.Write(str(path))
    if status != IFSelect_RetDone:
        raise RuntimeError(f"STEP export failed: {path}")


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    source_grid = infer_interleaved_top_grid(read_points(SOURCE))
    top, bottom = sample_grid(source_grid)
    shape = build_solid(top, bottom)
    valid = BRepCheck_Analyzer(shape).IsValid()
    solid_count = count(shape, TopAbs_SOLID)
    face_count = count(shape, TopAbs_FACE)
    step_path = OUT_DIR / "prismatica_morph_rings_500x500_bounded_brep_solid.step"
    export_step(shape, step_path)
    peak = max(p.Z() for row in top for p in row)
    min_z = min(p.Z() for row in top for p in row)
    readme = OUT_DIR / "README_for_fabricator.txt"
    readme.write_text(
        "Prismatica V2 bounded watertight BRep export\n"
        "============================================\n\n"
        "This file is a conservative watertight BRep solid built from bounded quad\n"
        "faces sampled from the Prismatica lens heightfield. It was made after\n"
        "spline/loft exports produced bad runaway geometry in external viewers.\n\n"
        "Important: this is a stable visual/CAM test model, not the final ideal\n"
        "smooth NURBS machining remodel. Please use it to confirm that the overall\n"
        "lens shape and watertight solid structure come through correctly.\n\n"
        "Basic dimensions:\n"
        "- Footprint: 500 x 500 mm\n"
        f"- Base thickness: {abs(BOTTOM_Z):.1f} mm\n"
        f"- Top surface range: {min_z:.2f} to {peak:.2f} mm\n"
        f"- Sample grid: {GRID} x {GRID}\n\n"
        "Local validation:\n"
        f"- BRepCheck valid: {valid}\n"
        f"- Solid count: {solid_count}\n"
        f"- Face count: {face_count}\n",
        encoding="utf-8",
    )
    print(step_path)
    print(readme)
    print(f"valid={valid} solids={solid_count} faces={face_count} peak={peak:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
