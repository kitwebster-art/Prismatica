#!/usr/bin/env python3
"""Build a watertight solid STEP for Prismatica V2.

Rick's CAM feedback was clear: smooth surfaces alone are not enough; the file
needs to be a closed, watertight solid. This script samples the existing lens
height field, creates closed cross-section wires, and lofts them into one BRep
solid using OpenCascade. The result should export as MANIFOLD_SOLID_BREP rather
than a mesh-like faceted body or loose untrimmed surfaces.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeWire
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepOffsetAPI import BRepOffsetAPI_ThruSections
from OCP.GeomAPI import GeomAPI_PointsToBSpline
from OCP.IFSelect import IFSelect_RetDone
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
from OCP.TColgp import TColgp_Array1OfPnt
from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer
from OCP.gp import gp_Pnt


ROOT = Path(__file__).resolve().parents[1]
SAVED = ROOT / "exports" / "saved"
SOURCE = SAVED / "prismatica_morph_rings_500x500.step"
OUT_DIR = SAVED / "cad_watertight"

SECTIONS_Y = 61
POINTS_X = 81
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


def infer_square_top_grid(points: list[tuple[float, float, float]]):
    half = len(points) // 2
    n = int(round(math.sqrt(half)))
    if n * n != half:
        raise RuntimeError(f"Could not infer square grid from {len(points)} points")
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


def bspline_edge(points: list[gp_Pnt]):
    arr = TColgp_Array1OfPnt(1, len(points))
    for i, point in enumerate(points, start=1):
        arr.SetValue(i, point)
    curve = GeomAPI_PointsToBSpline(arr, 3, 8).Curve()
    return BRepBuilderAPI_MakeEdge(curve).Edge()


def line_edge(a: gp_Pnt, b: gp_Pnt):
    return BRepBuilderAPI_MakeEdge(a, b).Edge()


def make_section_wire(grid, y: float):
    min_x, max_x = grid[0][0][0], grid[0][-1][0]
    top_points = []
    for j in range(POINTS_X):
        x = min_x + (max_x - min_x) * j / (POINTS_X - 1)
        top_points.append(gp_Pnt(x, y, bilinear(grid, x, y)))

    bottom_right = gp_Pnt(max_x, y, BOTTOM_Z)
    bottom_left = gp_Pnt(min_x, y, BOTTOM_Z)
    wire = BRepBuilderAPI_MakeWire()
    wire.Add(bspline_edge(top_points))
    wire.Add(line_edge(top_points[-1], bottom_right))
    wire.Add(line_edge(bottom_right, bottom_left))
    wire.Add(line_edge(bottom_left, top_points[0]))
    if not wire.IsDone():
        raise RuntimeError(f"Could not build section wire at y={y}")
    return wire.Wire()


def build_solid(grid):
    min_y, max_y = grid[0][0][1], grid[-1][0][1]
    loft = BRepOffsetAPI_ThruSections(True, False, TOLERANCE)
    loft.SetMaxDegree(8)
    for i in range(SECTIONS_Y):
        y = min_y + (max_y - min_y) * i / (SECTIONS_Y - 1)
        loft.AddWire(make_section_wire(grid, y))
    loft.CheckCompatibility(True)
    loft.Build()
    if not loft.IsDone():
        raise RuntimeError("Loft did not complete")
    return loft.Shape()


def count_subshapes(shape, kind) -> int:
    exp = TopExp_Explorer(shape, kind)
    count = 0
    while exp.More():
        count += 1
        exp.Next()
    return count


def export_step(shape, path: Path) -> None:
    writer = STEPControl_Writer()
    writer.Transfer(shape, STEPControl_AsIs)
    status = writer.Write(str(path))
    if status != IFSelect_RetDone:
        raise RuntimeError(f"STEP export failed: {path}")


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    grid = infer_square_top_grid(read_points(SOURCE))
    shape = build_solid(grid)
    valid = BRepCheck_Analyzer(shape).IsValid()
    solid_count = count_subshapes(shape, TopAbs_SOLID)
    face_count = count_subshapes(shape, TopAbs_FACE)

    step_path = OUT_DIR / "prismatica_morph_rings_500x500_watertight_solid.step"
    export_step(shape, step_path)

    peak = max(p[2] for row in grid for p in row)
    readme = OUT_DIR / "README_for_fabricator.txt"
    readme.write_text(
        "Prismatica V2 watertight solid CAD export\n"
        "=========================================\n\n"
        "This package replaces the earlier faceted STEP and loose surface exports.\n"
        "The STEP file was rebuilt as one closed OpenCascade BRep solid by lofting\n"
        "closed cross-section curves through the lens height field.\n\n"
        "File to test:\n"
        "- prismatica_morph_rings_500x500_watertight_solid.step\n\n"
        "Basic dimensions:\n"
        "- Footprint: 500 x 500 mm\n"
        f"- Base thickness: {abs(BOTTOM_Z):.1f} mm\n"
        f"- Approx. peak height above front plane: {peak:.2f} mm\n"
        f"- Loft sections: {SECTIONS_Y}\n"
        f"- Points per top section: {POINTS_X}\n\n"
        "Local validation before sending:\n"
        f"- BRepCheck valid: {valid}\n"
        f"- Solid count: {solid_count}\n"
        f"- Face count: {face_count}\n",
        encoding="utf-8",
    )
    print(step_path)
    print(readme)
    print(f"valid={valid} solids={solid_count} faces={face_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
