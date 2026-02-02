"""
MapAnt High-Resolution Export Tool

Flask backend that serves the map interface and handles high-resolution
export requests by fetching and stitching WMS tiles.
"""

import io
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, send_file, jsonify
from PIL import Image, ImageDraw, ImageFont
import requests

app = Flask(__name__, static_folder='static', static_url_path='')

# WMS Configuration
WMS_CRS = "EPSG:3301"
MAX_TILE_SIZE = 4000  # Max pixels per WMS request

# Layer configurations
LAYERS = {
    'mapant': {
        'url': 'https://mapantee.gokartor.se/ogc/wms.php',
        'layer': 'mapantee',
        'format': 'image/png'
    },
    'ortho': {
        'url': 'https://kaart.maaamet.ee/wms/fotokaart',
        'layer': 'EESTIFOTO',
        'format': 'image/jpeg'
    }
}

# A3 at 300 DPI
A3_LANDSCAPE = (4961, 3508)
A3_PORTRAIT = (3508, 4961)


@app.route('/')
def index():
    return app.send_static_file('index.html')


@app.route('/api/export', methods=['POST'])
def export_map():
    """
    Export a high-resolution map image.

    Expected JSON body:
    {
        "bbox": {"minx": float, "miny": float, "maxx": float, "maxy": float},
        "orientation": "landscape" | "portrait"
    }
    """
    data = request.get_json()

    if not data:
        return jsonify({"error": "No JSON data provided"}), 400

    bbox = data.get('bbox')
    orientation = data.get('orientation', 'landscape')
    layer = data.get('layer', 'mapant')
    grid = data.get('grid', True)

    if layer not in LAYERS:
        layer = 'mapant'

    if not bbox:
        return jsonify({"error": "Missing bbox parameter"}), 400

    try:
        minx = float(bbox['minx'])
        miny = float(bbox['miny'])
        maxx = float(bbox['maxx'])
        maxy = float(bbox['maxy'])
    except (KeyError, ValueError, TypeError) as e:
        return jsonify({"error": f"Invalid bbox format: {e}"}), 400

    # Determine output dimensions
    if orientation == 'portrait':
        output_width, output_height = A3_PORTRAIT
    else:
        output_width, output_height = A3_LANDSCAPE

    # Calculate tile grid
    tiles_x = math.ceil(output_width / MAX_TILE_SIZE)
    tiles_y = math.ceil(output_height / MAX_TILE_SIZE)

    # Calculate geographic extent
    geo_width = maxx - minx
    geo_height = maxy - miny

    # Create output image
    final_image = Image.new('RGB', (output_width, output_height), (255, 255, 255))

    # Build list of tile specifications
    tile_specs = []
    for ty in range(tiles_y):
        for tx in range(tiles_x):
            # Calculate pixel bounds for this tile
            px_left = tx * MAX_TILE_SIZE
            px_top = ty * MAX_TILE_SIZE
            px_right = min((tx + 1) * MAX_TILE_SIZE, output_width)
            px_bottom = min((ty + 1) * MAX_TILE_SIZE, output_height)

            tile_width = px_right - px_left
            tile_height = px_bottom - px_top

            # Calculate geographic bounds for this tile
            # Note: pixel Y increases downward, but geo Y increases upward
            tile_minx = minx + (px_left / output_width) * geo_width
            tile_maxx = minx + (px_right / output_width) * geo_width
            tile_maxy = maxy - (px_top / output_height) * geo_height
            tile_miny = maxy - (px_bottom / output_height) * geo_height

            tile_specs.append({
                'minx': tile_minx, 'miny': tile_miny,
                'maxx': tile_maxx, 'maxy': tile_maxy,
                'width': tile_width, 'height': tile_height,
                'px_left': px_left, 'px_top': px_top,
                'layer': layer
            })

    # Fetch tiles concurrently
    with ThreadPoolExecutor(max_workers=4) as executor:
        future_to_spec = {
            executor.submit(
                fetch_wms_tile,
                spec['minx'], spec['miny'], spec['maxx'], spec['maxy'],
                spec['width'], spec['height'], spec['layer']
            ): spec
            for spec in tile_specs
        }

        for future in as_completed(future_to_spec):
            spec = future_to_spec[future]
            tile_image = future.result()
            if tile_image:
                final_image.paste(tile_image, (spec['px_left'], spec['px_top']))

    meters_per_pixel = geo_width / output_width

    # Add grid overlay if enabled
    if grid:
        draw_grid(final_image, minx, miny, maxx, maxy, meters_per_pixel)

    # Add scale bar
    draw_scale_bar(final_image, meters_per_pixel)

    # Save to buffer and return
    buffer = io.BytesIO()
    final_image.save(buffer, format='PNG', optimize=True)
    buffer.seek(0)

    filename = f"{layer}_a3_{orientation}.png"

    return send_file(
        buffer,
        mimetype='image/png',
        as_attachment=True,
        download_name=filename
    )


