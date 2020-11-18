#===============================================================================
#
#  Flatmap viewer and annotation tools
#
#  Copyright (c) 2019, 2020  David Brooks
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

from collections import defaultdict, OrderedDict
import datetime
import json
import os
import subprocess
import sys

#===============================================================================

import cv2
import numpy as np

import shapely.affinity

#===============================================================================

from flatmap.feature import Feature
from flatmap.layers import FeatureLayer

from knowledgebase.labels import LabelDatabase

from output.geojson import GeoJSONOutput
from output.mbtiles import MBTiles
from output.styling import Style
from output.tilejson import tile_json
from output.tilemaker import TileMaker

from properties import JsonProperties

from sources.powerpoint import PowerpointSource

#===============================================================================

from mapmaker import FLATMAP_VERSION

#===============================================================================

class Flatmap(object):
    def __init__(self, specification, options):
        # Check options for validity and set defaults
        if options.get('backgroundOnly', False):
            options['backgroundTiles'] = True

        min_zoom = options.get('minZoom', 2)
        max_zoom = options.get('maxZoom', 10)
        initial_zoom = options.get('initialZoom', 4)
        if min_zoom < 0 or min_zoom > max_zoom:
            raise ValueError('Min zoom must be between 0 and {}'.format(max_zoom))
        if max_zoom < min_zoom or max_zoom > 15:
            raise ValueError('Max zoom must be between {} and 15'.format(min_zoom))
        if initial_zoom < min_zoom or initial_zoom > max_zoom:
            raise ValueError('Initial zoom must be between {} and {}'.format(min_zoom, max_zoom))
        self.__zoom = (min_zoom, max_zoom, initial_zoom)

        self.__options = options

        self.__specification = specification

        try:
            self.__id = specification['id']
        except KeyError:
            raise ValueError('Map specification requires an `id` field')

        self.__models = specification.get('models')

        # Make sure our output directories exist
        map_base = options.get('mapBase', 'maps')
        if not os.path.exists(map_base):
            os.makedirs(map_base)
        self.__map_dir = os.path.join(map_base, self.__id)
        if options.get('clear', False):
            shutil.rmtree(self.__map_dir, True)
        if not os.path.exists(self.__map_dir):
            os.makedirs(self.__map_dir)

        # The vector tiles' database that is created by `tippecanoe`
        self.__mbtiles_file = os.path.join(self.__map_dir, 'index.mbtiles')

        self.__geojson_files = []
        self.__tippe_inputs = []
        self.__upload_files = []

        # Use a local database to cache labels retrieved from knowledgebase
        label_database = LabelDatabase(map_base, options.get('refreshLabels', False))

        # Properties about map features
        self.__property_data = JsonProperties(specification.get('properties'),
                                              specification.get('anatomicalMap'),
                                              label_database)

        self.__layer_dict = OrderedDict()
        self.__visible_layer_count = 0

        self.__annotations = {}
        self.__creator = 'mapmaker' ## FIX, add version info creator

        self.__map_area = None
        self.__extent = None
        self.__centre = None

        self.__last_feature_id = 0
        self.__class_to_feature = defaultdict(list)
        self.__id_to_feature = {}

    def __len__(self):
        return self.__visible_layer_count

    @property
    def extent(self):
        return self.__extent

    @property
    def id(self):
        return self.__id

    @property
    def layer_ids(self):
        return list(self.__layer_dict.keys())

    @property
    def map_directory(self):
        return self.__map_dir

    @property
    def models(self):
        return self.__models

    @property
    def options(self):
        return self.__options


    def make(self):
    #==============
        self.__begin_make()

        # Process flatmap's sources to create FeatureLayers
        self.__process_sources()

        if not self.__options.get('errorCheck', False):
            # Add high-resolution features showing details
            self.__add_details()
            # Generate metadata with connection information
            self.__resolve_paths()
            # Output all features (as GeoJSON)
            self.__output_geojson()
            # Generate vector tiles from GeoJSON
            self.__make_vector_tiles()
            # Generate image tiles
            if self.__options.get('backgroundTiles', False):
                self.__make_image_tiles()
            # Save the flatmap's metadata
            self.__save_metadata()
            # Upload the generated map to a server
            if self.__options.get('uploadHost') is not None:
                self.__upload_map(self.__options.get('uploadHost'))

        # All done so clean up
        self.__finish_make()

    def __begin_make(self):
    #======================
        self.__geojson_files = []
        self.__tippe_inputs = []
        self.__upload_files = []

    def __finish_make(self):
    #=======================
        # Show what the map is about
        if self.models:
            print('Generated map for {}'.format(flatmap.models))
        ## FIX v's errorCheck
        for filename in self.__geojson_files:
            if self.__options.get('saveGeoJSON', False):
                print(filename)
            else:
                os.remove(filename)

    def __process_sources(self):
    #===========================
        for source in self.__specification.get('sources', []):
            source_id = source.get('id')
            source_kind = source.get('kind')
            source_href = source.get('href')
            if source_kind == 'slides':
                source = PowerpointSource(self, source_id, source_href)
            elif source_kind == 'image':
                source = MBFImageSource(self, source_id, source_href)
            elif source_kind in ['base', 'details']:
                # source = SVGSource(self, source_id, source_href)
                pass
            else:
                raise ValueError('Unsupported source kind: {}'.format(source_kind))

            source.process()
            for layer in source.layers:
                self.__add_layer(layer)
            if source_kind in ['base', 'slides']:
                if self.__extent is None:
                    self.__extent = source.extent()
                    self.__centre = ((self.__extent[0] + self.__extent[2])/2,
                                     (self.__extent[1] + self.__extent[3])/2)
                    self.__map_area = source.map_area()
                else:
                    raise ValueError("Multiple 'base' and 'slides' source kinds")
        if self.__visible_layer_count == 0:
            raise ValueError('No map layers in sources...')

    def is_duplicate_feature_id(self, id):
    #=====================================
        return self.__id_to_feature.get(id, None) is not None

    def save_feature_id(self, feature):
    #==================================
        if feature.has_property('id'):
            self.__id_to_feature[feature.get_property('id')] = feature.feature_id
        if feature.has_property('class'):
            self.__class_to_feature[feature.get_property('class')].append(feature.feature_id)

    def new_feature(self, geometry, properties, has_children=False):
    #===============================================================
        self.__last_feature_id += 1
        return Feature(self.__last_feature_id, geometry, properties, has_children)

    def __add_layer(self, layer):
    #============================
        if layer.id in self.__layer_dict:
            raise KeyError('Duplicate layer id: {}'.format(layer.id))
        self.__layer_dict[layer.id] = layer
        if not layer.hidden:
            self.__visible_layer_count += 1
