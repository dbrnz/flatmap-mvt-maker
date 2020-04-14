#===============================================================================
#
#  Flatmap viewer and annotation tools
#
#  Copyright (c) 2019  David Brooks
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
#===============================================================================

import json
import math
import os

#===============================================================================

# https://simoncozens.github.io/beziers.py/index.html
from beziers.cubicbezier import CubicBezier
from beziers.point import Point as BezierPoint
from beziers.quadraticbezier import QuadraticBezier

import numpy as np

import pyproj
import shapely.geometry
import shapely.ops
import shapely.prepared

#===============================================================================

from .arc_to_bezier import cubic_beziers_from_arc, tuple2
from .extractor import Feature, Extractor, Layer, Transform
from .extractor import ellipse_point
from .formula import Geometry, radians
from .presets import DML

#===============================================================================

METRES_PER_EMU = 0.1   ## This to become a command line parameter...
                       ## Or in a specification file...


#===============================================================================

mercator_transformer = pyproj.Transformer.from_proj(
                            pyproj.Proj(init='epsg:3857'),
                            pyproj.Proj(init='epsg:4326'))

def mercator_transform(geometry):
#================================
    return shapely.ops.transform(mercator_transformer.transform, geometry)

#===============================================================================

def transform_point(transform, point):
#=====================================
    return (transform@[point[0], point[1], 1.0])[:2]

def transform_bezier_samples(transform, bz):
#===========================================
    samples = 100
    return [transform_point(transform, (pt.x, pt.y)) for pt in bz.sample(samples)]

def extend_(p0, p1, delta):
#==========================
    """
    Extend the line through `p0` and `p1` by `delta`
    and return the new end point
    """
    dx = p1[0] - p0[0]
    dy = p1[1] - p0[1]
    l = math.sqrt(dx*dx + dy*dy)
    scale = (delta + l)/l
    return (p0[0] + scale*dx, p0[1] + scale*dy)

def extend_line(geometry, delta):
#================================
    if geometry.geom_type != 'LineString':
        return geometry
    coords = list(geometry.coords)
    if len(coords) == 2:
        return shapely.geometry.LineString([extend_(coords[1], coords[0], delta),
                                            extend_(coords[0], coords[1], delta)])
    else:
        coords[0] = extend_(coords[1], coords[0], delta)
        coords[-1] = extend_(coords[-2], coords[-1], delta)
        return shapely.geometry.LineString(coords)

#===============================================================================