def draw_grid(image, minx, miny, maxx, maxy, meters_per_pixel):
    """Draw a thin black grid overlay on the image, aligned to coordinate system."""
    # Grid spacing options (in meters)
    grid_options = [100, 200, 500, 1000, 2000, 5000]

    # Target: grid cells should be roughly 5-15% of image width
    target_cell_px = image.width * 0.10
    target_cell_meters = target_cell_px * meters_per_pixel

    # Find the best grid spacing
    grid_spacing = grid_options[0]
    for spacing in grid_options:
        if spacing <= target_cell_meters * 1.5:
            grid_spacing = spacing

    draw = ImageDraw.Draw(image)
    line_color = (0, 0, 0, 80)  # Semi-transparent black

    # Create overlay for semi-transparent lines
    overlay = Image.new('RGBA', image.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)

    geo_width = maxx - minx
    geo_height = maxy - miny

    # Draw vertical lines (constant X in EPSG:3301)
    # Start from first grid line >= minx
    first_x = math.ceil(minx / grid_spacing) * grid_spacing
    x = first_x
    while x <= maxx:
        # Convert geo X to pixel X
        px_x = int((x - minx) / geo_width * image.width)
        if 0 <= px_x < image.width:
            overlay_draw.line([(px_x, 0), (px_x, image.height)], fill=line_color, width=1)
        x += grid_spacing

    # Draw horizontal lines (constant Y in EPSG:3301)
    # Start from first grid line >= miny
    first_y = math.ceil(miny / grid_spacing) * grid_spacing
    y = first_y
    while y <= maxy:
        # Convert geo Y to pixel Y (Y is inverted: higher geo Y = lower pixel Y)
        px_y = int((maxy - y) / geo_height * image.height)
        if 0 <= px_y < image.height:
            overlay_draw.line([(0, px_y), (image.width, px_y)], fill=line_color, width=1)
        y += grid_spacing

    # Composite the grid overlay onto the image
    image.paste(Image.alpha_composite(image.convert('RGBA'), overlay).convert('RGB'))


