#!/usr/bin/env python3

# Copyright (C) 2021-2023 Guillaume Jouvet <guillaume.jouvet@unil.ch>
# Published under the GNU GPL (Version 3), check at the LICENSE file 

import numpy as np
import matplotlib.pyplot as plt
import datetime, time
import math
import tensorflow as tf
from netCDF4 import Dataset


def params(parser):
    parser.add_argument(
        "--smb_accpdd_update_freq",
        type=float,
        default=1,
        help="Update the mass balance each X years (1)",
    )
    parser.add_argument(
        "--smb_accpdd_refreeze_factor",
        type=float,
        default=0.6,
        help="Refreezing factor",
    )
    parser.add_argument(
        "--smb_accpdd_thr_temp_snow",
        type=float,
        default=0.0,
        help="Threshold temperature for solid precipitation (0.0)",
    )
    parser.add_argument(
        "--smb_accpdd_thr_temp_rain",
        type=float,
        default=2.0,
        help="Threshold temperature for liquid precipitation (2.0)",
    )
    parser.add_argument(
        "--smb_accpdd_melt_factor_snow",
        type=float,
        default=0.003 * 365.242198781,
        help="Degree-day factor for snow (water eq.) (unit: meter / (Kelvin year))",
    )
    parser.add_argument(
        "--smb_accpdd_melt_factor_ice",
        type=float,
        default=0.008 * 365.242198781,
        help="Degree-day factor for ice (water eq.) (unit: meter / (Kelvin year))",
    )
    parser.add_argument(
        "--smb_accpdd_shift_hydro_year",
        type=float,
        default=0.75,
        help="This serves to start Oct 1. the acc/melt computation (0.75)",
    )
    parser.add_argument(
        "--smb_accpdd_ice_density",
        type=float,
        default=910.0,
        help="Density of ice for conversion of SMB into ice equivalent",
    )
    parser.add_argument(
        "--smb_accpdd_wat_density",
        type=float,
        default=1000.0,
        help="Density of water",
    )
    parser.add_argument(
        "--offset_input_file",
        type=str,
        default="precip_offset.nc",
        help="NetCDF input data file for precip scalar multipliers per catchment",
    )


def initialize(params, state):
    state.tcomp_smb_accpdd = []
    state.tlast_mb = tf.Variable(-1.0e5000)

    # load the precip_offset_map_netcdf: this part was added by tanc
    nc = Dataset(params.offset_input_file, "r")
    # Read the 'precip_multiplier' variable and store it in the 'state' object
    state.precip_offset = nc.variables['precip_multiplier'][:]
    # Close the NetCDF file
    nc.close()


