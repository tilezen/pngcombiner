from __future__ import print_function

from collections import namedtuple
from cStringIO import StringIO
from enum import Enum
from PIL import Image
from time import time
import base64
import boto3
import json
import math
import os


def is_tile_valid(z, x, y):
    if z < 0 or x < 0 or y < 0:
        return False
    if z > 15:
        return False
    x_y_limit = int(math.pow(2, z))
    if x >= x_y_limit or y >= x_y_limit:
        return False
    return True


def log(request_state):
    print(json.dumps(request_state))


class Tile(object):
    """Simple container for a tile coordinate"""

    def __init__(self, z, x, y):
        assert is_tile_valid(z, x, y)
        self.z = z
        self.x = x
        self.y = y

    def __str__(self):
        return '%d/%d/%d' % (self.z, self.x, self.y)

    def __repr__(self):
        return str(self)

    def __eq__(self, that):
        return (self.z == that.z and
                self.x == that.x and
                self.y == that.y)


Tileset = Enum('Tileset', 'terrarium normal')

# TODO fetchresult can grow to contain response caching headers
FetchResult = namedtuple('FetchResult', 'image_bytes')

# image specification defines the image placement of the source in the
# final destination
# location is the PIL coordinate where the image gets pasted, ie local
# to the resulting merged image
# the crop_bounds is minx,miny,maxx,maxy and is local to the original
# image
ImageSpec = namedtuple('ImageSpec', 'location crop_bounds')

TileCoordinates = namedtuple('TileCoordinates', 'tile image_spec')
ImageInput = namedtuple('ImageInput', 'image_bytes image_spec tile')
PathParseResult = namedtuple('PathParseResult',
                             'not_found_reason tileset tilesize tile')


class MissingTileException(Exception):
    """
    Required tile missing

    Particular type of exception to represent a tile we expected to be
    there is missing
    """

    def __init__(self, tile):
        super(MissingTileException, self).__init__('Missing tile: %s' % tile)
        self.tile = tile


def invalid_parse_result(reason):
    return PathParseResult(reason, None, None, None)


def parse_apigateway_path(path):
    path_parts = path.split('/')
    try:
        tileset_name, tilesize_str, z_s, x_s, y_fmt = path_parts
    except ValueError:
        return invalid_parse_result('Invalid url path')

    y_fmt_parts = y_fmt.split('.')
    try:
        y_s, fmt = y_fmt_parts
    except ValueError:
        return invalid_parse_result('Invalid format')

    if fmt != 'png':
        return invalid_parse_result('Invalid format')

    try:
        tileset = Tileset[tileset_name]
    except KeyError:
        return invalid_parse_result('Invalid tileset')

    try:
        tilesize = int(tilesize_str)
    except ValueError:
        return invalid_parse_result('Invalid tilesize')

    if tilesize not in (260, 512, 516):
        return invalid_parse_result('Invalid tilesize')

    if tileset == Tileset.normal and tilesize != 512:
        return invalid_parse_result('Normal tiles are unbuffered')

    try:
        z = int(z_s)
        x = int(x_s)
        y = int(y_s)
    except ValueError:
        return invalid_parse_result('Invalid tile coordinate')

    tile = Tile(z, x, y)
    parse_result = PathParseResult(None, tileset, tilesize, tile)
    return parse_result


def generate_s3_key(tileset, tile):
    assert tileset in (Tileset.terrarium, Tileset.normal)
    s3_key = '%s/%s.png' % (tileset.name, tile)
    return s3_key


class S3TileFetcher(object):
    """Fetch the source tile data"""

    def __init__(self, s3_client, bucket, tileset):
        self.s3_client = s3_client
        self.bucket = bucket
        self.tileset = tileset

    def __call__(self, tile):
        s3_key = generate_s3_key(self.tileset, tile)
        try:
            resp = self.s3_client.get_object(
                Bucket=self.bucket,
                Key=s3_key,
            )
            body_file = resp['Body']
            image_bytes = body_file.read()
            body_file.close()
            # TODO caching response headers
            return FetchResult(image_bytes)
        except Exception as e:
            try:
                err_code = e.response['Error']['Code']
            except:
                err_code = ''

            if err_code == 'NoSuchKey':
                # opt to return these more specifically as an exception
                # we want to early out in all cases, but we might
                # want to know about missing tiles in particular
                raise MissingTileException(tile)
            else:
                # re-raise the original exception
                raise e


