#!/usr/bin/env python3
"""Build a smooth CAD export from a Prismatica faceted STEP sample.

This intentionally does not convert triangles into STEP faces. It reads the
top height samples from the previous mesh export, resamples them to a compact
regular grid, and asks OpenCascade to fit B-spline surfaces for the top,
bottom, and four side faces. The output is a true surface/solid CAD file for
fabricator review.
"""

from __future__ import annotations

import math
import re
import sys
from pathlib import Path

import cadquery as cq
from OCP.BRep import BRep_Builder
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeSolid, BRepBuilderAPI_Sewing
from OCP.GeomAPI import GeomAPI_PointsToBSplineSurface
from OCP.IFSelect import IFSelect_RetDone
from OCP.IGESControl import IGESControl_Controller, IGESControl_Writer
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
from OCP.TColgp import TColgp_Array2OfPnt
from OCP.TopoDS import TopoDS_Compound, TopoDS_Shell
from OCP.gp import gp_Pnt


ROOT = Path(__file__).resolve().parents[1]
SAVED = ROOT / "exports" / "saved"
SOURCE = SAVED / "prismatica_morph_rings_500x500.step"
OUT_DIR = SAVED / "cad_native"

# A 51 x 51 B-spline grid is a good practical handoff: smooth CAD surfaces,
# small files, and enough detail for quoting the ripple field. Increase this if
# the fabricator asks for a closer approximation after opening the file.
FIT_GRID = 51
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


def sampled_top_grid(source_grid, samples: int):
    min_x, max_x = source_grid[0][0][0], source_grid[0][-1][0]
    min_y, max_y = source_grid[0][0][1], source_grid[-1][0][1]
    rows = []
    for i in range(samples):
        y = min_y + (max_y - min_y) * i / (samples - 1)
        row = []
        for j in range(samples):
            x = min_x + (max_x - min_x) * j / (samples - 1)
            row.append((x, y, bilinear(source_grid, x, y)))
        rows.append(row)
    return rows


def surface_from_rows(rows, degree_u: int = 3, degree_v: int = 3):
    nv = len(rows)
    nu = len(rows[0])
    arr = TColgp_Array2OfPnt(1, nv, 1, nu)
    for i, row in enumerate(rows, start=1):
        for j, (x, y, z) in enumerate(row, start=1):
            arr.SetValue(i, j, gp_Pnt(x, y, z))
    surf = GeomAPI_PointsToBSplineSurface(arr, degree_u, degree_v).Surface()
    return BRepBuilderAPI_MakeFace(surf, TOLERANCE).Face()


def transpose(rows):
    return [list(col) for col in zip(*rows)]


def make_shell(rows):
    bottom = [[(x, y, BOTTOM_Z) for x, y, _ in row] for row in rows]
    faces = [
        surface_from_rows(rows),
        surface_from_rows(bottom),
        surface_from_rows([bottom[0], rows[0]], 1, 3),
        surface_from_rows([bottom[-1], rows[-1]], 1, 3),
        surface_from_rows([transpose(bottom)[0], transpose(rows)[0]], 1, 3),
        surface_from_rows([transpose(bottom)[-1], transpose(rows)[-1]], 1, 3),
    ]
    sewing = BRepBuilderAPI_Sewing(TOLERANCE)
    for face in faces:
        sewing.Add(face)
    sewing.Perform()
    return sewing.SewedShape()


def export_step(shape, path: Path) -> None:
    writer = STEPControl_Writer()
    writer.Transfer(shape, STEPControl_AsIs)
    status = writer.Write(str(path))
    if status != IFSelect_RetDone:
        raise RuntimeError(f"STEP export failed: {path}")


def export_iges(shape, path: Path) -> None:
    IGESControl_Controller.Init_s()
    writer = IGESControl_Writer()
    writer.AddShape(shape)
    status = writer.Write(str(path))
    if status != IFSelect_RetDone:
        raise RuntimeError(f"IGES export failed: {path}")


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    points = read_points(SOURCE)
    source_grid = infer_square_top_grid(points)
    rows = sampled_top_grid(source_grid, FIT_GRID)
    shape = make_shell(rows)

    step_path = OUT_DIR / f"prismatica_morph_rings_500x500_smooth_bspline_{FIT_GRID}.step"
    iges_path = OUT_DIR / f"prismatica_morph_rings_500x500_smooth_bspline_{FIT_GRID}.igs"
    export_step(shape, step_path)
    export_iges(shape, iges_path)

    peak = max(z for row in rows for _, _, z in row)
    note = OUT_DIR / "README_for_fabricator.txt"
    note.write_text(
        "Prismatica V2 lens CAD export\n"
        "================================\n\n"
        "These files were rebuilt from the exported Prismatica lens height field as "
        "OpenCascade B-spline surfaces. They are intended to avoid the previous "
        "faceted mesh-style STEP file.\n\n"
        f"Footprint: 500 x 500 mm\n"
        f"Base thickness: {abs(BOTTOM_Z):.1f} mm\n"
        f"Approx. peak height above front plane: {peak:.2f} mm\n"
        f"B-spline fit grid: {FIT_GRID} x {FIT_GRID}\n\n"
        "Recommended file to test first: the IGES (.igs), because it carries the "
        "smooth surface geometry plainly. The STEP file is included as an "
        "alternative if preferred by the CAM workflow.\n",
        encoding="utf-8",
    )
    print(step_path)
    print(iges_path)
    print(note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