###            layer.add_image_layer(layer.id, slide_number, self.__zoom[0])

    def __add_details(self):
    #=======================
        # Add details of high-resolution features by adding a details layer
        # for feature with details

        ## Need image layers...
        ##
        ## have Image of slide with outline image and outline's BBOX
        ## so can get Rect with just outline. Transform to match detail's feature.
        ##
        ## set layer.__image from slide when first making??
        ## Also want image layers scaled and with minzoom set...

        print('Adding details...')
        detail_layers = []
        for layer in self.__layer_dict.values():
            if not layer.hidden and layer.detail_features:
                detail_layer = FeatureLayer('{}-details'.format(layer.id), layer.source)
                detail_layers.append(detail_layer)
                self.__add_detail_features(layer, detail_layer, layer.detail_features)
        for layer in detail_layers:
            self.__add_layer(layer)

## Put this into 'features.py' as a function??
    def __add_detail_features(self, layer, detail_layer, lowres_features):
    #=====================================================================
        extra_details = []
        for feature in lowres_features:
            hires_layer_id = '{}-{}'.format(layer.source.id, feature.get_property('details'))
            hires_layer = self.__layer_dict.get(hires_layer_id)
            if hires_layer is None:
                raise KeyError("Cannot find details' layer '{}'".format(feature.get_property('details')))

            outline_feature = hires_layer.features_with_id.get(hires_layer.outline_feature_id)
            if outline_feature is None:
                raise KeyError("Cannot find outline feature '{}'".format(hires_layer.outline_feature_id))

            # Calculate ``shapely.affinity`` 2D affine transform matrix to map source shapes to the destination

            # NOTE: We have no way of ensuring that the vertices of the source and destination rectangles
            #       align as intended. As a result, output features might be rotated by some multiple
            #       of 90 degrees.
            src = np.array(outline_feature.geometry.minimum_rotated_rectangle.exterior.coords, dtype = "float32")[:-1]
            dst = np.array(feature.geometry.minimum_rotated_rectangle.exterior.coords, dtype = "float32")[:-1]
            M = cv2.getPerspectiveTransform(src, dst)
            transform = np.concatenate((M[0][0:2], M[1][0:2], M[0][2], M[1][2]), axis=None).tolist()

            # Set the feature's geometry to that of the high-resolution outline
            feature.geometry = shapely.affinity.affine_transform(outline_feature.geometry, transform)
            minzoom = feature.get_property('maxzoom') + 1

