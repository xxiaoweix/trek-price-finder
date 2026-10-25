from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from ...core.geography import MILES_PER_LATITUDE_DEGREE, conus_grid_points
from .models import LocallyWidgetPoint, ScanProfile


@dataclass(frozen=True)
class GridCell:
    cell_id: str
    label: str
    lat: float
    lng: float
    distance_diag: int
    depth: int = 0
    parent_cell_id: str | None = None

    def as_widget_point(self) -> LocallyWidgetPoint:
        return LocallyWidgetPoint(
            label=self.label,
            lat=self.lat,
            lng=self.lng,
            distance_diag=self.distance_diag,
        )


def _cell_id(
    profile: str,
    lat: float,
    lng: float,
    distance_diag: int,
    depth: int,
) -> str:
    raw = f"{profile}|{lat:.6f}|{lng:.6f}|{distance_diag}|{depth}".encode()
    return hashlib.sha256(raw).hexdigest()[:20]


def _point_cell(profile: ScanProfile, point: LocallyWidgetPoint) -> GridCell:
    return GridCell(
        cell_id=_cell_id(profile.name, point.lat, point.lng, point.distance_diag, 0),
        label=point.label,
        lat=point.lat,
        lng=point.lng,
        distance_diag=point.distance_diag,
    )


def seed_cells(profile: ScanProfile) -> tuple[GridCell, ...]:
    if profile.coverage == "points":
        return tuple(_point_cell(profile, point) for point in profile.points)

    cells: list[GridCell] = []
    spacing_miles = profile.initial_distance_diag * (1.0 - profile.overlap)
    for label, lat, lng in conus_grid_points(spacing_miles):
        cells.append(
            GridCell(
                cell_id=_cell_id(profile.name, lat, lng, profile.initial_distance_diag, 0),
                label=label,
                lat=lat,
                lng=lng,
                distance_diag=profile.initial_distance_diag,
            )
        )
    return tuple(cells)


def subdivide(cell: GridCell, profile: ScanProfile) -> tuple[GridCell, ...]:
    child_diag = max(profile.minimum_distance_diag, cell.distance_diag // 2)
    if cell.depth >= profile.max_depth or child_diag >= cell.distance_diag:
        return ()
    offset_miles = cell.distance_diag / (4 * math.sqrt(2))
    lat_offset = offset_miles / MILES_PER_LATITUDE_DEGREE
    lng_offset = offset_miles / (
        MILES_PER_LATITUDE_DEGREE * math.cos(math.radians(cell.lat))
    )
    children: list[GridCell] = []
    for lat_sign, lng_sign, quadrant in (
        (1, -1, "NW"),
        (1, 1, "NE"),
        (-1, -1, "SW"),
        (-1, 1, "SE"),
    ):
        lat = round(cell.lat + lat_sign * lat_offset, 6)
        lng = round(cell.lng + lng_sign * lng_offset, 6)
        children.append(
            GridCell(
                cell_id=_cell_id(profile.name, lat, lng, child_diag, cell.depth + 1),
                label=f"{cell.label}/{quadrant}",
                lat=lat,
                lng=lng,
                distance_diag=child_diag,
                depth=cell.depth + 1,
                parent_cell_id=cell.cell_id,
            )
        )
    return tuple(children)