class ImageReducer(object):
    """Combine or reduce multiple source images into one"""

    def __init__(self, tilesize):
        self.tilesize = tilesize
        assert tilesize in (512, 516, 260)

    def create_initial_state(self):
        image_state = Image.new('RGB', (self.tilesize, self.tilesize))
        return image_state

    def reduce(self, image_state, image_input):
        tile_fp = StringIO(image_input.image_bytes)
        image_spec = image_input.image_spec
        image = Image.open(tile_fp)
        if image_spec.crop_bounds:
            image = image.crop(image_spec.crop_bounds)
        image_state.paste(image, image_spec.location)

    def finalize(self, image_state):
        out_fp = StringIO()
        image_state.save(out_fp, format='PNG')
        image_bytes = out_fp.getvalue()
        return image_bytes


class time_block(object):
    """Convenience to capture timing information"""

    def __init__(self, timing, metadata_key):
        self.timing = timing
        self.metadata_key = metadata_key
        self.start = None

    def __enter__(self):
        self.start = time()

    def __exit__(self, exc_type, exc_val, exc_tb):
        stop = time()
        duration = stop - self.start
        self.timing[self.metadata_key] = duration
        suppress_exception = False
        return suppress_exception


def img_pos(x, y):
    pos = x, y
    crop = None
    return ImageSpec(pos, crop)


def generate_coordinates_512(tile):
    zp1 = tile.z + 1
    dbl_x = tile.x * 2
    dbl_y = tile.y * 2
    # see ImageSpec description above for coordinate meaning

    tile_coordinates = (
        TileCoordinates(Tile(zp1, dbl_x, dbl_y), img_pos(0, 0)),
        TileCoordinates(Tile(zp1, dbl_x+1, dbl_y), img_pos(256, 0)),
        TileCoordinates(Tile(zp1, dbl_x, dbl_y+1), img_pos(0, 256)),
        TileCoordinates(Tile(zp1, dbl_x+1, dbl_y+1), img_pos(256, 256)),
    )
    return tile_coordinates


def generate_coordinates_260(tile):
    """
    generate a 3x3 grid with the source tile in the center

    x x x
    x o x
    x x x

    """

    # see ImageSpec description above for coordinate meaning

    tile_coordinates = []

    x_y_max = int(math.pow(2, tile.z)) - 1

    # NOTE: using a north, east, south, west naming scheme
    # top row placement positions
    loc_nw, loc_n, loc_ne = (0, 0), (2, 0), (258, 0)
    # mid row placement positions
    loc_w, loc_c, loc_e = (0, 2), (2, 2), (258, 2)
    # bot row placement positions
    loc_sw, loc_s, loc_se = (0, 258), (2, 258), (258, 258)

    # set the top row tiles to account for edge cases
    top_y = 0 if tile.y == 0 else tile.y-1
    if tile.x == 0:
        nw_tile = Tile(tile.z, x_y_max, top_y)
    else:
        nw_tile = Tile(tile.z, tile.x-1, top_y)
    n_tile = Tile(tile.z, tile.x, top_y)
    if tile.x == x_y_max:
        ne_tile = Tile(tile.z, 0, top_y)
    else:
        ne_tile = Tile(tile.z, tile.x+1, top_y)

    # set the mid row of tiles
    if tile.x == 0:
        w_tile = Tile(tile.z, x_y_max, tile.y)
    else:
        w_tile = Tile(tile.z, tile.x-1, tile.y)
    c_tile = Tile(tile.z, tile.x, tile.y)
    if tile.x == x_y_max:
        e_tile = Tile(tile.z, 0, tile.y)
    else:
        e_tile = Tile(tile.z, tile.x+1, tile.y)

    # set the bot row of tiles
    bot_y = x_y_max if tile.y == x_y_max else tile.y+1
    if tile.x == 0:
        sw_tile = Tile(tile.z, x_y_max, bot_y)
    else:
        sw_tile = Tile(tile.z, tile.x-1, bot_y)
    s_tile = Tile(tile.z, tile.x, bot_y)
    if tile.x == x_y_max:
        se_tile = Tile(tile.z, 0, bot_y)
    else:
        se_tile = Tile(tile.z, tile.x+1, bot_y)

    # relevant tiles are set appropriately
    # now we need to figure out the parts that are cropped from each
    # if we are the top or bot, we need to invert the piece that gets cropped
    if tile.y == 0:
        # the tiles will be set to be the top row
        # we'll be extracting the top bounds from these
        top_crop_bounds = (
            (254, 0, 256, 2),
            (0, 0, 256, 2),
            (0, 0, 2, 2),
        )
    else:
        # we are not the top row
        # we'll be extracting the bot bounds from the row above us
        top_crop_bounds = (
            (254, 254, 256, 256),
            (0, 254, 256, 256),
            (0, 254, 2, 256),
        )
    mid_crop_bounds = (
        (254, 0, 256, 256),
        None,
        (0, 0, 2, 256),
    )
    if tile.y == x_y_max:
        # the tiles will be set to the bot row
        # we'll be extrating the bot bounds from these
        bot_crop_bounds = (
            (254, 254, 256, 256),
            (0, 254, 256, 256),
            (0, 254, 2, 256),
        )
    else:
        # we are not the bot row
        # we'll be extracting the top bounds from the row below us
        bot_crop_bounds = (
            (254, 0, 256, 2),
            (0, 0, 256, 2),
            (0, 0, 2, 2),
        )

    # the tiles, locations, and bounds are now all assembled
    # weave them together to generate the list of all tile coordinates
    all_tiles = (
        nw_tile, n_tile, ne_tile,
        w_tile, c_tile, e_tile,
        sw_tile, s_tile, se_tile,
    )
    all_locs = (
        loc_nw, loc_n, loc_ne,
        loc_w, loc_c, loc_e,
        loc_sw, loc_s, loc_se,
    )
    all_bounds = (list(top_crop_bounds) +
                  list(mid_crop_bounds) +
                  list(bot_crop_bounds))

    for tile, loc, crop_bounds in zip(all_tiles, all_locs, all_bounds):
        tc = TileCoordinates(tile, ImageSpec(loc, crop_bounds))
        tile_coordinates.append(tc)

    return tile_coordinates


