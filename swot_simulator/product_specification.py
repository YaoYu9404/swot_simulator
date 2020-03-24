# Copyright (c) 2020 CNES/JPL
#
# All rights reserved. Use of this source code is governed by a
# BSD-style license that can be found in the LICENSE file.
"""
Parse/Load the product specification
====================================
"""
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union
import datetime
import copy
import collections
import logging
import os
import pathlib
import netCDF4
import numpy as np
import xarray as xr
import xml.etree.ElementTree as xt
from . import orbit_propagator
from . import math

LOGGER = logging.getLogger(__name__)

REFERENCE = "Gaultier, L., C. Ubelmann, and L.-L. Fu, 2016: The " \
    "Challenge of Using Future SWOT Data for Oceanic Field Reconstruction." \
    " J. Atmos. Oceanic Technol., 33, 119-126, doi:10.1175/jtech-d-15-0160" \
    ".1. http://dx.doi.org/10.1175/JTECH-D-15-0160.1."

GROUP = dict(
    basic=dict(
        description="Provides corrected sea surface height (SSH), sea surface "
        "height anomaly (SSHA), flags to indicate data quality, geophysical "
        "reference fields, and height-correction information on a 2 km "
        "geographically fixed grid."),
    windwave=dict(
        description="Provides measured significant wave height (SWH), "
        "normalized radar cross section (NRCS or backscatter cross section or "
        "sigma0), wind speed derived from sigma0 and SWH, model information "
        "on wind and waves, and quality flags on a 2 km geographically fixed "
        "grid."),
    expert=dict(
        description="Includes copies of the Basic and the Wind and Wave files "
        "plus more detailed information on instrument and environmental "
        "corrections, radiometer data, and geophysical models on a 2 km "
        "geographically fixed grid."))


def _find(element: xt.Element, tag: str) -> xt.Element:
    result = element.find(tag)
    if result is None:
        raise RuntimeError("The XML tag '" + tag + "' doesn't exist")
    return result


def _parse_type(dtype, width, signed):
    if dtype == "real":
        return getattr(np, "float" + width)
    elif dtype == "integer":
        return getattr(np, ("u" if not signed else "") + "int" + width)
    elif dtype == "string":
        return np.str
    elif dtype == "char":
        return np.dtype(f"S{width}")
    raise ValueError("Data type '" + dtype + "' is not recognized.")


def _cast_to_dtype(attr_value: Union[int, float], properties: Dict[str, str]):
    return getattr(np, properties["dtype"])(attr_value)


def global_attributes(attributes: Dict[str, Dict[str, Any]], cycle_number: int,
                      pass_number: int, date: np.ndarray, lng: np.ndarray,
                      lat: np.ndarray) -> Dict[str, Any]:
    def _iso_date(date: np.datetime64) -> str:
        return datetime.datetime.utcfromtimestamp(
            date.astype("datetime64[us]").astype("int64") *
            1e-6).isoformat() + "Z"

    def _iso_duration(timedelta: np.timedelta64) -> str:
        seconds = timedelta.astype("timedelta64[s]").astype("int64")
        hours = seconds // 3600
        seconds -= hours * 3600
        minutes = seconds // 60
        seconds -= minutes * 60

        result = ""
        if hours:
            result += f"{hours}H"
        if minutes or result:
            result += f"{minutes}M"
        result += f"{seconds}S"
        return "P" + result

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S : Creation")

    ellipsoid_semi_major_axis = _cast_to_dtype(
        1, attributes["ellipsoid_semi_major_axis"])
    ellipsoid_flattening = _cast_to_dtype(
        0, attributes["ellipsoid_semi_major_axis"])

    result = collections.OrderedDict({
        "Conventions":
        "CF-1.7",
        "title":
        attributes["title"]["attrs"]["description"],
        "institution":
        "CNES/JPL",
        "source":
        "Simulate product",
        "history":
        now,
        "platform":
        "SWOT",
        "references":
        REFERENCE,
        "reference_document":
        "D-56407_SWOT_Product_Description_L2_LR_SSH",
        "contact":
        "CNES aviso@altimetry.fr, JPL podaac@podaac.jpl.nasa.gov",
        "cycle_number":
        _cast_to_dtype(cycle_number, attributes["cycle_number"]),
        "pass_number":
        _cast_to_dtype(pass_number, attributes["pass_number"]),
        "time_coverage_start":
        _iso_date(date[0]),
        "time_coverage_end":
        _iso_date(date[-1]),
        "time_coverage_duration":
        _iso_duration(date[-1] - date[0]),
        "time_coverage_resolution":
        "P1S",
        "geospatial_lon_min":
        lng.min(),
        "geospatial_lon_max":
        lng.max(),
        "geospatial_lat_min":
        lat.min(),
        "geospatial_lat_max":
        lat.max()
    })
    if len(lng.shape) == 2:
        result.update({
            "left_first_longitude": lng[0, 0],
            "left_first_latitude": lat[0, 0],
            "left_last_longitude": lng[-1, 0],
            "left_last_latitude": lat[-1, 0],
            "right_first_longitude": lng[0, -1],
            "right_first_latitude": lat[0, -1],
            "right_last_longitude": lng[-1, -1],
            "right_last_latitude": lat[-1, -1],
        })
    result.update({
        "wavelength":
        _cast_to_dtype(0.008385803020979, attributes["wavelength"]),
        "orbit_solution":
        "POE",
        "ellipsoid_semi_major_axis":
        ellipsoid_semi_major_axis,
        "ellipsoid_flattening":
        ellipsoid_flattening,
        "standard_name_vocabulary":
        "CF Standard Name Table vNN",
    })
    for item in attributes:
        if item.startswith("xref_input"):
            result[item] = "N/A"
    return result


