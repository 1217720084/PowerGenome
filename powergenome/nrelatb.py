"""
Functions to fetch and modify NREL ATB data from PUDL
"""

import logging
import numpy as np
import pandas as pd

idx = pd.IndexSlice
logger = logging.getLogger(__name__)


def fetch_atb_costs(pudl_engine, settings):
    """Get NREL ATB power plant cost data from database, filter where applicable

    Parameters
    ----------
    pudl_engine : sqlalchemy.Engine
        A sqlalchemy connection for use by pandas
    settings : dict
        User-defined parameters from a settings file

    Returns
    -------
    DataFrame
        Power plant cost data with columns:
        ['technology', 'cap_recovery_years', 'cost_case', 'financial_case',
       'basis_year', 'tech_detail', 'o_m_fixed', 'o_m_variable', 'capex', 'cf',
       'fuel', 'lcoe', 'o_m', 'waccnomtech']
    """

    atb_costs = pd.read_sql_table("technology_costs_nrelatb", pudl_engine)

    index_cols = [
        "technology",
        "cap_recovery_years",
        "cost_case",
        "financial_case",
        "basis_year",
        "tech_detail",
    ]
    atb_costs.set_index(index_cols, inplace=True)
    atb_costs.drop(columns=["key", "id"], inplace=True)

    cap_recovery = str(settings["atb_cap_recovery_years"])
    financial = settings["atb_financial_case"]
    # basis_year = settings["model_year"]

    # logger.warning("The model year is being used as the cost basis year in all cases")
    atb_costs = atb_costs.loc[idx[:, cap_recovery, :, financial, :, :], :]
    atb_costs = atb_costs.reset_index()

    return atb_costs


def fetch_atb_heat_rates(pudl_engine):
    """Get heat rate projections for power plants

    Data is originally from AEO, NREL does a linear interpolation between current and
    final years.

    Parameters
    ----------
    pudl_engine : sqlalchemy.Engine
        A sqlalchemy connection for use by pandas

    Returns
    -------
    DataFrame
        Power plant heat rate data by year with columns:
        ['technology', 'tech_detail', 'basis_year', 'heat_rate']
    """

    heat_rates = pd.read_sql_table("technology_heat_rates_nrelatb", pudl_engine)

    return heat_rates


def atb_fixed_var_om_existing(results, atb_costs_df, atb_hr_df, settings):
    """Add fixed and variable O&M for existing power plants

    ATB O&M data for new power plants are used as reference values. Fixed and variable
    O&M for each technology and heat rate are calculated. Assume that O&M scales with
    heat rate from new plants to existing generators. A separate multiplier for fixed
    O&M is specified in the settings file.

    Parameters
    ----------
    results : DataFrame
        Compiled results of clustered power plants with weighted average heat rates.
        Note that column names should include "technology", "Heat_rate_MMBTU_per_MWh",
        and "region". Technology names should not yet be converted to snake case.
    atb_costs_df : DataFrame
        Cost data from NREL ATB
    atb_hr_df : DataFrame
        Heat rate data from NREL ATB
    settings : dict
        User-defined parameters from a settings file

    Returns
    -------
    DataFrame
        Same as incoming "results" dataframe but with new columns
        "Fixed_OM_cost_per_MWyr" and "Var_OM_cost_per_MWh"
    """

    techs = settings["eia_atb_tech_map"]
    existing_year = settings["atb_existing_year"]

    # ATB string is <technology>_<tech_detail>
    techs = {eia: atb_costs_df.split("_") for eia, atb_costs_df in techs.items()}

    df_list = []
    grouped_results = results.reset_index().groupby(
        ["technology", "Heat_rate_MMBTU_per_MWh"], as_index=False
    )
    for group, _df in grouped_results:

        eia_tech, existing_hr = group
        atb_tech, tech_detail = techs[eia_tech]
        print(group, techs[eia_tech])
        try:
            new_build_hr = (
                atb_hr_df.query(
                    "technology==@atb_tech & tech_detail==@tech_detail"
                    "& basis_year==@existing_year"
                )
                .squeeze()
                .at["heat_rate"]
            )
        except ValueError:
            # Not all technologies have a heat rate. If they don't, just set both values
            # to 1
            existing_hr = 1
            new_build_hr = 1
        # print(new_build_hr)
        atb_fixed_om_mw_yr = (
            atb_costs_df.query(
                "technology==@atb_tech & cost_case=='Mid' & tech_detail==@tech_detail"
                "& basis_year==@existing_year"
            )
            .squeeze()
            .at["o_m_fixed_mw"]
        )
        # print(atb_fixed_om_mw_yr)
        atb_var_om_mwh = (
            atb_costs_df.query(
                "technology==@atb_tech & cost_case=='Mid' & tech_detail==@tech_detail"
                "& basis_year==@existing_year"
            )
            .squeeze()
            .at["o_m_variable_mwh"]
        )
        # print(atb_var_om_mwh)
        _df["Fixed_OM_cost_per_MWyr"] = (
            atb_fixed_om_mw_yr
            * settings["existing_om_multiplier"]
            * (existing_hr / new_build_hr)
        )
        _df["Var_OM_cost_per_MWh"] = atb_var_om_mwh * (existing_hr / new_build_hr)

        df_list.append(_df)

    # logger.info(_df)
    mod_results = pd.concat(df_list, ignore_index=True)
    mod_results = mod_results.sort_values(["region", "technology", "cluster"])

    return mod_results