##            layer.add_image_layer('{}-{}'.format(hires_layer.id, feature.id).replace('#', '_'),
##                                  hires_layer.slide_number,
##                                  minzoom,
##                                  bounding_box=outline_feature.geometry.bounds,
##                                  image_transform=M)

            # The detail layer gets a scaled copy of each high-resolution feature
            for hires_feature in hires_layer.features:
                new_feature = self.new_feature(shapely.affinity.affine_transform(hires_feature.geometry, transform),
                                               hires_feature.copy_properties())
                new_feature.set_property('layer', layer.id)
                new_feature.set_property('minzoom', minzoom)
                detail_layer.add_feature(new_feature)
                self.save_feature_id(new_feature)
                if new_feature.has_property('details'):
                    extra_details.append(new_feature)

        # If hires features that we've just added also have details then add them
        # to the detail layer
        if extra_details:
            self.__add_detail_features(layer, detail_layer, extra_details)

    def __make_image_tiles(self, pdf_bytes, pdf_source_name):
    #========================================================
        print('Generating background tiles (may take a while...)')
        tilemaker = TileMaker(pdf_source_name, self.__extent, self.__map_dir, self.__zoom)
        for layer in self.__layer_dict.values():
            for image_layer in layer.image_layers:
                tilemaker.start_tiles_from_pdf_process(pdf_bytes, image_layer)
        tilemaker.wait_for_processes()
        self.__upload_files.extend(tilemaker.database_names)

    def __make_vector_tiles(self, compressed=True):
    #==============================================
        # Generate Mapbox vector tiles
        if len(self.__tippe_inputs) == 0:
            raise ValueError('No selectable layers found...')

        print('Running tippecanoe...')
        tippe_command = ['tippecanoe',
                            '--force',
                            '--projection=EPSG:4326',
                            '--buffer=100',
                            '--minimum-zoom={}'.format(self.__zoom[0]),
                            '--maximum-zoom={}'.format(self.__zoom[1]),
                            '--no-tile-size-limit',
                            '--output={}'.format(self.__mbtiles_file),
                        ]
        if not compressed:
            tippe_command.append('--no-tile-compression')
        subprocess.run(tippe_command
                       + list(["-L{}".format(json.dumps(input)) for input in self.__tippe_inputs])
                      )

        # `tippecanoe` uses the bounding box containing all features as the
        # map bounds, which is not the same as the extracted bounds, so update
        # the map's metadata
        tile_db = MBTiles(self.__mbtiles_file)
        tile_db.add_metadata(compressed=compressed)
        tile_db.update_metadata(center=','.join([str(x) for x in self.__centre]),
                                bounds=','.join([str(x) for x in self.__extent]))
        tile_db.execute("COMMIT")
        tile_db.close();
        self.__upload_files.append('index.mbtiles')

    def __layer_metadata(self):
    #==========================
        metadata = []
        for layer in self.__layer_dict.values():
            if not layer.hidden:
                map_layer = {
                    'id': layer.id,
                    'description': layer.description,
                    'selectable': layer.selectable,
                    'selected': layer.selected,
                    'queryable-nodes': layer.queryable_nodes,
                    'features': layer.feature_types,
                    'image-layers': [l.id for l in layer.image_layers]
                }