def _strtobool(value: str) -> bool:
    value = value.lower()
    if value == "true":
        return True
    elif value == "false":
        return False
    raise ValueError(f"invalid truth value {value!r}")


def _parser(tree: xt.ElementTree):
    variables = dict()
    attributes = dict()
    shapes = dict()

    for item in tree.getroot().findall("shape"):
        dims = [dim.attrib["name"] for dim in item.findall("dimension")]
        if dims:
            shapes[item.attrib["name"]] = tuple(dims)

    for item in _find(_find(tree.getroot(), 'science'), 'nodes'):
        dtype = _parse_type(
            item.tag, item.attrib["width"],
            _strtobool(item.attrib["signed"])
            if "signed" in item.attrib else None)
        if not isinstance(dtype, np.dtype):
            dtype = dtype.__name__
        annotation = item.find("annotation")
        if annotation is None:
            continue
        varname = item.attrib["name"]
        if varname.startswith("/@"):
            attributes[varname[2:]] = dict(attrs=annotation.attrib,
                                           dtype=dtype)
        else:
            del annotation.attrib["app"]
            variables[varname[1:]] = dict(attrs=annotation.attrib,
                                          dtype=dtype,
                                          shape=shapes[item.attrib["shape"]])

    return variables, attributes


def _create_variable_args(encoding: Dict[str, Dict], name: str,
                          variable: xr.Variable) -> Tuple[str, Dict[str, Any]]:
    """Initiation of netCDF4.Dataset.createVariable method parameters from
    user-defined encoding information.
    """
    kwargs = dict()
    keywords = encoding[name] if name in encoding else dict()
    if "_FillValue" in keywords:
        keywords["fill_value"] = keywords.pop("_FillValue")
    dtype = keywords.pop("dtype", variable.dtype)
    for key, value in dict(zlib=True,
                           complevel=4,
                           shuffle=True,
                           fletcher32=False,
                           contiguous=False,
                           chunksizes=None,
                           endian='native',
                           least_significant_digit=None,
                           fill_value=None).items():
        kwargs[key] = keywords.pop(key, value)
    return dtype, kwargs


def _create_variable(dataset: netCDF4.Dataset,
                     encoding: Dict[str, Dict[str, Dict[str, Any]]], name: str,
                     variable: xr.Variable) -> None:
    """Creation and writing of the NetCDF variable"""
    variable.attrs.pop("_FillValue", None)
    dtype, kwargs = _create_variable_args(encoding, name, variable)
    if np.issubdtype(dtype, np.datetime64):
        dtype = np.int64
        variable.values = variable.values.astype("datetime64[us]").astype(
            "int64")
        assert (variable.attrs["units"] ==
                "microseconds since 2000-01-01 00:00:00.0")
        # 946684800000000 number of microseconds between 2000-01-01 and
        # 1970-01-01
        variable.values -= 946684800000000

    parts = name.split("/")
    name = parts.pop()
    group = parts.pop() if parts else None

    if group is not None:
        if group not in dataset.groups:
            dataset = dataset.createGroup(group)
            if group in GROUP:
                dataset.setncatts(GROUP[group])
        else:
            dataset = dataset.groups[group]
    ncvar = dataset.createVariable(name, dtype, variable.dims, **kwargs)
    ncvar.setncatts(variable.attrs)
    values = variable.values
    if kwargs['fill_value'] is not None:
        if values.dtype.kind == "f" and np.any(np.isnan(values)):
            values[np.isnan(values)] = kwargs['fill_value']
        values = np.ma.array(values, mask=values == kwargs['fill_value'])
    dataset[name][:] = values


