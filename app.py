"""
MapAnt High-Resolution Export Tool

Flask backend that serves the map interface and handles high-resolution
export requests by fetching and stitching WMS tiles.
"""

import collections
import io
import math
import os
import secrets
import string
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, send_file, jsonify
from werkzeug.middleware.proxy_fix import ProxyFix
from PIL import Image, ImageDraw, ImageFont
import requests

app = Flask(__name__, static_folder='static', static_url_path='')

# One reverse proxy (Caddy) in front: trust exactly one hop of X-Forwarded-*,
# so request.remote_addr is the client, not the proxy. Direct hits (the
# container healthcheck) carry no such header and keep their real address.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Correlation id for log lines: 36 random bits as 6 URL-safe base64 chars.
ID_ALPHABET = string.ascii_uppercase + string.ascii_lowercase + string.digits + '-_'


def correlation_id():
    n = secrets.randbits(36)
    return ''.join(ID_ALPHABET[(n >> (6 * i)) & 63] for i in range(6))

# WMS Configuration
WMS_CRS = "EPSG:3301"
MAX_TILE_SIZE = 4000  # Max pixels per WMS request

# Layer configurations
LAYERS = {
    'mapant': {
        'url': 'https://mapantee.gokartor.se/ogc/wms.php',
        'layer': 'mapantee',
        'format': 'image/png',
        # Cartography uses ~180 distinct colours, so a 256-entry palette is
        # bit-exact here and cuts the response from 16 MB to 6 MB.
        'export': ('PNG', 'image/png', 'png'),
        # Measured by requesting one patch at rising densities until the result
        # stopped changing. Asking finer than this returns upsampled pixels.
        'native_mpp': 1.0,
        # MapAnt rate-limits bursts (429 at ~48 rapid requests), so go gently.
        'max_tiles': 16,
        'workers': 2
    },
    'ortho': {
        'url': 'https://kaart.maaamet.ee/wms/fotokaart',
        'layer': 'EESTIFOTO',
        'format': 'image/jpeg',
        # Photography: lossless PNG lands at 38 MB to preserve the artefacts of
        # an already-JPEG source. See JPEG_QUALITY for the re-encode tradeoff.
        'export': ('JPEG', 'image/jpeg', 'jpg'),
        'native_mpp': 0.25,
        # Maa-amet is slow (~5 s a request) but has never rate-limited us: fewer
        # tiles, more in flight. 6 tiles still gives ~2.2x oversampling on a
        # 10 km sheet in ~16 s; 15 tiles took 80 s for little visible gain.
        'max_tiles': 6,
        'workers': 4
    }
}

# The WMS call must finish well inside the gunicorn worker timeout, otherwise
# a single slow tile takes the worker down instead of returning an error.
WMS_TIMEOUT = 25

# Measured on a full A3 ortho export against the lossless original: q=90 gives
# 40.0 dB PSNR but q=92 jumps to 47.6 dB for 0.35 MB more -- there is a quality
# cliff just below 92. q=95 lands at 50.1 dB (mean error 0.54/255, worst 7/255),
# which is visually lossless in print, and costs 2.6 MB over q=92. Beyond that
# the curve flattens: q=97 buys 1.1 dB for 2.4 MB. 4:4:4 keeps full chroma,
# which matters for the thin coloured grid lines drawn over the photo.
JPEG_QUALITY = 95

# A3 at 300 DPI
A3_LANDSCAPE = (4961, 3508)
A3_PORTRAIT = (3508, 4961)

# How many pixels an export gets, regardless of shape. Default is an A3 sheet
# at 300 DPI, so an A3-ratio bbox comes out at exactly 4961x3508. Raise it with
# EXPORT_MEGAPIXELS to keep more of the source detail on large selections, at
# the cost of a proportionally bigger download.
PIXEL_BUDGET = (int(float(os.environ['EXPORT_MEGAPIXELS']) * 1e6)
                if 'EXPORT_MEGAPIXELS' in os.environ
                else A3_LANDSCAPE[0] * A3_LANDSCAPE[1])

