import pickle
import numpy as np
import random
import os
import pandas as pd
import yaml
import copy
from tqdm import tqdm
import xarray as xr

from LMRt.proxy import ProxyDatabase
from LMRt.gridded import Dataset
from LMRt.utils import (
    pp,
    p_header,
    p_hint,
    p_success,
    p_fail,
    p_warning,
    cfg_abspath,
    cwd_abspath,
    geo_mean,
    nino_indices,
    calc_tpi,
    global_hemispheric_means,
)

from .solver import GraphEM

class ReconJob:
    ''' Reconstruction Job

    General rule of loading parameters: load from the YAML first if available, then update with the parameters in the function calling,
      so the latter has a higher priority
    '''
    def __init__(self, configs=None, proxydb=None, prior=None, obs=None):
        self.configs = configs
        self.proxydb = proxydb
        self.prior = prior
        self.obs = obs

    def copy(self):
        return copy.deepcopy(self)

    def load_configs(self, cfg_path=None, job_dirpath=None, verbose=False):
        ''' Load the configuration YAML file

        self.configs will be updated

        Parameters
        ----------

        cfg_path : str
            the path of a configuration YAML file

        '''
        pwd = os.path.dirname(__file__)
        if cfg_path is None:
            cfg_path = os.path.abspath(os.path.join(pwd, './cfg/cfg_template.yml'))

        self.cfg_path = cfg_path
        if verbose: p_header(f'GraphEM: job.load_configs() >>> loading reconstruction configurations from: {cfg_path}')

        self.configs = yaml.safe_load(open(cfg_path, 'r'))
        if verbose: p_success(f'GraphEM: job.load_configs() >>> job.configs created')

        if job_dirpath is None:
            if os.path.isabs(self.configs['job_dirpath']):
                job_dirpath = self.configs['job_dirpath']
            else:
                job_dirpath = cfg_abspath(self.cfg_path, self.configs['job_dirpath'])
        else:
            job_dirpath = cwd_abspath(job_dirpath)

        self.configs['job_dirpath'] = job_dirpath
        os.makedirs(job_dirpath, exist_ok=True)
        if verbose:
            p_header(f'GraphEM: job.load_configs() >>> job.configs["job_dirpath"] = {job_dirpath}')
            p_success(f'GraphEM: job.load_configs() >>> {job_dirpath} created')
            pp.pprint(self.configs)

    def load_proxydb(self, path=None, verbose=False, load_df_kws=None):
        ''' Load the proxy database

        self.proxydb will be updated

        Parameters
        ----------
        
        proxydb_path : str
            if given, should point to a pickle file with a Pandas DataFrame underlying

        '''
        # update self.configs with not None parameters in the function calling
        if path is None:
            if os.path.isabs(self.configs['proxydb_path']):
                path = self.configs['proxydb_path']
            else:
                path = cfg_abspath(self.cfg_path, self.configs['proxydb_path'])
        else:
            path = cwd_abspath(path)

        self.configs['proxydb_path'] = path
        if verbose: p_header(f'GraphEM: job.load_proxydb() >>> job.configs["proxydb_path"] = {path}')

        # load proxy database
        proxydb = ProxyDatabase()
        proxydb_df = pd.read_pickle(self.configs['proxydb_path'])
        load_df_kws = {} if load_df_kws is None else load_df_kws.copy()
        proxydb.load_df(proxydb_df, ptype_psm='linear', ptype_season=None, verbose=verbose, **load_df_kws)
        if verbose: p_success(f'GraphEM: job.load_proxydb() >>> {proxydb.nrec} records loaded')

        proxydb.source = self.configs['proxydb_path']
        self.proxydb = proxydb
        if verbose: p_success(f'GraphEM: job.load_proxydb() >>> job.proxydb created')

    def filter_proxydb(self, ptype_list=None, dt=1, pids=None, verbose=False):
        if ptype_list is None:
            ptype_list = self.configs['ptype_list']
        else:
            self.configs['ptype_list'] = ptype_list
            if verbose: p_header(f'GraphEM: job.filter_proxydb() >>> job.configs["ptype_list"] = {ptype_list}')

        proxydb = self.proxydb.copy()
        if verbose: p_header(f'GraphEM: job.filter_proxydb() >>> filtering proxy records according to: {ptype_list}')
        proxydb.filter_ptype(ptype_list, inplace=True)

        proxydb.filter_dt(dt, inplace=True)

        if pids is not None:
            self.configs['assim_pids'] = pids
            if verbose: p_header(f'GraphEM: job.filter_proxydb() >>> job.configs["assim_pids"] = {pids}')

        if 'assim_pids' in self.configs and self.configs['assim_pids'] is not None:
            proxydb.filter_pids(self.configs['assim_pids'], inplace=True)

        if verbose: p_success(f'GraphEM: job.filter_proxydb() >>> {proxydb.nrec} records remaining')

        self.proxydb = proxydb

    def seasonalize_proxydb(self, ptype_season=None, verbose=False):
        if ptype_season is None:
            if 'ptype_season' not in self.configs:
                ptype_season = {}
                for ptype in self.configs['ptype_list']:
                    ptype_season[ptype] = list(range(1, 13))

                self.configs['ptype_season'] = ptype_season
                if verbose: p_header(f'GraphEM: job.seasonalize_proxydb() >>> job.configs["ptype_season"] = {ptype_season}')
            else:
                ptype_season = self.configs['ptype_season']

        else:
            self.configs['ptype_season'] = ptype_season
            if verbose: p_header(f'GraphEM: job.seasonalize_proxydb() >>> job.configs["ptype_season"] = {ptype_season}')

        proxydb = self.proxydb.copy()
        if self.configs['ptype_season'] is not None:
            if verbose: p_header(f'GraphEM: job.seasonalize_proxydb() >>> seasonalizing proxy records according to: {self.configs["ptype_season"]}')
            proxydb.seasonalize(self.configs['ptype_season'], inplace=True)
            if verbose: p_success(f'GraphEM: job.seasonalize_proxydb() >>> {proxydb.nrec} records remaining')

        self.proxydb = proxydb
        if verbose: p_success(f'GraphEM: job.seasonalize_proxydb() >>> job.proxydb updated')

    def load_obs(self, path_dict=None, varname_dict=None, verbose=False, anom_period=None):
        ''' Load instrumental observations fields

        Parameters
        ----------

        path_dict: dict
            a dict of environmental variables

        varname_dict: dict
            a dict to map variable names, e.g. {'tas': 'sst'} means 'tas' is named 'sst' in the input NetCDF file
        
        '''
        if path_dict is None:
            obs_path = cfg_abspath(self.cfg_path, self.configs['obs_path'])
        else:
            obs_path = cwd_abspath(path_dict)
        self.configs['obs_path'] = obs_path

        if anom_period is None:
            anom_period = self.configs['anom_period']
        else:
            self.configs['obs_anom_period'] = anom_period
            if verbose: p_header(f'GraphEM: job.load_obs() >>> job.configs["anom_period"] = {anom_period}')

        vn_dict = {
            'time': 'time',
            'lat': 'lat',
            'lon': 'lon',
        }
        if 'obs_varname' in self.configs:
            vn_dict.update(self.configs['obs_varname'])
        if varname_dict is not None:
            vn_dict.update(varname_dict)
        self.configs['obs_varname'] = vn_dict

        if verbose: p_header(f'GraphEM: job.load_obs() >>> loading instrumental observation fields from: {self.configs["obs_path"]}')

        ds = Dataset()
        ds.load_nc(self.configs['obs_path'], varname_dict=vn_dict, anom_period=anom_period, inplace=True)
        self.obs = ds
        if verbose: p_success(f'GraphEM: job.load_obs() >>> job.obs created')

    def seasonalize_obs(self, season=None, verbose=False):
        if season is None:
            if 'obs_season' not in self.configs:
                season = list(range(1, 13))
                self.configs['obs_season'] = season
                if verbose: p_header(f'GraphEM: job.seasonalize_obs() >>> job.configs["obs_season"] = {season}')
            else:
                season = self.configs['obs_season']
        else:
            self.configs['obs_season'] = season
            if verbose: p_header(f'GraphEM: job.seasonalize_obs() >>> job.configs["obs_season"] = {season}')

        ds = self.obs.copy()
        ds.seasonalize(self.configs['obs_season'], inplace=True)
        if verbose:
            p_hint(f'GraphEM: job.seasonalize_obs() >>> seasonalized obs w/ season {season}')
            print(ds)

        self.obs = ds
        if verbose: p_success(f'GraphEM: job.seasonalize_obs() >>> job.obs updated')