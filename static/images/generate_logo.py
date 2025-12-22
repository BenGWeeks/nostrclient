#!/usr/bin/env python3
"""
Generate the Nostr Proxy logo.
Requires: pip install Pillow
"""
import math
from PIL import Image, ImageDraw

# Create 128x128 image with transparency
size = 128
img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)

# Colors
dark_purple = (80, 40, 120)      # Darker base purple
light_purple = (140, 100, 180)   # Lighter purple for swoosh
white = (255, 255, 255)
white_transparent = (255, 255, 255, 180)  # Semi-transparent white for lines

# Draw a rounded rectangle background (darker purple)
margin = 8
draw.rounded_rectangle(
    [margin, margin, size - margin, size - margin],
    radius=20,
    fill=dark_purple
)

# Draw a large "swoosh" circle in the background (lighter purple)
# Positioned so circumference intersects the middle
swoosh_center = (size + 100, -90)
swoosh_radius = 220
draw.ellipse([
    swoosh_center[0] - swoosh_radius,
    swoosh_center[1] - swoosh_radius,
    swoosh_center[0] + swoosh_radius,
    swoosh_center[1] + swoosh_radius
], fill=light_purple)

# Redraw the rounded rectangle as a mask (clip the swoosh)
mask = Image.new('L', (size, size), 0)
mask_draw = ImageDraw.Draw(mask)
mask_draw.rounded_rectangle(
    [margin, margin, size - margin, size - margin],
    radius=20,
    fill=255
)

# Create final background with swoosh clipped
bg = Image.new('RGBA', (size, size), (0, 0, 0, 0))
bg_draw = ImageDraw.Draw(bg)
bg_draw.rounded_rectangle([margin, margin, size - margin, size - margin], radius=20, fill=dark_purple)
bg_draw.ellipse([
    swoosh_center[0] - swoosh_radius,
    swoosh_center[1] - swoosh_radius,
    swoosh_center[0] + swoosh_radius,
    swoosh_center[1] + swoosh_radius
], fill=light_purple)

# Apply mask
final = Image.new('RGBA', (size, size), (0, 0, 0, 0))
final.paste(bg, mask=mask)
draw = ImageDraw.Draw(final)

center_x = size // 2
center_y = size // 2

# 4 output circles equidistant from center, fanning out
radius = 44
angles = [-35, -12, 12, 35]

relay_positions = []
for angle in angles:
    rad = math.radians(angle)
    x = center_x + radius * math.cos(rad)
    y = center_y + radius * math.sin(rad)
    relay_positions.append((x, y))

# Draw semi-transparent lines first
for x, y in relay_positions:
    draw.line([(center_x, center_y), (x, y)], fill=white_transparent, width=2)

# Central circle (the multiplexer) - in the middle (on top of lines)
draw.ellipse([center_x - 14, center_y - 14, center_x + 14, center_y + 14], fill=white)

# Arrow coming in from the left
arrow_start_x = 16
arrow_end_x = center_x - 14
draw.line([(arrow_start_x, center_y), (arrow_end_x, center_y)], fill=white, width=4)
draw.polygon([
    (arrow_end_x, center_y),
    (arrow_end_x - 8, center_y - 6),
    (arrow_end_x - 8, center_y + 6)
], fill=white)

# Draw output circles on top
for x, y in relay_positions:
    draw.ellipse([x - 7, y - 7, x + 7, y + 7], fill=white)

final.save('nostr-proxy.png')
print("Logo saved to nostr-proxy.png")