def single_generator_row(atb_costs, new_gen_type, model_year):

    technology, tech_detail, cost_case, size_mw = new_gen_type
    row = atb_costs.query(
        "technology==@technology & tech_detail==@tech_detail "
        "& cost_case==@cost_case & basis_year==@model_year"
    ).copy()
    row["Cap_size"] = size_mw

    return row


def investment_cost_calculator(capex, wacc, cap_rec_years):

    inv_cost = capex * (
        np.exp(wacc * cap_rec_years)
        * (np.exp(wacc) - 1)
        / (np.exp(wacc * cap_rec_years) - 1)
    )

    return inv_cost


def atb_new_generators(results, atb_costs, atb_hr, settings):
    """Add rows for new generators in each region

    Parameters
    ----------
    results : DataFrame
        Compiled results of clustered power plants with weighted average heat
    atb_costs : [type]
        [description]
    atb_hr : [type]
        [description]
    settings : [type]
        [description]

    Returns
    -------
    [type]
        [description]
    """

    new_gen_types = settings["atb_new_gen"]
    model_year = settings["model_year"]
    regions = settings["model_regions"]

    new_gen_df = pd.concat(
        [
            single_generator_row(atb_costs, new_gen, model_year)
            for new_gen in new_gen_types
        ],
        ignore_index=True,
    )

    new_gen_df["Inv_cost_per_MWyr"] = investment_cost_calculator(
        capex=new_gen_df["capex"],
        wacc=new_gen_df["waccnomtech"],
        cap_rec_years=settings["atb_cap_recovery_years"],
    )

    new_gen_df = new_gen_df.merge(
        atb_hr, on=["technology", "tech_detail", "basis_year"], how="left"
    )

    new_gen_df = new_gen_df.rename(
        columns={
            "heat_rate": "Heat_rate_MMBTU_per_MWh",
            "o_m_fixed_mw": "Fixed_OM_cost_per_MWyr",
            "o_m_variable_mwh": "Var_OM_cost_per_MWh",
        }
    )

    new_gen_df["technology"] = (
        new_gen_df["technology"]
        + "_"
        + new_gen_df["tech_detail"]
        + "_"
        + new_gen_df["cost_case"]
    )

    keep_cols = [
        "technology",
        "basis_year",
        # "tech_detail",
        "Fixed_OM_cost_per_MWyr",
        "Var_OM_cost_per_MWh",
        # "fuel",
        "Inv_cost_per_MWyr",
        "Heat_rate_MMBTU_per_MWh",
        "Cap_size"
    ]

    df_list = []
    for region in regions:
        _df = new_gen_df.loc[:, keep_cols].copy()
        _df["region"] = region
        df_list.append(_df)

    results = pd.concat([results, pd.concat(df_list, ignore_index=True)], ignore_index=True)

    return results