# Guard against a pathological aspect ratio turning into thousands of tiles.
MAX_OUTPUT_EDGE = 20000

# The server only ever has its native raster (1 m/px for MapAnt). Asking it to
# draw at any other scale makes it resample -- smearing small selections into
# blurry, 57-colour blobs and dropping thin features from large ones. So the
# export never asks it to: it fetches the raster at native resolution and
# resamples once, locally, with Lanczos. How close to native a fetch can get is
# bounded only by each layer's request budget (`max_tiles` above, overridable
# with EXPORT_MAX_TILES); past it the fetch is coarsened just enough to fit.
# With the tile cache on, the budget bounds blocks, not cold tile fetches.
MAX_TILES_OVERRIDE = int(os.environ['EXPORT_MAX_TILES']) if 'EXPORT_MAX_TILES' in os.environ else None
WMS_RETRIES = 3

# Server-side tile cache, opt-in. Fetches are cacheable at all only because
# they are raster-aligned (see render_block): the server returns byte-identical
# pixels for the same aligned request every time. Tiles live on a fixed global
# grid -- CACHE_TILE fetch pixels square, at absolute multiples of
# CACHE_TILE * fetch_mpp metres -- so overlapping selections share them. Stored
# as the raw bytes the server sent. Neither server offers ETag/Last-Modified,
# so freshness is a plain TTL; MapAnt regenerates nightly, the content changes
# rarely. Unset EXPORT_CACHE_DIR means no caching and no change in behaviour.
CACHE_DIR = os.environ.get('EXPORT_CACHE_DIR') or None
# Grid tile edge in fetch pixels. Bigger tiles mean fewer requests on a cold
# export but more wasted edge, worst for small selections: measured on a 2 km
# selection, 4000 px tiles fetch 11-22x the pixels needed (alignment-dependent),
# 2000 px tiles 6-9x; a cold 10 km export is 10 requests at 4000 vs 29 at 2000.
CACHE_TILE = min(MAX_TILE_SIZE, max(500, int(os.environ.get('EXPORT_CACHE_TILE', '4000'))))
CACHE_TTL = float(os.environ.get('EXPORT_CACHE_TTL_DAYS', '7')) * 86400
CACHE_MAX_BYTES = int(float(os.environ.get('EXPORT_CACHE_MAX_MB', '2048')) * 1e6)

# An export holds a couple of 4000x4000 source tiles plus the sheet and its
# overlay buffers: ~550 MB peak for a 16.5 km sheet. Bound how many run at once
# per process so thread count cannot multiply that.
EXPORT_CONCURRENCY = max(1, int(os.environ.get('EXPORT_CONCURRENCY', '2')))
export_slots = threading.BoundedSemaphore(EXPORT_CONCURRENCY)

def block_margin(scale):
    """Source pixels to fetch beyond each block edge so Lanczos has its full
    support (3 output px, i.e. 3 * scale source px) at the join. Fetched,
    resampled and cropped away, so blocks join seamlessly."""
    return math.ceil(3 * max(scale, 1.0)) + 2


def compute_output_size(geo_width, geo_height):
    """Pixel size for a bbox: the ratio it asks for, with square pixels.

    Square pixels are the point. The WMS scales a request by width alone and
    anchors it top-left, so a tile whose pixel aspect differs from its bbox
    aspect comes back showing a different patch of ground -- which turned a
    non-A3 selection into a silent collage. Keeping the output at the bbox ratio
    makes every tile's aspect match its bbox by construction.

    Picking the shape here rather than from a paper size is what lets the UI
    stay in charge: it constrains selections to A3, and anything else exports
    honestly at whatever ratio it was given.
    """
    aspect = geo_width / geo_height

    width = math.sqrt(PIXEL_BUDGET * aspect)
    height = width / aspect

    longest = max(width, height)
    if longest > MAX_OUTPUT_EDGE:
        # Scale both axes together: lower resolution, still square pixels.
        width *= MAX_OUTPUT_EDGE / longest
        height *= MAX_OUTPUT_EDGE / longest

    return max(round(width), 1), max(round(height), 1)