def generate_coordinates_516(tile):
    """
    generate a 4x4 grid with the source tiles being the 4 in the middle

    The source tile is zoomed in one, which generates 4 tiles. Then
    the border around these 4 is used.

    x x x x
    x O o x
    x o o x
    x x x x

    """

    tile_coordinates = []

    # pre-bump the coordinates to the next highest zoom
    z = tile.z + 1
    x = tile.x * 2
    y = tile.y * 2

    x_y_max = int(math.pow(2, z)) - 1

    # see ImageSpec description above for coordinate meaning

    # NOTE: using a row/col scheme to organize the values

    # these are the origin locations where the images will be placed
    locations = (
        # first row
        (0, 0), (2, 0), (258, 0), (514, 0),
        # second row
        (0, 2), (2, 2), (258, 2), (514, 2),
        # third row
        (0, 258), (2, 258), (258, 258), (514, 258),
        # fourth row
        (0, 514), (2, 514), (258, 514), (514, 514),
    )

    # set the row tiles to account for edge cases
    tiles = []
    for y_iter in xrange(y-1, y+3):

        if y_iter < 0:
            y_val = 0
        elif y_iter > x_y_max:
            y_val = x_y_max
        else:
            y_val = y_iter

        for x_iter in xrange(x-1, x+3):

            x_val = x_iter
            if x_iter < 0:
                x_val = x_y_max
            elif x_iter > x_y_max:
                x_val = 0

            tiles.append(Tile(z, x_val, y_val))

    assert(len(tiles) == 16)

    # set the crop bounds for each
    if y == 0:
        top_row_crop_bounds = (
            (254, 0, 256, 2),
            (0, 0, 256, 2),
            (0, 0, 256, 2),
            (0, 0, 2, 2),
        )
    else:
        top_row_crop_bounds = (
            (254, 254, 256, 256),
            (0, 254, 256, 256),
            (0, 254, 256, 256),
            (0, 254, 2, 256),
        )
    mid_rows_crop_bounds = (
        (254, 0, 256, 256),
        None,
        None,
        (0, 0, 2, 256),
    )
    if y+1 == x_y_max:
        bot_row_crop_bounds = (
            (254, 254, 256, 256),
            (0, 254, 256, 256),
            (0, 254, 256, 256),
            (0, 254, 2, 256),
        )
    else:
        bot_row_crop_bounds = (
            (254, 0, 256, 2),
            (0, 0, 256, 2),
            (0, 0, 256, 2),
            (0, 0, 2, 2),
        )

    all_crop_bounds = (
        list(top_row_crop_bounds) +
        list(mid_rows_crop_bounds) +
        list(mid_rows_crop_bounds) +
        list(bot_row_crop_bounds))

    for tile, loc, crop_bounds in zip(tiles, locations, all_crop_bounds):
        tc = TileCoordinates(tile, ImageSpec(loc, crop_bounds))
        tile_coordinates.append(tc)

    return tile_coordinates


