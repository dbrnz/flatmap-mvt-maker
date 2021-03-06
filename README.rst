========
Overview
========

Mapmaker is a Python application for generating `Mapbox <https://www.mapbox.com/>`_ compatible tilesets from a range of sources, currently Powerpoint slides, SVG diagrams, and segmented image files from MBF Biosciences.

Requirements
------------

* Python 3.8 with `pipenv <https://pipenv.pypa.io/en/latest/#install-pipenv-today>`_.
* `Tippecanoe <https://github.com/mapbox/tippecanoe#installation>`_.

Installation
------------

* Download the latest released Python wheel from https://github.com/dbrnz/flatmap-maker/releases/latest, currently ``mapmaker-1.0.0b1-py3-none-any.whl``.
* Create a directory in which to install ``mapmaker`` and change into it.

::

    $ pipenv install mapmaker-1.0.0b1-py3-none-any.whl

Running
-------

Command line help::

    $ pipenv run python -m mapmaker --help

::

    usage: mapmaker [-h] [-v]
                    [--log LOG_FILE] [-q] [--silent]
                    [--clean] [--background-tiles]
                    [--check-errors] [--save-beziers] [--save-drawml] [--save-geojson] [--tippecanoe]
                    [--initialZoom N] [--max-zoom N] [--min-zoom N]
                    [--refresh-labels] [--upload USER@SERVER]
                    --output OUTPUT --source SOURCE

    Generate a flatmap from its source manifest.

    optional arguments:
      -h, --help            show this help message and exit
      -v, --version         show program's version number and exit

    logging:
      --log LOG_FILE        append messages to a log file
      -q, --quiet           don't show progress bars
      --silent              suppress all messages to screen

    image tiling:
      --clean               Remove all files from generated map's directory before generating new map
      --background-tiles    generate image tiles of map's layers (may take a while...)

    diagnostics:
      --check-errors        check for errors without generating a map
      --save-beziers        Save Bezier curve segments as a feature property
      --save-drawml         save a slide's DrawML for debugging
      --save-geojson        Save GeoJSON files for each layer
      --tippecanoe          Show command used to run Tippecanoe

    zoom level:
      --initialZoom N       initial zoom level (defaults to 4)
      --max-zoom N          maximum zoom level (defaults to 10)
      --min-zoom N          minimum zoom level (defaults to 2)

    miscellaneous:
      --refresh-labels      Clear the label text cache before map making
      --upload USER@SERVER  Upload generated map to server

    required arguments:
      --output OUTPUT       base directory for generated flatmaps
      --source SOURCE       URL or directory path containing a flatmap manifest

For instance::

    $ pipenv run python -m mapmaker --output ./flatmaps   \
                                    --source ../PMR/rat
::

    Mapmaker 0.11.0.b4
    100%|█████████████████████████▉| 678/679
     98%|███████████████████████████▌| 65/66
    Adding details...
    Outputting GeoJson features...
    Layer: whole-rat
    100%|████████████████████████| 2477/2477
    Layer: whole-rat_details
    100%|██████████████████████████| 180/180
    Running tippecanoe...
    2657 features, 6439698 bytes of geometry, 25397 bytes of separate metadata, 485295 bytes of string pool
      99.9%  10/528/531
    Creating index and style files...
    Generated map for NCBITaxon:10114

Manifest files
--------------

The sources of a flatmap are specified using a JSON file called ``manifest.json``.

The manifest is a JSON dictionary that MUST specify:

* an ``id`` for the flatmap.
* a list of ``sources``.

It MAY optionally specify:

* a taxon identifier specifying what the flatmap ``models`.
* the name of a ``properties`` JSON file specifying properties of features.
* the name of an ``anatomicalMap`` file assigning anatomical identifiers to features.

A source is a JSON dictionary that MUST specify:

* the ``id`` of the source.
* the source ``kind``.
* an ``href`` giving the location of the source. If the href is relative then it is with respect to the location of the manifest file.

Valid source kinds are:

* ``slides`` -- a set of Powerpoint slides, with the first slide being the base map and subsequent slides providing details for features.
* ``base`` -- a SVG file defining a base map.
* ``details`` -- a SVG file providing details for a feature.
* ``image`` -- a segmented MBF Biosciences image file providing details for a feature

An image source MUST also specify:

* ``boundary`` -- the id of an image feature that defines the image's boundary.

For example::

    {
        "id": "whole-rat",
        "models": "NCBITaxon:10114",
        "anatomicalMap": "anatomical_map.xlsx",
        "properties": "rat_flatmap_properties.json",
        "sources": [
            {
                "id": "whole-rat",
                "href": "whole-rat.svg",
                "kind": "base"
            },
            {
                "id": "tissue-slide",
                "href": "tissue-slide.svg",
                "kind": "details"
            },
            {
                "id": "vagus",
                "href": "sub-10_sam-1_P10-1MergeMask.xml",
                "kind": "image",
                "boundary": "http://purl.org/sig/ont/fma/fma5731"
            }
        ]
    }