@app.route('/')
def index():
    return app.send_static_file('index.html')


def cache_path(layer, fetch_mpp, col, row):
    ext = LAYERS[layer]['format'].split('/')[1].replace('jpeg', 'jpg')
    # Tile size is part of the namespace, so changing it never mixes grids.
    return os.path.join(CACHE_DIR, layer, f"{fetch_mpp:g}_{CACHE_TILE}", f"{col}_{row}.{ext}")


def cache_fresh(path):
    """True if the tile is on disk and younger than the TTL. Touches it, so
    eviction below is least-recently-used rather than oldest-written."""
    try:
        if time.time() - os.stat(path).st_mtime > CACHE_TTL:
            return False
        os.utime(path, None)
        return True
    except OSError:
        return False


def cache_put(path, data):
    """Atomic: a reader never sees a half-written tile, and two gunicorn
    workers writing the same tile at once just race to an identical result."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, path)


def cache_evict():
    """Drop least-recently-used tiles until the cache fits CACHE_MAX_BYTES."""
    entries = []
    for root, _, files in os.walk(CACHE_DIR):
        for name in files:
            if name.endswith('.tmp'):
                continue
            p = os.path.join(root, name)
            try:
                st = os.stat(p)
                entries.append((st.st_mtime, st.st_size, p))
            except OSError:
                pass
    total = sum(size for _, size, _ in entries)
    for _, size, p in sorted(entries):
        if total <= CACHE_MAX_BYTES:
            break
        try:
            os.remove(p)
            total -= size
        except OSError:
            pass


def tiles_touched(blocks, scale, grid_x, grid_y, off_x, off_y):
    """The global grid tiles (col, row) a set of blocks reads from, in absolute
    fetch-pixel space. Row indices count downward from the EPSG:3301 origin."""
    tiles = set()
    margin = block_margin(scale)
    for ox0, oy0, ox1, oy1 in blocks:
        x0 = grid_x + math.floor(off_x + ox0 * scale) - margin
        y0 = grid_y + math.floor(off_y + oy0 * scale) - margin
        x1 = grid_x + math.ceil(off_x + ox1 * scale) + margin
        y1 = grid_y + math.ceil(off_y + oy1 * scale) + margin
        for col in range(math.floor(x0 / CACHE_TILE), math.ceil(x1 / CACHE_TILE)):
            for row in range(math.floor(y0 / CACHE_TILE), math.ceil(y1 / CACHE_TILE)):
                tiles.add((col, row))
    return tiles


def fetch_grid_tile(layer, fetch_mpp, col, row):
    """Fetch one global grid tile and store it. Returns True on success."""
    x0 = col * CACHE_TILE * fetch_mpp
    y1 = -row * CACHE_TILE * fetch_mpp            # top edge (rows count downward)
    data = fetch_wms_bytes(x0, y1 - CACHE_TILE * fetch_mpp, x0 + CACHE_TILE * fetch_mpp, y1,
                           CACHE_TILE, CACHE_TILE, layer)
    if data is None:
        return False
    cache_put(cache_path(layer, fetch_mpp, col, row), data)
    return True


def source_colours(tile):
    """Pixel count per colour in a paletted source tile, as {rgb: count}. The
    encoder pins the most frequent ones so the dominant cartographic colours
    survive quantisation exactly. Non-paletted sources (ortho JPEG) have none.
    Costs one 256-bin histogram per tile."""
    if tile.mode != 'P':
        return collections.Counter()
    palette = tile.getpalette()
    return collections.Counter({tuple(palette[3 * i:3 * i + 3]): n
                                for n, i in tile.getcolors(256) or []})


def assemble_from_cache(layer, fetch_mpp, x0, y0, x1, y1):
    """Build the source rectangle [x0,x1)x[y0,y1) (absolute fetch px) from
    cached grid tiles. Every tile was prefetched, so a miss here is a bug.
    Returns (canvas, colours) with the source colour histogram seen."""
    canvas = Image.new('RGB', (x1 - x0, y1 - y0), (255, 255, 255))
    colours = collections.Counter()
    for col in range(math.floor(x0 / CACHE_TILE), math.ceil(x1 / CACHE_TILE)):
        for row in range(math.floor(y0 / CACHE_TILE), math.ceil(y1 / CACHE_TILE)):
            tx, ty = col * CACHE_TILE, row * CACHE_TILE
            ix0, iy0 = max(x0, tx), max(y0, ty)
            ix1, iy1 = min(x1, tx + CACHE_TILE), min(y1, ty + CACHE_TILE)
            with Image.open(cache_path(layer, fetch_mpp, col, row)) as tile:
                colours.update(source_colours(tile))
                part = tile.crop((ix0 - tx, iy0 - ty, ix1 - tx, iy1 - ty)).convert('RGB')
            canvas.paste(part, (ix0 - x0, iy0 - y0))
    return canvas, colours


def plan_render(geo_width, geo_height, output_width, output_height, native_mpp,
                max_tiles):
    """Decide the fetch resolution and split the output into blocks.

    Returns (fetch_mpp, scale, blocks) where scale is fetched pixels per output
    pixel and each block is (ox0, oy0, ox1, oy1) in output pixels.

    The fetch starts at native resolution and, if the block count would exceed
    max_tiles, is coarsened in whole multiples of the native pixel. Whole
    multiples matter: the fetch grid is anchored to the raster (see
    render_block), and only requests whose corners land on raster pixels come
    back consistent between windows.
    """
    output_mpp = geo_width / output_width
    multiple = 1
    while True:
        fetch_mpp = native_mpp * multiple
        scale = output_mpp / fetch_mpp
        usable = MAX_TILE_SIZE - 2 * block_margin(scale)
        block = max(1, int(usable / scale))
        cols = math.ceil(output_width / block)
        rows = math.ceil(output_height / block)
        blocks = [(x, y, min(x + block, output_width), min(y + block, output_height))
                  for y in range(0, output_height, block)
                  for x in range(0, output_width, block)]
        if cols * rows <= max_tiles or fetch_mpp >= output_mpp * usable:
            break
        multiple += 1

    return fetch_mpp, scale, blocks


def fetch_grid(minx, maxy, fetch_mpp):
    """Anchor of the raster-aligned fetch grid for a selection, as integer
    absolute fetch-pixel indices (x rightward, y downward from the EPSG:3301
    origin), plus the selection's sub-pixel offset within that grid."""
    grid_x = math.floor(minx / fetch_mpp)
    grid_y = -math.ceil(maxy / fetch_mpp)
    off_x = minx / fetch_mpp - grid_x
    off_y = -maxy / fetch_mpp - grid_y
    return grid_x, grid_y, off_x, off_y


