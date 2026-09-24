import cadquery as cq
from cadquery import exporters

# ====================== PARAMETERS ======================
body_length = 10.20
body_width  = 5.30
body_height = 1.75

pitch = 0.65
pins_per_side = 14
lead_width = 0.28
standoff = 0.10

# ====================== BODY ======================
body = (
    cq.Workplane("XY")
    .box(body_length, body_width, body_height)
    .translate((0, 0, body_height/2 + standoff))
)

# ====================== PROPER GULL-WING LEAD ======================
def make_gullwing():
    # This profile matches the left drawing you circled
    lead = (
        cq.Workplane("XZ")
        .moveTo(0, body_height * 0.45)          # start at body
        .lineTo(0.15, body_height * 0.45)       # small horizontal out
        .lineTo(0.25, standoff + 0.45)          # go down
        .lineTo(0.55, standoff + 0.15)          # angle
        .lineTo(0.90, standoff + 0.12)          # foot start
        .lineTo(0.90, standoff)                 # foot bottom
        .lineTo(0.65, standoff)                 # foot tip
        .lineTo(0.20, body_height * 0.35)
        .close()
        .extrude(lead_width)
        .translate((-lead_width/2, 0, 0))
    )
    return lead

# ====================== PLACE LEADS ======================
leads = []
start_x = -(pins_per_side - 1) * pitch / 2

# Bottom side
for i in range(pins_per_side):
    x = start_x + i * pitch
    lead = make_gullwing().rotate((0,0,0), (0,0,1), 180)
    lead = lead.translate((x, -body_width/2, 0))
    leads.append(lead)

# Top side
for i in range(pins_per_side):
    x = start_x + i * pitch
    lead = make_gullwing()
    lead = lead.translate((x, body_width/2, 0))
    leads.append(lead)

# ====================== COMBINE ======================
result = body
for lead in leads:
    result = result.union(lead)

# ====================== EXPORT ======================
exporters.export(result, "SSOP_28pin.step")
print("STEP saved: SSOP_28pin.step")

exporters.export(result, "SSOP_28pin.stl")
print("STL saved: SSOP_28pin.stl")