class GeoJsonLayer(Layer):
    def __init__(self, extractor, slide, slide_number):
        super().__init__(extractor, slide, slide_number)
        self._geo_collection = {}
        self._geo_features = []
        self._region_id = 10000
        self._transform = extractor.transform

    def process(self):
    #=================
        self._geo_features = []

        features = self.process_shape_list(self._slide.shapes, self._transform)
        self.add_geo_features_(features)

        self._geo_collection = {
            'type': 'FeatureCollection',
            'id': self.layer_id,
            'creator': 'mapmaker',        # Add version
            'features': self._geo_features,
            'properties': {
                'id': self.layer_id,
                'description': self.description
            }
        }

    def save(self, filename=None):
    #=============================
        if filename is None:
            filename = os.path.join(self.settings.output_dir, '{}.json'.format(self.layer_id))
        with open(filename, 'w') as output_file:
            json.dump(self._geo_collection, output_file)

    def process_group(self, group, transform):
    #=========================================
        features = self.process_shape_list(group.shapes, transform@Transform(group).matrix())
        return self.add_geo_features_(features)

    def add_geo_features_(self, features):
    #===============================================
        divided_area = 0
        child_properties = {'layer': self.layer_id}

        group_features = []
        output_group_feature = False
        group_properties = {'layer': self.layer_id}
        for feature in features:
            if feature.properties.get('boundary', False):
                divided_area += feature.geometry.area
            elif feature.properties.get('children', False):
                child_properties.update(feature.properties)
            elif feature.properties.get('group', False):
                group_properties.update(feature.properties)
                output_group_feature = True
            else:
                group_features.append(feature)

        if divided_area > 0:
            group_features = []
            boundaries = []
            dividers = []
            regions = []
            tolerance = 0.02*math.sqrt(divided_area)  # Scale with size of divided region
            for feature in features:
                if feature.properties.get('region', False):
                    regions.append(Feature(feature.id, feature.geometry.representative_point(), feature.properties))
                elif (feature.geometry.geom_type == 'LineString'
                 and (feature.properties.get('annotation', '') == ''
                   or feature.properties.get('boundary', False))):
                    longer_line = extend_line(feature.geometry, tolerance)
                    dividers.append(longer_line)
                    group_features.append(feature)
                elif (feature.geometry.geom_type == 'Polygon'
                 and (feature.properties.get('annotation', '') == ''
                   or feature.properties.get('boundary', False))):
                    dividers.append(feature.geometry.boundary)
                    # Only show divider if not flagged as invisible
                    if not feature.properties.get('invisible', False):
                        group_features.append(Feature(self._region_id,
                                                      feature.geometry.boundary,
                                                      {'layer': self.layer_id} ))
                        self._region_id += 1
                    if feature.properties.get('boundary', False):
                        group_features.append(feature)
                elif not feature.properties.get('group', False):
                    group_features.append(feature)
            if dividers:
                for polygon in shapely.ops.polygonize(shapely.ops.unary_union(dividers)):
                    prepared_polygon = shapely.prepared.prep(polygon)
                    region_id = None
                    region_properties = child_properties.copy()
                    for region in filter(lambda p: prepared_polygon.contains(p.geometry), regions):
                        region_id = region.id
                        region_properties.update(region.properties)
                    if region_id is None:
                        region_id = self._region_id
                        self._region_id += 1
                    group_features.append(Feature(region_id, polygon, region_properties))

        for feature in group_features:
            unique_id = '{}-{}'.format(self.slide_id, feature.id)

            if feature.geometry is not None and not feature.is_group:
                geometry = feature.geometry
                mercator_geometry = mercator_transform(geometry)
                # Initial set of properties come from ``.group``
                properties = child_properties.copy()
                # And are overriden by feature specific ones
                properties.update(feature.properties)

                geojson = {
                    'type': 'Feature',
                    'id': feature.id,   # Must be numeric for tipeecanoe
                    'geometry': shapely.geometry.mapping(mercator_geometry),
                    'properties': {
                        'id': unique_id,
                        'bounds': mercator_geometry.bounds,
                        'area': geometry.area,
                        # Also set 'centre' ??
                        'length': geometry.length,
                    }
                }
                if properties:
                    for (key, value) in properties.items():
                        if key not in ['boundary', 'children', 'group', 'layer', 'region']:
                            geojson['properties'][key] = value
                    properties['geometry'] = geojson['geometry']['type']
                    self._annotations[unique_id] = properties

                self._geo_features.append(geojson)
                self._map_features.append({
                    'id': unique_id,
                    'type': geojson['geometry']['type']
                })


    def process_shape(self, shape, transform):
    #=========================================
    ##
    ## Returns shape's geometry as `shapely` object.
    ##
        coordinates = []
        pptx_geometry = Geometry(shape)
        for path in pptx_geometry.path_list:
            bbox = (shape.width, shape.height) if path.w is None or path.h is None else (path.w, path.h)
            T = transform@Transform(shape, bbox).matrix()

            moved = False
            first_point = None
            current_point = None
            closed = False

            for c in path.getchildren():
                if   c.tag == DML('arcTo'):
                    wR = pptx_geometry.attrib_value(c, 'wR')
                    hR = pptx_geometry.attrib_value(c, 'hR')
                    stAng = radians(pptx_geometry.attrib_value(c, 'stAng'))
                    swAng = radians(pptx_geometry.attrib_value(c, 'swAng'))
                    p1 = ellipse_point(wR, hR, stAng)
                    p2 = ellipse_point(wR, hR, stAng + swAng)
                    pt = (current_point[0] - p1[0] + p2[0],
                          current_point[1] - p1[1] + p2[1])
                    large_arc_flag = 1 if swAng >= math.pi else 0
                    beziers = cubic_beziers_from_arc(tuple2(wR, hR), 0, large_arc_flag, 1,
                                                     tuple2(*current_point), tuple2(*pt))
                    for bz in beziers:
                        coordinates.extend(transform_bezier_samples(T, bz))
                    current_point = pt

                elif c.tag == DML('close'):
                    if first_point is not None and current_point != first_point:
                        coordinates.append(transform_point(T, first_point))
                    closed = True
                    first_point = None
                    # Close current pptx_geometry and start a new one...

                elif c.tag == DML('cubicBezTo'):
                    coords = [BezierPoint(*current_point)]
                    for p in c.getchildren():
                        pt = pptx_geometry.point(p)
                        coords.append(BezierPoint(*pt))
                        current_point = pt
                    bz = CubicBezier(*coords)
                    coordinates.extend(transform_bezier_samples(T, bz))

                elif c.tag == DML('lnTo'):
                    pt = pptx_geometry.point(c.pt)
                    if moved:
                        coordinates.append(transform_point(T, current_point))
                        moved = False
                    coordinates.append(transform_point(T, pt))
                    current_point = pt

                elif c.tag == DML('moveTo'):
                    pt = pptx_geometry.point(c.pt)
                    if first_point is None:
                        first_point = pt
                    current_point = pt
                    moved = True

                elif c.tag == DML('quadBezTo'):
                    coords = [BezierPoint(*current_point)]
                    for p in c.getchildren():
                        pt = pptx_geometry.point(p)
                        coords.append(BezierPoint(*pt))
                        current_point = pt
                    bz = QuadraticBezier(*coords)
                    coordinates.extend(transform_bezier_samples(T, bz))

                else:
                    print('Unknown path element: {}'.format(c.tag))


        return (shapely.geometry.Polygon(coordinates) if closed
           else shapely.geometry.LineString(coordinates))

#===============================================================================

class GeoJsonExtractor(Extractor):
    def __init__(self, pptx, settings):
        super().__init__(pptx, settings)
        self._LayerClass = GeoJsonLayer
        self._transform = np.array([[METRES_PER_EMU,               0, 0],
                                    [             0, -METRES_PER_EMU, 0],
                                    [             0,               0, 1]])@np.array([[1, 0, -self._slide_size[0]/2.0],
                                                                                     [0, 1, -self._slide_size[1]/2.0],
                                                                                     [0, 0,                      1.0]])
    @property
    def transform(self):
        return self._transform

    def bounds(self):
    #================
        bounds = super().bounds()
        top_left = mercator_transformer.transform(*transform_point(self._transform, (bounds[0], bounds[1])))
        bottom_right = mercator_transformer.transform(*transform_point(self._transform, (bounds[2], bounds[3])))
        return [top_left[0], top_left[1], bottom_right[0], bottom_right[1]]

#===============================================================================