def render_block(minx, maxy, fetch_mpp, scale, block, layer):
    """Fetch one block at native resolution and resample it to output pixels.

    Returns (piece, colours): the block and the source colour histogram it was
    built from, or None if its tiles could not be fetched.

    The fetch grid is anchored to the raster -- whole multiples of fetch_mpp
    from the origin -- not to the selection. MapAnt returns a *different*
    resampling of the same ground for every request whose bbox has a
    fractional-metre corner, so a grid anchored on an arbitrary selection
    gives neighbouring blocks slightly different pixels and a visible seam.
    On raster-aligned corners it returns the raw raster, identically every
    time. The selection's sub-pixel offset is applied here instead, in the
    resize's float `box`, which is exact.

    Each fetched region is an integer rectangle on that grid, so its bbox
    aspect matches its pixel aspect exactly and the server never pads it.
    """
    grid_x, grid_y, off_x, off_y = fetch_grid(minx, maxy, fetch_mpp)

    margin = block_margin(scale)
    ox0, oy0, ox1, oy1 = block
    # Absolute fetch-pixel rectangle (rows count downward from the origin).
    fx0 = grid_x + math.floor(off_x + ox0 * scale) - margin
    fy0 = grid_y + math.floor(off_y + oy0 * scale) - margin
    fx1 = grid_x + math.ceil(off_x + ox1 * scale) + margin
    fy1 = grid_y + math.ceil(off_y + oy1 * scale) + margin

    if CACHE_DIR:
        tile, colours = assemble_from_cache(layer, fetch_mpp, fx0, fy0, fx1, fy1)
    else:
        tile = fetch_wms_tile(fx0 * fetch_mpp, -fy1 * fetch_mpp,
                              fx1 * fetch_mpp, -fy0 * fetch_mpp,
                              fx1 - fx0, fy1 - fy0, layer)
        if tile is None:
            return None
        colours = source_colours(tile)

    # Back to block-relative for the resize box.
    fx0 -= grid_x
    fy0 -= grid_y
    box = (off_x + ox0 * scale - fx0, off_y + oy0 * scale - fy0,
           off_x + ox1 * scale - fx0, off_y + oy1 * scale - fy0)
    piece = tile.convert('RGB').resize((ox1 - ox0, oy1 - oy0), Image.LANCZOS, box=box)
    return piece, colours


