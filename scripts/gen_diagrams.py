#!/usr/bin/env python3
"""Generate all 5 launch campaign diagrams using Pillow."""

from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "assets" / "diagrams"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Design tokens
BG = "#0B0F14"
BLUE = "#2F80ED"
RED = "#EB5757"
STROKE = "#2C3440"
TEXT_COLOR = "#E6EDF3"
MUTED = "#9AA4B2"
W16_9 = (1920, 1080)
W1_1 = (1080, 1080)


def get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Get a font, falling back to default if Inter not available."""
    names = [
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def draw_rounded_rect(draw, xy, radius, fill=None, outline=None, width=2):
    """Draw a rounded rectangle."""
    x1, y1, x2, y2 = xy
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)


def draw_arrow(draw, start, end, color, width=2):
    """Draw a line with arrowhead."""
    draw.line([start, end], fill=color, width=width)
    # Arrowhead
    import math
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    angle = math.atan2(dy, dx)
    arrow_len = 12
    for offset in [2.5, -2.5]:
        ax = end[0] - arrow_len * math.cos(angle + offset * 0.3)
        ay = end[1] - arrow_len * math.sin(angle + offset * 0.3)
        draw.line([end, (ax, ay)], fill=color, width=width)


# ---------------------------------------------------------------------------
# Diagram 1: $12K Weekend (05-12k-weekend.png)
# ---------------------------------------------------------------------------
def gen_12k_weekend():
    img = Image.new("RGB", W16_9, BG)
    draw = ImageDraw.Draw(img)

    title_font = get_font(52, bold=True)
    label_font = get_font(28)
    big_font = get_font(72, bold=True)
    small_font = get_font(20)
    muted_font = get_font(18)

    # Title
    draw.text((80, 60), "The $12K Weekend", fill=TEXT_COLOR, font=title_font)

    # Timeline boxes
    timeline = [
        ("Friday 6pm", "$0", "Deploy"),
        ("Saturday 2am", "$12", "Retry cascade"),
        ("Sunday", "$4,200", "90K API calls"),
        ("Monday 9am", "$12,847", "Invoice shock"),
    ]

    box_w, box_h = 350, 200
    start_x, start_y = 100, 280
    gap = 60

    for i, (time_label, cost, desc) in enumerate(timeline):
        x = start_x + i * (box_w + gap)
        y = start_y

        is_last = i == len(timeline) - 1
        outline_color = RED if is_last else STROKE
        fill_color = "#1A1520" if is_last else None

        draw_rounded_rect(draw, (x, y, x + box_w, y + box_h),
                          radius=16, fill=fill_color, outline=outline_color, width=2)

        draw.text((x + 20, y + 20), time_label, fill=MUTED, font=small_font)
        cost_color = RED if is_last else BLUE
        cost_font = big_font if is_last else get_font(48, bold=True)
        draw.text((x + 20, y + 60), cost, fill=cost_color, font=cost_font)
        draw.text((x + 20, y + 150), desc, fill=TEXT_COLOR, font=small_font)

        # Arrow between boxes
        if i < len(timeline) - 1:
            ax = x + box_w + 5
            bx = x + box_w + gap - 5
            ay = y + box_h // 2
            draw_arrow(draw, (ax, ay), (bx, ay), MUTED, width=2)

    # Cost curve annotation
    curve_y = 560
    draw.text((100, curve_y), "48 hours. Zero alerts. No circuit breaker.",
              fill=TEXT_COLOR, font=label_font)

    # Big red number
    draw.text((100, curve_y + 80), "$12,847", fill=RED, font=get_font(96, bold=True))
    draw.text((520, curve_y + 120), "from one weekend", fill=MUTED, font=label_font)

    # Cost bar visualization (right side)
    bar_heights = [8, 24, 280, 380]
    bar_labels = ["Fri", "Sat", "Sun", "Mon"]
    bar_x = 1200
    bar_bottom = 960

    for i, (h, lbl) in enumerate(zip(bar_heights, bar_labels)):
        x = bar_x + i * 120
        bar_color = RED if i == 3 else BLUE
        draw.rectangle((x, bar_bottom - h, x + 80, bar_bottom),
                        fill=bar_color)
        draw.text((x + 20, bar_bottom + 10), lbl, fill=MUTED, font=small_font)

    # Footer
    draw.text((80, 1030), "Without execution enforcement.",
              fill=MUTED, font=muted_font)

    img.save(OUTPUT_DIR / "05-12k-weekend.png", "PNG")
    print(f"  [OK] 05-12k-weekend.png")


# ---------------------------------------------------------------------------
# Diagram 2: Retry Cascade (01-retry-cascade.png)
# ---------------------------------------------------------------------------
def gen_retry_cascade():
    img = Image.new("RGB", W16_9, BG)
    draw = ImageDraw.Draw(img)

    title_font = get_font(52, bold=True)
    label_font = get_font(26)
    big_font = get_font(64, bold=True)
    small_font = get_font(20)
    muted_font = get_font(18)

    draw.text((80, 60), "Retry Cascade", fill=TEXT_COLOR, font=title_font)

    # Service chain boxes
    services = ["User Request", "Service A", "Service B", "Service C", "LLM API"]
    box_w, box_h = 260, 80
    start_x, start_y = 80, 220
    gap = 50

    for i, svc in enumerate(services):
        x = start_x + i * (box_w + gap)
        y = start_y
        outline = BLUE if i < len(services) - 1 else RED
        draw_rounded_rect(draw, (x, y, x + box_w, y + box_h),
                          radius=16, outline=outline, width=2)
        draw.text((x + 20, y + 25), svc, fill=TEXT_COLOR, font=label_font)

        if i < len(services) - 1:
            ax = x + box_w + 5
            bx = x + box_w + gap - 5
            ay = y + box_h // 2
            draw_arrow(draw, (ax, ay), (bx, ay), MUTED, width=2)

    # Retry annotation
    retry_y = 380
    for i in range(3):
        y = retry_y + i * 60
        x = start_x + 3 * (box_w + gap)
        draw_rounded_rect(draw, (x, y, x + box_w, y + 45),
                          radius=12, outline=RED, width=1)
        draw.text((x + 15, y + 10), f"Retry {i + 1} -> LLM API",
                  fill=RED, font=small_font)

    # Math
    math_y = 620
    draw.text((80, math_y), "3 retries", fill=RED, font=get_font(40, bold=True))
    draw.text((340, math_y + 8), "x", fill=MUTED, font=label_font)
    draw.text((380, math_y), "5 nested calls", fill=BLUE, font=get_font(40, bold=True))
    draw.text((770, math_y + 8), "=", fill=MUTED, font=label_font)
    draw.text((820, math_y), "15 LLM Calls", fill=RED, font=big_font)

    draw.text((820, math_y + 80), "From one request.", fill=MUTED, font=label_font)

    # Footer
    draw.text((80, 1030), "Without chain-level containment.",
              fill=MUTED, font=muted_font)

    img.save(OUTPUT_DIR / "01-retry-cascade.png", "PNG")
    print(f"  [OK] 01-retry-cascade.png")


# ---------------------------------------------------------------------------
# Diagram 3: Agent Runaway Loop (02-agent-runaway-loop.png)
# ---------------------------------------------------------------------------
def gen_agent_runaway():
    img = Image.new("RGB", W1_1, BG)
    draw = ImageDraw.Draw(img)

    title_font = get_font(44, bold=True)
    label_font = get_font(24)
    small_font = get_font(20)
    muted_font = get_font(18)

    draw.text((60, 50), "Agent Runaway Loop", fill=TEXT_COLOR, font=title_font)

    # Center loop - 3 boxes in triangle
    cx, cy = 540, 420
    r = 180

    import math
    positions = []
    labels = ["Agent", "LLM Call", "Tool Call"]
    for i in range(3):
        angle = -90 + i * 120
        x = cx + int(r * math.cos(math.radians(angle)))
        y = cy + int(r * math.sin(math.radians(angle)))
        positions.append((x, y))

    box_w, box_h = 160, 60
    for i, (x, y) in enumerate(positions):
        bx, by = x - box_w // 2, y - box_h // 2
        draw_rounded_rect(draw, (bx, by, bx + box_w, by + box_h),
                          radius=16, outline=BLUE, width=2)
        draw.text((bx + 15, by + 15), labels[i], fill=TEXT_COLOR, font=label_font)

    # Arrows between boxes
    for i in range(3):
        j = (i + 1) % 3
        x1, y1 = positions[i]
        x2, y2 = positions[j]
        mx = (x1 + x2) // 2
        my = (y1 + y2) // 2
        draw_arrow(draw, (x1, y1), (mx, my), MUTED, width=2)

    # Side annotations
    annotations = [
        "No step limit",
        "No budget ceiling",
        "No loop detection",
    ]
    for i, text in enumerate(annotations):
        draw.text((760, 300 + i * 45), text, fill=RED, font=label_font)

    # Step counter at bottom
    step_y = 720
    draw.text((60, step_y), "Step 1", fill=MUTED, font=small_font)
    draw.text((170, step_y), "Step 2", fill=MUTED, font=small_font)
    draw.text((280, step_y), "Step 3", fill=MUTED, font=small_font)
    draw.text((390, step_y), "...", fill=MUTED, font=label_font)
    draw.text((440, step_y), "Step 137", fill=RED, font=get_font(28, bold=True))

    # Footer
    draw.text((60, 1030), "Execution enforcement stops this.",
              fill=MUTED, font=muted_font)

    img.save(OUTPUT_DIR / "02-agent-runaway-loop.png", "PNG")
    print(f"  [OK] 02-agent-runaway-loop.png")


# ---------------------------------------------------------------------------
# Diagram 4: Observability vs Enforcement (03)
# ---------------------------------------------------------------------------
def gen_obs_vs_enforcement():
    img = Image.new("RGB", W16_9, BG)
    draw = ImageDraw.Draw(img)

    title_font = get_font(48, bold=True)
    label_font = get_font(26)
    small_font = get_font(22)
    muted_font = get_font(18)

    draw.text((80, 60), "Observability vs Execution Enforcement",
              fill=TEXT_COLOR, font=title_font)

    # Left column: Observability (muted)
    left_x = 120
    draw.text((left_x, 180), "Observability", fill=MUTED, font=get_font(36, bold=True))

    obs_steps = ["Event", "Logs", "Dashboard", "Post-incident analysis"]
    for i, step in enumerate(obs_steps):
        y = 270 + i * 100
        draw_rounded_rect(draw, (left_x, y, left_x + 360, y + 65),
                          radius=16, outline=STROKE, width=2)
        draw.text((left_x + 20, y + 18), step, fill=MUTED, font=label_font)
        if i < len(obs_steps) - 1:
            draw_arrow(draw, (left_x + 180, y + 65 + 5),
                       (left_x + 180, y + 100 - 5), STROKE, width=2)

    draw.text((left_x, 690), "Detects after impact.", fill=MUTED, font=small_font)

    # Divider
    draw.line([(920, 160), (920, 800)], fill=STROKE, width=1)

    # Right column: Enforcement (blue accents)
    right_x = 1000
    draw.text((right_x, 180), "Execution Enforcement",
              fill=BLUE, font=get_font(36, bold=True))

    enf_steps = ["Policy check", "Budget check", "Step guard", "Allow / Stop"]
    for i, step in enumerate(enf_steps):
        y = 270 + i * 100
        is_last = i == len(enf_steps) - 1
        outline = BLUE
        draw_rounded_rect(draw, (right_x, y, right_x + 360, y + 65),
                          radius=16, outline=outline, width=2)
        color = TEXT_COLOR if not is_last else BLUE
        draw.text((right_x + 20, y + 18), step, fill=color, font=label_font)
        if i < len(enf_steps) - 1:
            draw_arrow(draw, (right_x + 180, y + 65 + 5),
                       (right_x + 180, y + 100 - 5), BLUE, width=2)

    draw.text((right_x, 690), "Prevents before impact.",
              fill=BLUE, font=small_font)

    # Footer
    draw.text((80, 1030), "Both are necessary. Only one prevents the invoice.",
              fill=MUTED, font=muted_font)

    img.save(OUTPUT_DIR / "03-observability-vs-enforcement.png", "PNG")
    print(f"  [OK] 03-observability-vs-enforcement.png")


# ---------------------------------------------------------------------------
# Diagram 5: Execution Safety Stack (04)
# ---------------------------------------------------------------------------
def gen_safety_stack():
    img = Image.new("RGB", W16_9, BG)
    draw = ImageDraw.Draw(img)

    title_font = get_font(48, bold=True)
    label_font = get_font(30)
    small_font = get_font(22)
    muted_font = get_font(18)

    draw.text((80, 60), "LLM Execution Safety Stack",
              fill=TEXT_COLOR, font=title_font)

    # Stack layers
    layers = [
        ("Application", STROKE, TEXT_COLOR),
        ("Agent", STROKE, TEXT_COLOR),
        ("Retry Containment", BLUE, TEXT_COLOR),
        ("Budget Enforcement", BLUE, TEXT_COLOR),
        ("Circuit Breaker", BLUE, TEXT_COLOR),
        ("LLM Provider", STROKE, MUTED),
    ]

    layer_w, layer_h = 700, 85
    start_x, start_y = 200, 180
    gap = 12

    enforcement_top = None
    enforcement_bottom = None

    for i, (name, outline, text_color) in enumerate(layers):
        y = start_y + i * (layer_h + gap)
        draw_rounded_rect(draw, (start_x, y, start_x + layer_w, y + layer_h),
                          radius=16, outline=outline, width=2)
        draw.text((start_x + 30, y + 25), name, fill=text_color, font=label_font)

        if name == "Retry Containment":
            enforcement_top = y
        if name == "Circuit Breaker":
            enforcement_bottom = y + layer_h

    # Blue bracket for enforcement layer
    if enforcement_top and enforcement_bottom:
        bx = start_x + layer_w + 40
        draw.line([(bx, enforcement_top), (bx, enforcement_bottom)],
                  fill=BLUE, width=3)
        draw.line([(bx - 10, enforcement_top), (bx, enforcement_top)],
                  fill=BLUE, width=3)
        draw.line([(bx - 10, enforcement_bottom), (bx, enforcement_bottom)],
                  fill=BLUE, width=3)

        mid_y = (enforcement_top + enforcement_bottom) // 2
        draw.text((bx + 20, mid_y - 40), "Execution", fill=BLUE, font=label_font)
        draw.text((bx + 20, mid_y - 5), "Enforcement", fill=BLUE, font=label_font)
        draw.text((bx + 20, mid_y + 30), "Layer", fill=BLUE, font=label_font)

    # Red underline near LLM Provider
    provider_y = start_y + 5 * (layer_h + gap) + layer_h + 10
    draw.line([(start_x, provider_y), (start_x + layer_w, provider_y)],
              fill=RED, width=2)

    # Footer
    draw.text((80, 1030),
              "Failures amplify below this line without enforcement.",
              fill=MUTED, font=muted_font)

    img.save(OUTPUT_DIR / "04-execution-safety-stack.png", "PNG")
    print(f"  [OK] 04-execution-safety-stack.png")


if __name__ == "__main__":
    print("Generating diagrams...")
    gen_12k_weekend()
    gen_retry_cascade()
    gen_agent_runaway()
    gen_obs_vs_enforcement()
    gen_safety_stack()
    print(f"\nAll 5 diagrams saved to: {OUTPUT_DIR}")