def lambda_handler(event, context):
    request_state = {}
    request_state['timing'] = timing = {}

    with time_block(timing, 'total'):

        # NOTE: these need to be set as staging variables in the
        # apigateway integration request mapping
        request_state['env'] = event['env']
        bucket = event['bucket']

        status = 200
        # these will get set conditionally based on the path
        # and status will be updated appropriately
        reason_not_found = ''
        reason_error = ''
        response = ''

        # captures the state of the requested path, and the more
        # specific parts as parse continues
        request_state['request'] = {}

        # TODO update this once the apigateway change is made
        # path = request_state['request']['path'] = event['path']
        path = request_state['request']['path'] = event['tile']

        parse_result = parse_apigateway_path(path)
        if parse_result.not_found_reason:
            reason_not_found = parse_result.not_found_reason
            status = 404
        else:
            tile = parse_result.tile
            request_state['request']['tile'] = dict(
                z=tile.z,
                x=tile.x,
                y=tile.y,
            )
            tileset = parse_result.tileset
            request_state['request']['tileset'] = tileset.name
            tilesize = parse_result.tilesize
            request_state['request']['tilesize'] = tilesize

            s3_client = boto3.client('s3')
            tile_fetcher = S3TileFetcher(s3_client, bucket, tileset)
            image_reducer = ImageReducer(tilesize)

            if tilesize == 512:
                # both terrarium and normal tiles follow the same
                # coordinate generation strategy. They just point to a
                # different location for the source data
                coords_generator = generate_coordinates_512
            elif tileset == Tileset.terrarium and tilesize == 260:
                coords_generator = generate_coordinates_260
            elif tileset == Tileset.terrarium and tilesize == 516:
                coords_generator = generate_coordinates_516
            else:
                assert not 'tileset/tilesize combination unimplemented: ' \
                           '%s %s' % (tileset, tilesize)

            try:
                image_bytes, timing_metadata = process_tile(
                    coords_generator, tile_fetcher, image_reducer, tile)
                request_state['timing'].update(timing_metadata)

                # base 64 encode the response
                with time_block(timing, 'b64'):
                    response = base64.b64encode(image_bytes)

            except Exception as e:
                if isinstance(e, MissingTileException):
                    request_state['missing_tile'] = dict(
                        z=e.tile.z,
                        x=e.tile.x,
                        y=e.tile.y,
                    )
                status = 500
                # this logs any unexpected exceptions to the log, and will
                # provide a stack trace
                import traceback
                traceback.print_exc()
                reason_error = str(e)

    # finalize request state and log it
    request_state['status'] = status
    if status == 404:
        request_state['error'] = reason_not_found
    elif status == 500:
        request_state['error'] = reason_error

    log(request_state)

    # return the appropriate response
    if status == 200:
        return response
    else:
        if status == 404:
            err = 'Not Found: %s' % reason_not_found
        else:
            err = 'Internal Server Error: %s' % reason_error
        raise Exception(err)


def process_tile(coords_generator, tile_fetcher, image_reducer, tile):
    all_tile_coords = coords_generator(tile)

    timing_fetch = {}
    timing_process = {}
    timing_metadata = dict(
        fetch=timing_fetch,
        process=timing_process,
    )

    image_inputs = []

    # TODO support cache headers for 304 responses?
    with time_block(timing_fetch, 'total'):
        for tile_coords in all_tile_coords:
            tile = tile_coords.tile
            with time_block(timing_fetch, str(tile)):
                fetch_result = tile_fetcher(tile)

            image_input = ImageInput(
                fetch_result.image_bytes, tile_coords.image_spec, tile)
            image_inputs.append(image_input)

    with time_block(timing_process, 'total'):
        image_state = image_reducer.create_initial_state()
        for image_input in image_inputs:
            with time_block(timing_process, str(image_input.tile)):
                image_reducer.reduce(image_state, image_input)

    with time_block(timing_metadata, 'save'):
        image_bytes = image_reducer.finalize(image_state)

    return image_bytes, timing_metadata