@app.route('/api/export', methods=['POST'])
def export_map():
    """
    Export a high-resolution map image.

    Expected JSON body:
    {
        "bbox": {"minx": float, "miny": float, "maxx": float, "maxy": float}
    }

    Output size is derived from the bbox. An "orientation" key is accepted for
    older clients but ignored: the bbox already determines the shape.
    """
    rid = correlation_id()
    data = request.get_json(silent=True)
    print(f"[{rid}] export requested from {request.remote_addr}: "
          f"layer={data.get('layer', 'mapant') if isinstance(data, dict) else '?'} "
          f"bbox={data.get('bbox') if isinstance(data, dict) else data}")

    if not data:
        return jsonify({"error": "No JSON data provided"}), 400

    bbox = data.get('bbox')
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

    # Calculate geographic extent
    geo_width = maxx - minx
    geo_height = maxy - miny

    if geo_width <= 0 or geo_height <= 0:
        return jsonify({"error": "bbox must have maxx > minx and maxy > miny"}), 400

    # Size from the bbox, not from `orientation`: a bbox and an orientation that
    # disagree used to be accepted and produce a wrong sheet.
    output_width, output_height = compute_output_size(geo_width, geo_height)

    config = LAYERS[layer]
    max_tiles = MAX_TILES_OVERRIDE or config['max_tiles']

    # The plan is the same with or without a cache, so a cold cached export is
    # never coarser than an uncached one. A cold export touches up to
    # (cols+1)*(rows+1) grid tiles rather than cols*rows blocks; that overshoot
    # of the request budget is bounded by the layer's worker count and absorbed
    # by retry/backoff, and it is what buys hits for every overlapping export
    # afterwards.
    fetch_mpp, scale, blocks = plan_render(
        geo_width, geo_height, output_width, output_height,
        config['native_mpp'], max_tiles)

    started = time.time()
    with export_slots:
        if CACHE_DIR:
            touched = sorted(tiles_touched(blocks, scale, *fetch_grid(minx, maxy, fetch_mpp)))
            misses = [t for t in touched
                      if not cache_fresh(cache_path(layer, fetch_mpp, *t))]
            with ThreadPoolExecutor(max_workers=config['workers']) as executor:
                ok = list(executor.map(
                    lambda t: fetch_grid_tile(layer, fetch_mpp, *t), misses))

            hits = len(touched) - len(misses)
            print(f"[{rid}] cache: {layer} {fetch_mpp:g} m/px, {len(touched)} tiles, "
                  f"{hits} hit, {ok.count(True)} fetched, {ok.count(False)} failed, "
                  f"hit rate {hits / len(touched):.0%}")

            if not all(ok):
                return jsonify({
                    "error": f"{ok.count(False)} of {len(misses)} map tiles could "
                             f"not be fetched. The tile server may be rate "
                             f"limiting; try again."
                }), 502
            if misses:
                cache_evict()
            fetched = time.time()

        response, timing, nbytes = render_and_send(
            minx, miny, maxx, maxy, output_width, output_height,
            fetch_mpp, scale, blocks, layer, grid)

    # Without a cache each block fetches its own source inside render, so the
    # phases cannot be separated; with one, fetch is the prefetch above.
    if CACHE_DIR:
        phases = (f"{len(touched)} tiles -> {len(blocks)} blocks, "
                  f"fetch {fetched - started:.1f}s, render {timing['render']:.1f}s")
    else:
        phases = f"{len(blocks)} blocks, fetch+render {timing['render']:.1f}s"
    print(f"[{rid}] export: {layer} {fetch_mpp:g} m/px -> {output_width}x{output_height}, "
          f"{phases}, encode {timing['encode']:.1f}s, {nbytes / 1e6:.1f} MB, "
          f"total {time.time() - started:.1f}s")
    return response