## FIX ??               if layer.slide_id is not None:
## layer source v's map source v's spec info.
##                         map_layer['source'] = layer.slide_id
                metadata.append(map_layer)
        return metadata

    def __output_geojson(self):
    #==========================
        print('Outputting GeoJson features...')
        for layer in self.__layer_dict.values():
            if not layer.hidden:
                print('Layer:', layer.id)
                layer.extend_nerve_cuffs()
                geojson_output = GeoJSONOutput(layer.id, self.__map_area, self.__map_dir)
                saved_layer = geojson_output.save(layer.features, self.__options['saveGeoJSON'])
                for (layer_name, filename) in saved_layer.items():
                    self.__geojson_files.append(filename)
                    self.__tippe_inputs.append({
                        'file': filename,
                        'layer': layer_name,
                        'description': '{} -- {}'.format(layer.description, layer_name)
                    })
                self.__annotations.update(layer.annotations)

    def __resolve_paths(self):
    #=========================
        # Set feature ids of path components
        self.__property_data.resolve_pathways(self.__id_to_feature, self.__class_to_feature)

    def __save_metadata(self):
    #=========================
        print('Creating index and style files...')
        tile_db = MBTiles(self.__mbtiles_file)

        # Save path of the Powerpoint source
        ## FIX tile_db.add_metadata(source=self.__source)    ## We don't always want this updated...
                                                   ## e.g. if re-running after tile generation
        # What the map models
        if self.__models is not None:
            tile_db.add_metadata(describes=self.__models)
        # Save layer details in metadata
        tile_db.add_metadata(layers=json.dumps(self.__layer_metadata()))
        # Save pathway details in metadata
        tile_db.add_metadata(pathways=json.dumps(self.__property_data.resolved_pathways))
        # Save annotations in metadata
        tile_db.add_metadata(annotations=json.dumps(self.__annotations))
        # Save command used to run mapmaker
        tile_db.add_metadata(created_by=self.__creator)
        # Save the maps creation time
        tile_db.add_metadata(created=datetime.datetime.utcnow().isoformat())
        # Commit updates to the database
        tile_db.execute("COMMIT")

#*        ## TODO: set ``layer.properties`` for annotations...
#*        ##update_RDF(options['map_base'], options['map_id'], source, annotations)

        image_layers = []
        for layer in self.__layer_dict.values():
            image_layers.extend(layer.image_layers)

        map_index = {
            'id': self.__id,
            'min-zoom': self.__zoom[0],
            'max-zoom': self.__zoom[1],
            'bounds': self.__extent,
            'version': FLATMAP_VERSION,
            'image_layer': len(image_layers) > 0  ## For compatibility
        }
        if self.__models is not None:
            map_index['describes'] = self.__models
        # Create `index.json` for building a map in the viewer
        with open(os.path.join(self.__map_dir, 'index.json'), 'w') as output_file:
            json.dump(map_index, output_file)

        # Create style file
        metadata = tile_db.metadata()
        style_dict = Style.style(image_layers, metadata, self.__zoom)
        with open(os.path.join(self.__map_dir, 'style.json'), 'w') as output_file:
            json.dump(style_dict, output_file)

        # Create TileJSON file
        json_source = tile_json(self.__id, self.__zoom, self.__extent)
        with open(os.path.join(self.__map_dir, 'tilejson.json'), 'w') as output_file:
            json.dump(json_source, output_file)

        tile_db.close();
        self.__upload_files.extend(['index.json', 'style.json', 'tilejson.json'])

    def __upload_map(self, host):
    #============================
        upload = ' '.join([ '{}/{}'.format(self.__id, f) for f in self.__upload_files ])
        cmd_stream = os.popen('tar -C {} -c -z {} | ssh {} "tar -C /flatmaps -x -z"'
                             .format(self.__map_dir, upload, host))
        return cmd_stream.read()

#===============================================================================