def to_netcdf(dataset: xr.Dataset,
              path: Union[str, pathlib.Path],
              encoding: Optional[Dict[str, Dict]] = None,
              unlimited_dims: Optional[List[str]] = None,
              **kwargs):
    """Write dataset contents to a netCDF file"""
    encoding = encoding or dict()
    unlimited_dims = unlimited_dims or list()

    if isinstance(path, str):
        path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with netCDF4.Dataset(path, **kwargs) as stream:
        for name, size in dataset.dims.items():
            stream.createDimension(name,
                                   None if name in unlimited_dims else size)
            stream.setncatts(dataset.attrs)

        for name, variable in dataset.coords.items():
            _create_variable(stream, encoding, name, variable)

        for name, variable in dataset.data_vars.items():
            _create_variable(stream, encoding, name, variable)


class ProductSpecification:
    """Parse and load into memory the product specification"""
    SPECIFICATION = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "l2b-expert.xml")

    def __init__(self):
        self.variables, self.attributes = _parser(xt.parse(self.SPECIFICATION))
        for item in ["basic/time", "basic/time_tai"]:
            properties = self.variables[item]
            properties["attrs"].update(
                dict(units="microseconds since 2000-01-01 00:00:00.0",
                     _FillValue=-2**63))
            properties["dtype"] = "int64"

    @staticmethod
    def fill_value(properties):
        dtype = properties["dtype"]
        if isinstance(dtype, str):
            return getattr(np, dtype)(properties["attrs"]["_FillValue"])
        return np.array(properties["attrs"]["_FillValue"], dtype)

    def time(self, time: np.ndarray) -> Tuple[Dict, List[xr.DataArray]]:
        properties = self.variables["basic/time"]
        attrs = properties["attrs"]
        attrs.pop("_FillValue")
        return {
            "basic/time": {}
        }, [
            xr.DataArray(data=time,
                         dims=properties["shape"],
                         name="basic/time",
                         attrs=attrs)
        ]

    def _data_array(self, name: str,
                    data: np.ndarray) -> Tuple[Dict, xr.DataArray]:
        properties = self.variables[name]
        attrs = copy.deepcopy(properties["attrs"])

        # The fill value is casted to the target value of the variable
        fill_value = self.fill_value(properties)
        del attrs["_FillValue"]

        # Reading the storage properties of the variable ()
        encoding: Dict[str, Any] = dict(_FillValue=fill_value,
                                        dtype=properties["dtype"])

        # Some values read from the XML files must be decoded
        # TODO(fbriol): The type of these attributes should be determined from
        # their type, but at the moment this is not possible.
        for item in ["add_offset", "scale_factor"]:
            if item in attrs:
                attrs[item] = float(attrs[item])
        static_cast = (float
                       if "add_offset" in attrs or "scale_factor" in attrs else
                       lambda x: _cast_to_dtype(float(x), properties))
        for item in ["valid_min", "valid_max"]:
            if item in attrs:
                attrs[item] = static_cast(attrs[item])
        if "scale_factor" in attrs and "add_offset" not in attrs:
            attrs["add_offset"] = 0.0
        if "add_offset" in attrs and "scale_factor" not in attrs:
            attrs["scale_factor"] = 1.0
        return encoding, xr.DataArray(data=data,
                                      dims=properties["shape"],
                                      name=name,
                                      attrs=attrs)

    def x_ac(self, x_ac: np.ndarray) -> Tuple[Dict, xr.DataArray]:
        return self._data_array("expert/cross_track_distance", x_ac)

    def lon(self, lon: np.ndarray) -> Tuple[Dict, xr.DataArray]:
        # Longitude must be in [0, 360.0[
        return self._data_array("basic/longitude",
                                math.normalize_longitude(lon, 0))

    def lon_nadir(self, lon_nadir: np.ndarray) -> Tuple[Dict, xr.DataArray]:
        # Longitude must be in [0, 360.0[
        return self._data_array("expert/longitude_nadir",
                                math.normalize_longitude(lon_nadir, 0))

    def lat(self, lat: np.ndarray) -> Tuple[Dict, xr.DataArray]:
        return self._data_array("basic/latitude", lat)

    def lat_nadir(self, lat_nadir: np.ndarray) -> Tuple[Dict, xr.DataArray]:
        return self._data_array("expert/latitude_nadir", lat_nadir)

    def ssh_karin(self, ssh: np.ndarray) -> Tuple[Dict, xr.DataArray]:

        return self._data_array("basic/ssh_karin", ssh)

    def ssh_nadir(self, ssh: np.ndarray) -> Tuple[Dict, xr.DataArray]:
        return {
            '_FillValue': 2147483647,
            'dtype': 'int32'
        }, xr.DataArray(data=ssh,
                        dims=self.variables["basic/time"]["shape"],
                        name="basic/ssh_nadir",
                        attrs={
                            'coordinates': 'longitude latitude',
                            'long_name': 'sea surface height',
                            'scale_factor': 0.0001,
                            'standard_name':
                            'sea surface height above reference ellipsoid',
                            'units': 'm',
                            'valid_min': -15000000.0,
                            'valid_max': 150000000.0
                        })

    def fill_variables(self, variables,
                       shape) -> Iterator[Tuple[Dict, xr.DataArray]]:
        for item in self.variables:
            if item in variables:
                continue
            properties = self.variables[item]
            fill_value = self.fill_value(properties)
            data = np.full(tuple(shape[dim] for dim in properties["shape"]),
                           fill_value, properties["dtype"])
            yield self._data_array(item, data)