def render_and_send(minx, miny, maxx, maxy, output_width, output_height,
                    fetch_mpp, scale, blocks, layer, grid):
    """The memory-heavy half of an export, run under `export_slots`.

    Returns (response, timing, nbytes); timing has 'render' (blocks and
    overlays) and 'encode' seconds.
    """
    started = time.time()
    final_image = Image.new('RGB', (output_width, output_height), (255, 255, 255))

    # Each worker holds one source tile plus its resampled block; the full
    # native-resolution sheet never exists in memory.
    failed = 0
    source_freq = collections.Counter()
    with ThreadPoolExecutor(max_workers=LAYERS[layer]['workers']) as executor:
        future_to_block = {
            executor.submit(render_block, minx, maxy, fetch_mpp, scale, block, layer): block
            for block in blocks
        }
        for future in as_completed(future_to_block):
            result = future.result()
            if result is None:
                # Pasting nothing leaves white, and a part-blank sheet that still
                # returns 200 is worse than no sheet: it looks like a real map.
                failed += 1
            else:
                piece, colours = result
                source_freq.update(colours)
                ox0, oy0, _, _ = future_to_block[future]
                final_image.paste(piece, (ox0, oy0))

    if failed:
        error = jsonify({
            "error": f"{failed} of {len(blocks)} map tiles could not be "
                     f"fetched, so the export would have had blank areas. "
                     f"The tile server may be rate limiting; try again, or "
                     f"lower EXPORT_MAX_TILES."
        })
        error.status_code = 502
        return error, {'render': time.time() - started, 'encode': 0.0}, 0

    meters_per_pixel = (maxx - minx) / output_width

    # Add grid overlay if enabled
    if grid:
        draw_grid(final_image, minx, miny, maxx, maxy, meters_per_pixel)

    # Add scale bar
    draw_scale_bar(final_image, meters_per_pixel)

    rendered = time.time()

    # Encode. Gunicorn streams this response from the worker, so payload size
    # decides how long the request stays open -- keep it small.
    buffer, mimetype, ext = encode_image(final_image, layer, source_freq)
    encoded = time.time()

    shape = 'landscape' if output_width >= output_height else 'portrait'
    filename = f"{layer}_a3_{shape}.{ext}"

    response = send_file(
        buffer,
        mimetype=mimetype,
        as_attachment=True,
        download_name=filename
    )
    timing = {'render': rendered - started, 'encode': encoded - rendered}
    return response, timing, buffer.getbuffer().nbytes


# Palette entries pinned to the most frequent source colours; the other 192
# learn the blends that resampling introduced at edges. A 30 Mpx MapAnt sheet
# draws on ~640 source colours (each server tile carries its own anti-aliased
# palette), of which the top 64 cover ~85% of source pixels. Pinning more than
# this leaves too few entries for blends and lowers fidelity.
PINNED_COLOURS = 64


