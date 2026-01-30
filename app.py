"""
MapAnt High-Resolution Export Tool

Flask backend that serves the map interface and handles high-resolution
export requests by fetching and stitching WMS tiles.
"""

import io
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, send_file, jsonify
from PIL import Image
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
