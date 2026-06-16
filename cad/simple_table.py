"""Simple four-legged rectangular table.

Coordinate convention:
- Origin: center of the table footprint, on the floor.
- XY: floor plane. +Z: up.
- Tabletop top surface sits at z = table_height.
"""

from build123d import *
from cadpy.assembly import AssemblyHelper

# --- Parameters (mm) ---
top_length = 1200.0       # X span of the tabletop
top_width = 750.0         # Y span of the tabletop
top_thickness = 30.0      # tabletop slab thickness
table_height = 750.0      # height of the top surface above the floor
edge_chamfer = 2.0        # cosmetic chamfer on the tabletop top perimeter

leg_section = 60.0        # square leg cross-section (X and Y)
leg_inset = 50.0          # inset of each leg's outer faces from the tabletop edges

leg_height = table_height - top_thickness  # legs stop at the underside of the top

# Leg centers: inset from the footprint edges by leg_inset + half the section.
leg_x = top_length / 2 - leg_inset - leg_section / 2
leg_y = top_width / 2 - leg_inset - leg_section / 2
leg_positions = {
    "front_left": (-leg_x, -leg_y),
    "front_right": (leg_x, -leg_y),
    "rear_left": (-leg_x, leg_y),
    "rear_right": (leg_x, leg_y),
}


def make_tabletop():
    with BuildPart() as top:
        Box(top_length, top_width, top_thickness)
        top_perimeter = top.edges().filter_by(Plane.XY).group_by(Axis.Z)[-1]
        chamfer(top_perimeter, edge_chamfer)
    # Raise so the top surface lands at table_height.
    return Pos(0, 0, table_height - top_thickness / 2) * top.part


def make_leg():
    # Centered solid; raised so it spans floor (z=0) to underside of the top.
    return Pos(0, 0, leg_height / 2) * Box(leg_section, leg_section, leg_height)


def gen_step():
    asm = AssemblyHelper("simple_table")
    asm.add(make_tabletop(), "tabletop")
    for placement, (x, y) in leg_positions.items():
        asm.add(Pos(x, y, 0) * make_leg(), "leg", placement)
    return asm.build()