def encode_image(image, layer, source_freq=None):
    """Encode the composed map for delivery, returning (buffer, mimetype, ext).

    PNG output is 256-colour: the PINNED_COLOURS most frequent source colours
    are kept verbatim and the rest of the palette is learned from a 1/16
    subsample with the fast octree quantiser. Measured on a 30 Mpx sheet
    against the lossless RGB: 40.4 dB in 0.05 s, versus the same 40.4 dB in
    2.2 s for an adaptive median cut over the whole sheet -- which was most of
    the export time on the production host.
    """
    image_format, mimetype, ext = LAYERS[layer]['export']

    buffer = io.BytesIO()
    if image_format == 'JPEG':
        image.save(buffer, format='JPEG', quality=JPEG_QUALITY, subsampling=0)
    elif source_freq:
        pinned = [c for c, _ in source_freq.most_common(PINNED_COLOURS)]
        sample = image.resize((max(1, image.width // 4), max(1, image.height // 4)),
                              Image.NEAREST)
        learned = sample.quantize(256 - len(pinned), method=Image.Quantize.FASTOCTREE,
                                  dither=Image.Dither.NONE).convert('RGB')
        colours = pinned + [c for _, c in learned.getcolors(256) if c not in source_freq]
        colours = (colours + [colours[-1]] * 256)[:256]
        palette = Image.new('P', (1, 1))
        palette.putpalette([v for c in colours for v in c])
        image.quantize(palette=palette, dither=Image.Dither.NONE).save(buffer, format='PNG')
    else:
        # No source histogram (should not happen for a PNG layer): fall back to
        # an adaptive palette over the whole sheet.
        image.convert('P', palette=Image.ADAPTIVE, colors=256,
                      dither=Image.NONE).save(buffer, format='PNG')
    buffer.seek(0)
    return buffer, mimetype, ext


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

    # Draw the scale bar background
    bg_padding = 15
    bg_left = x_left - bg_padding
    bg_top = y_top - text_height - bg_padding * 2
    bg_right = x_right + bg_padding
    bg_bottom = y_bottom + bg_padding

    # Create overlay for the scale bar background
    overlay = Image.new('RGBA', image.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    # Opaque, not translucent: blending the box over the map generated ~80 extra
    # colours and pushed the image past 256, forcing a lossy palette or a 16 MB
    # RGB PNG. Solid white also reads better on paper.
    overlay_draw.rounded_rectangle(
        [bg_left, bg_top, bg_right, bg_bottom],
        radius=8,
        fill=(255, 255, 255, 255)
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
    """Fetch a single tile from the WMS service, decoded."""
    data = fetch_wms_bytes(minx, miny, maxx, maxy, width, height, layer)
    if data is None:
        return None
    try:
        return Image.open(io.BytesIO(data))
    except Exception as e:
        print(f"Error processing tile: {e}")
        return None


def fetch_wms_bytes(minx, miny, maxx, maxy, width, height, layer='mapant'):
    """Fetch a single tile from the WMS service as the raw encoded bytes."""
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

    for attempt in range(WMS_RETRIES):
        try:
            response = requests.get(layer_config['url'], params=params,
                                    timeout=WMS_TIMEOUT)

            # Rate limiting and transient server errors are worth waiting out;
            # a big oversampled export is a burst of requests by nature.
            if response.status_code in (429, 500, 502, 503, 504):
                if attempt + 1 < WMS_RETRIES:
                    time.sleep(retry_delay(response, attempt))
                    continue

            response.raise_for_status()

            # Check if response is an image
            content_type = response.headers.get('Content-Type', '')
            if 'image' not in content_type:
                print(f"WMS error: {response.text[:500]}")
                return None

            return response.content

        except requests.RequestException as e:
            if attempt + 1 < WMS_RETRIES:
                time.sleep(retry_delay(None, attempt))
                continue
            print(f"Failed to fetch tile: {e}")
            return None

    return None


def retry_delay(response, attempt):
    """Backoff before retrying a tile, honouring Retry-After when offered."""
    if response is not None:
        header = response.headers.get('Retry-After')
        if header:
            try:
                return min(float(header), 10.0)
            except ValueError:
                pass
    return min(0.5 * (2 ** attempt), 4.0)


if __name__ == '__main__':
    app.run(debug=True, port=5000)