# Warning: The decorator permits to take full benefit from efficient TensorFlow operation (especially on GPU)
# Note that tf.function works best with TensorFlow ops; NumPy and Python calls are converted to constants.
# Therefore: you must make sure any variables are TensorFlow Tensor (and not Numpy)
# @tf.function()
def update(params, state):
    """
    mass balance forced by climate with accumulation and temperature-index melt model
    Input:  state.precipitation [Unit: kg * m^(-2) * y^(-1) water eq]
            state.air_temp      [Unit: °C           ]
    Output  state.smb           [Unit: m ice eq. / y]

    This mass balance routine implements a combined accumulation / temperature-index model [Hock, 2003].
    It is a TensorFlow re-implementation similar to the one used in the aletsch-1880-2100 example
    but adapted to fit as closely as possible (thought it is not a strict fit)
    the Positive Degree Day model implemented in PyPDD (Seguinot, 2019) used for the Parralel Ice Sheet
    Model (PISM, Khroulev and the PISM Authors, 2020) necessary to perform PISM / IGM comparison.
    The computation of the PDD using the expectation integration formulation (Calov and Greve, 2005),
    the computation of the snowpack, and the refereezing parameters are taken from PyPDD / PISM implementation.

    References:

    Hock R. (2003). Temperature index melt modelling in mountain areas, J. Hydrol.

    Seguinot J. (2019). PyPDD: a positive degree day model for glacier surface mass balance (v0.3.1).
    Zenodo. https://doi.org/10.5281/zenodo.3467639

    Khroulev C. and the PISM Authors. PISM, a Parallel Ice Sheet Model v1.2: User’s Manual. 2020.
    www.pism-docs.org

    Calov and Greve (2005), A semi-analytical solution for the positive degree-day model with
    stochastic temperature variations, JOG.
    """

    # update smb each X years
    if (state.t - state.tlast_mb) >= params.smb_accpdd_update_freq:
        if hasattr(state, "logger"):
            state.logger.info(
                "Construct mass balance at time : " + str(state.t.numpy())
            )

        state.tcomp_smb_accpdd.append(time.time())

        #check if state has state.precip_offsets which is the scalar multiplier map: this part was added by Tanc
        #and if it has, multiply it to modify state.precipitation
        if hasattr(state, 'precip_offset'):
            precipitation_for_smb = state.precipitation * state.precip_offset
            print("Multiplication of precip has been done")
        else:
            precipitation_for_smb = state.precipitation

        # keep solid precipitation when temperature < smb_accpdd_thr_temp_snow
        # with linear transition to 0 between smb_accpdd_thr_temp_snow and smb_accpdd_thr_temp_rain
        accumulation = tf.where(
            state.air_temp <= params.smb_accpdd_thr_temp_snow,
            precipitation_for_smb,
            tf.where(
                state.air_temp >= params.smb_accpdd_thr_temp_rain,
                0.0,
                precipitation_for_smb
                * (params.smb_accpdd_thr_temp_rain - state.air_temp)
                / (params.smb_accpdd_thr_temp_rain - params.smb_accpdd_thr_temp_snow),
            ),
        )

        if hasattr(state, "air_temp_sd"):
            # compute the positive temp with the integral formaulation from Calov and Greve (2005)
            # the formulation assumes the air temperature follows a normal distribution, it is obtained
            # by integrating by part of the integral of T * normal density over {T>=0} since
            # only positive temp. matters. This yields to a first boundary terms, and a second
            # involving the integral of the normal density, i.e. the erf function.
            cela = state.air_temp / (1.4142135623730951 * state.air_temp_sd)
            pos_temp_year = (
                state.air_temp_sd
                * tf.math.exp(-tf.math.square(cela))
                / 2.5066282746310002
                + state.air_temp * tf.math.erfc(-cela) / 2.0
            )
        else:
            pos_temp_year = tf.where(state.air_temp > 0.0, state.air_temp, 0.0)

        # unit to [  kg * m^(-2) * y^(-1) water eq  ] -> [ m water eq ]
        accumulation /= (accumulation.shape[0] * params.smb_accpdd_wat_density) 
        
        # unit to [ °C ]  -> [ °C y ]
        pos_temp_year /= pos_temp_year.shape[0]  

        ablation = []  # [ unit : water-eq m ]

        snow_depth = tf.zeros((state.air_temp.shape[1], state.air_temp.shape[2]))

        for kk in range(state.air_temp.shape[0]):
            # shift to hydro year, i.e. start Oct. 1
            k = (
                kk + int(state.air_temp.shape[0] * params.smb_accpdd_shift_hydro_year)
            ) % (state.air_temp.shape[0])

            # add accumulation to the snow depth
            snow_depth += accumulation[k]

            # the ablation (unit is m water eq.) is the product of positive temp  with melt
            # factors for ice, or snow, or a fraction of the two if all snow has melted
            ablation.append(
                tf.where(
                    snow_depth == 0,
                    pos_temp_year[k] * params.smb_accpdd_melt_factor_ice,
                    tf.where(
                        pos_temp_year[k] * params.smb_accpdd_melt_factor_snow
                        < snow_depth,
                        pos_temp_year[k] * params.smb_accpdd_melt_factor_snow,
                        snow_depth
                        + (
                            pos_temp_year[k]
                            - snow_depth / params.smb_accpdd_melt_factor_snow
                        )
                        * params.smb_accpdd_melt_factor_ice,
                    ),
                )
            )

            # remove snow melt to snow depth, and cap it as snow_depth can not be negative
            snow_depth = tf.clip_by_value(snow_depth - ablation[-1], 0.0, 1.0e10)

        ablation = (1 - params.smb_accpdd_refreeze_factor) * tf.stack(ablation, axis=0)

        # sum accumulation and ablation over the year, and conversion to ice equivalent
        state.smb = tf.math.reduce_sum(accumulation - ablation, axis=0)* (
            params.smb_accpdd_wat_density / params.smb_accpdd_ice_density
        )

        if hasattr(state, "icemask"):
            state.smb = tf.where(
                (state.smb < 0) | (state.icemask > 0.5), state.smb, -10
            )

        state.tlast_mb.assign(state.t)

        state.tcomp_smb_accpdd[-1] -= time.time()
        state.tcomp_smb_accpdd[-1] *= -1


def finalize(params, state):
    pass