class Nadir:
    def __init__(self,
                 track: orbit_propagator.Pass,
                 standalone: Optional[bool] = True):
        self.standalone = standalone
        self.product_spec = ProductSpecification()
        self.num_lines = track.time.size
        self.encoding, self.data_vars = self.product_spec.time(track.time)
        self._data_array("lon_nadir",
                         track.lon_nadir)._data_array("lat_nadir",
                                                      track.lat_nadir)

    def _data_array(self, attr, data: np.ndarray):
        encoding, array = getattr(self.product_spec, attr)(data)
        if self.standalone:
            array.name = array.name.replace("_nadir", "")
        self.encoding[array.name] = encoding
        self.data_vars.append(array)
        return self

    def ssh(self, array: np.ndarray) -> None:
        self._data_array("ssh_nadir", array)

    def to_netcdf(self, cycle_number: int, pass_number: int, path: str,
                  complete_product: bool) -> None:
        LOGGER.info("write %s", path)
        vars = dict((item.name, item) for item in self.data_vars)

        # Variables that are not calculated are filled in in order to have a
        # product compatible with the PDD SWOT. Longitude is used as a
        # template.
        if complete_product and "basic/longitude" in vars:
            item = vars["basic/longitude"]
            shape = dict(zip(item.dims, item.shape))
            shape["num_sides"] = 2
            for encoding, array in self.product_spec.fill_variables(
                    vars.keys(), shape):
                self.encoding[array.name] = encoding
                self.data_vars.append(array)
        if "basic/longitude" in vars:
            lng = vars["basic/longitude"]
            lat = vars["basic/latitude"]
        else:
            lng = vars["expert/longitude_nadir"]
            lat = vars["expert/latitude_nadir"]

        dataset = xr.Dataset(data_vars=dict(
            (item.name, item) for item in self.data_vars),
                             attrs=global_attributes(
                                 self.product_spec.attributes, cycle_number,
                                 pass_number, self.data_vars[0].values, lng,
                                 lat))
        to_netcdf(dataset, path, self.encoding, mode="w")


class Swath(Nadir):
    def __init__(self, track: orbit_propagator.Pass) -> None:
        super().__init__(track, False)
        self.num_pixels = track.x_ac.size
        self._data_array(
            "x_ac",
            np.full((track.time.size, track.x_ac.size),
                    track.x_ac,
                    dtype=track.x_ac.dtype))._data_array(
                        "lon", track.lon)._data_array("lat", track.lat)

    def ssh(self, array: np.ndarray) -> None:
        self._data_array("ssh_karin", array)