def draw_scale_bar(image, meters_per_pixel):
    """Draw a scale bar on the bottom right of the image."""
    # Scale bar distances to choose from (in meters)
    scale_options = [100, 200, 500, 1000, 2000, 5000, 10000]

    # Target scale bar width: ~15% of image width
    target_width_px = image.width * 0.15

    # Find the best scale distance
    best_distance = scale_options[0]
    for distance in scale_options:
        bar_width_px = distance / meters_per_pixel
        if bar_width_px <= target_width_px * 1.5:
            best_distance = distance

    bar_width_px = int(best_distance / meters_per_pixel)

    # Format label
    if best_distance >= 1000:
        label = f"{best_distance // 1000} km"
    else:
        label = f"{best_distance} m"

    # Position (bottom right with margin)
    margin = 40
    bar_height = 12
    x_right = image.width - margin
    x_left = x_right - bar_width_px
    y_bottom = image.height - margin
    y_top = y_bottom - bar_height

    draw = ImageDraw.Draw(image)

    # Try to load a font, fall back to default
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
    except (IOError, OSError):
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 28)
        except (IOError, OSError):
            font = ImageFont.load_default()

    # Get text size
    text_bbox = draw.textbbox((0, 0), label, font=font)
    text_width = text_bbox[2] - text_bbox[0]
    text_height = text_bbox[3] - text_bbox[1]

    # Draw semi-transparent background
    bg_padding = 15
    bg_left = x_left - bg_padding
    bg_top = y_top - text_height - bg_padding * 2
    bg_right = x_right + bg_padding
    bg_bottom = y_bottom + bg_padding

    # Create overlay for semi-transparent background
    overlay = Image.new('RGBA', image.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    overlay_draw.rounded_rectangle(
        [bg_left, bg_top, bg_right, bg_bottom],
        radius=8,
        fill=(255, 255, 255, 200)
    )
    image.paste(Image.alpha_composite(image.convert('RGBA'), overlay).convert('RGB'))

    # Redraw on the composited image
    draw = ImageDraw.Draw(image)

    # Draw scale bar (black with white outline for visibility)
    # White outline
    draw.rectangle([x_left-2, y_top-2, x_right+2, y_bottom+2], fill=(255, 255, 255))
    # Black bar
    draw.rectangle([x_left, y_top, x_right, y_bottom], fill=(0, 0, 0))

    # Draw end ticks
    tick_height = 8
    draw.rectangle([x_left, y_top - tick_height, x_left + 3, y_bottom], fill=(0, 0, 0))
    draw.rectangle([x_right - 3, y_top - tick_height, x_right, y_bottom], fill=(0, 0, 0))

    # Draw middle tick
    mid_x = (x_left + x_right) // 2
    draw.rectangle([mid_x - 1, y_top - tick_height // 2, mid_x + 2, y_bottom], fill=(0, 0, 0))

    # Draw label centered above bar
    text_x = x_left + (bar_width_px - text_width) // 2
    text_y = y_top - text_height - 10
    draw.text((text_x, text_y), label, fill=(0, 0, 0), font=font)


def fetch_wms_tile(minx, miny, maxx, maxy, width, height, layer='mapant'):
    """Fetch a single tile from the WMS service."""
    layer_config = LAYERS.get(layer, LAYERS['mapant'])

    # WMS 1.3.0 axis order depends on CRS
    # Maaamet uses Y,X (Northing, Easting) for EPSG:3301
    # MapAnt uses X,Y order
    if layer == 'ortho':
        bbox = f"{miny},{minx},{maxy},{maxx}"  # Y,X order for Maaamet
    else:
        bbox = f"{minx},{miny},{maxx},{maxy}"  # X,Y order for MapAnt

    params = {
        'SERVICE': 'WMS',
        'VERSION': '1.3.0',
        'REQUEST': 'GetMap',
        'LAYERS': layer_config['layer'],
        'CRS': WMS_CRS,
        'BBOX': bbox,
        'WIDTH': width,
        'HEIGHT': height,
        'FORMAT': layer_config['format'],
        'STYLES': ''
    }

    try:
        response = requests.get(layer_config['url'], params=params, timeout=60)
        response.raise_for_status()

        # Check if response is an image
        content_type = response.headers.get('Content-Type', '')
        if 'image' not in content_type:
            print(f"WMS error: {response.text[:500]}")
            return None

        return Image.open(io.BytesIO(response.content))

    except requests.RequestException as e:
        print(f"Failed to fetch tile: {e}")
        return None
    except Exception as e:
        print(f"Error processing tile: {e}")
        return None


if __name__ == '__main__':
    app.run(debug=True, port=5000)
