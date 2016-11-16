#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (C) 2016 Bitergia
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA 02111-1307, USA.
#
# Authors:
#     Luis Cañas-Díaz <lcanas@bitergia.com>
#

import configparser
import logging
import argparse
import time
import threading
import json
import sys
import requests
import random

from grimoire.arthur import feed_backend, enrich_backend, get_ocean_backend, load_identities
from grimoire.panels import import_dashboard
from grimoire.utils import get_connectors, get_connector_from_name, get_elastic

logger = logging.getLogger(__name__)

class Task():
    """ Basic class shared by all tasks """

    def __init__(self, conf):
        self.conf = conf

    def __get_github_owner_repo(self, github_url):
        owner = github_url.split('/')[-2]
        repo = github_url.split('/')[-1]
        return (owner, repo)

    def compose_perceval_params(self, backend_name, repo):
        params = []
        if backend_name == 'git':
            params.append(str(repo))
        elif backend_name == 'github':
            owner, github_repo = self.__get_github_owner_repo(repo)
            params.append('--owner')
            params.append(owner)
            params.append('--repository')
            params.append(github_repo)
            params.append('--sleep-for-rate')
            params.append('-t')
            params.append(self.conf['github']['token'])
        return params

    def set_repos(self, repos):
        self.repos = repos

    def set_backend_name(self, backend_name):
        self.backend_name = backend_name

    def run(self):
        """ Execute the Task """
        logger.debug("A bored task. It does nothing!")


class TaskSortingHat(Task):
    """ Basic class shared by all Sorting Hat tasks """

    def __init__(self, conf, load=False, unify=False, autoprofile=False,
                 affiliate=False):
        super().__init__(conf)

        self.db_sh = self.conf['sh_database']
        self.db_user = self.conf['sh_user']
        self.db_password = self.conf['sh_password']
        self.db_host = self.conf['sh_host']
        self.load = load  # Load identities
        self.unify = unify  # Unify identities
        self.autoprofile = autoprofile  # Execute autoprofile
        self.affiliate = affiliate # Affiliate identities


    def run(self):

        if not self.backend_name:
            logging.error ("Backend not configured in TaskSortingHat.")
            return

        db_projects_map = None
        json_projects_map = None
        no_incremental = False
        clean = False
        connector = get_connector_from_name(self.backend_name)

        enrich_backend = connector[2](self.db_sh, db_projects_map, json_projects_map,
                                      self.db_user, self.db_password, self.db_host)
        elastic_enrich = get_elastic(self.conf['es_enrichment'],
                                     self.conf[self.backend_name]['enriched_index'],
                                     clean, enrich_backend)
        enrich_backend.set_elastic(elastic_enrich)

        backend_cmd = None  # FIXME: Could we build a backend_cmd with params?
        ocean_backend = get_ocean_backend(self.backend_name, backend_cmd,
                                          enrich_backend, no_incremental)
        elastic_ocean = get_elastic(self.conf['es_collection'],
                                    self.conf[self.backend_name]['raw_index'],
                                    clean, ocean_backend)
        ocean_backend.set_elastic(elastic_ocean)


        if self.load:
            logger.info("Loading identities from index raw")
            load_identities(ocean_backend, enrich_backend)


class TaskStudies(Task):
    """ Run studies for the data sources  """

class TaskPanels(Task):
    """ Create the panels  """

    panels = {
        "git": ["panels/dashboards/git-organizations-projects.json",
                "panels/dashboards/git_demographics-organizations-projects.json"
                ],
        "github": ["panels/dashboards/github_backlog_organizations.json",
                   "panels/dashboards/github_issues-organizations.json",
                   "panels/dashboards/github_pullrequests_delays-organizations.json",
                   "panels/dashboards/github_pullrequests-organizations.json"
                   ]
    }

    def run(self):
        for panel_file in self.panels[self.backend_name]:
            import_dashboard(self.conf['es_enrichment'], panel_file)


class TaskCollect(Task):
    """ Basic class shared by all collection tasks """

    def __init__(self, conf, repos=None, backend_name=None):
        super().__init__(conf)
        self.repos = repos
        self.backend_name = backend_name
        # This will be options in next iteration
        self.clean = False
        self.fetch_cache = False

    def run(self):
        t2 = time.time()
        logger.info('Data collection starts for %s ', self.backend_name)
        clean = False
        fetch_cache = False
        cfg = self.conf
        for r in self.repos:
            backend_args = self.compose_perceval_params(self.backend_name, r)
            logger.info('Collection starts for %s %s', self.backend_name, r)
            feed_backend(cfg['es_collection'], clean, fetch_cache,
                        self.backend_name,
                        backend_args,
                        cfg[self.backend_name]['raw_index'],
                        cfg[self.backend_name]['enriched_index'],
                        r)

        time.sleep(random.randint(0,20)) # FIXME test purposes

        t3 = time.time()
        spent_time = time.strftime("%H:%M:%S", time.gmtime(t3-t2))
        logger.info('Data collection finished for %s in %s' % (self.backend_name, spent_time))

class TaskEnrich(Task):
    """ Basic class shared by all enriching tasks """

    def __init__(self, conf, repos=None, backend_name=None):
        super().__init__(conf)
        self.repos = repos
        self.backend_name = backend_name
        # This will be options in next iteration
        self.clean = False
        self.fetch_cache = False

    def run(self, only_identities=False):
        t2 = time.time()

        if only_identities:
            phase_name = 'Identities collection'
        else:
            phase_name = 'Data enrichment'
        logger.info('%s starts for %s ', phase_name, self.backend_name)

        cfg = self.conf

        no_incremental = False
        github_token = None
        if 'github' in self.conf and 'token' in self.conf['github']:
            github_token = self.conf['github']['token']
        only_studies = False
        for r in self.repos:
            backend_args = self.compose_perceval_params(self.backend_name, r)

            try:
                logger.info('Enrichment starts for %s %s', self.backend_name, r)
                enrich_backend(cfg['es_collection'], self.clean, self.backend_name,
                                backend_args, #FIXME #FIXME
                                cfg[self.backend_name]['raw_index'],
                                cfg[self.backend_name]['enriched_index'],
                                None, #projects_db is deprecated
                                cfg['projects_file'],
                                cfg['sh_database'],
                                no_incremental, only_identities,
                                github_token,
                                cfg['studies_enabled'],
                                only_studies,
                                cfg['es_enrichment'],
                                None, #args.events_enrich
                                cfg['sh_user'],
                                cfg['sh_password'],
                                cfg['sh_host'],
                                None, #args.refresh_projects,
                                None) #args.refresh_identities)
            except KeyError as e:
                logger.exception(e)

        time.sleep(random.randint(0,20)) # FIXME test purposes

        t3 = time.time()
        spent_time = time.strftime("%H:%M:%S", time.gmtime(t3-t2))
        logger.info('%s finished for %s in %s' % (phase_name, self.backend_name, spent_time))


class TasksManager(threading.Thread):
    """
    Class to manage tasks execution

    All tasks in the same task manager will be executed in the same thread
    in a serial way.

    """

    def __init__(self, tasks, backend_name, repos, stopper, conf):
        """
        :tasks : tasks to be executed using the backend
        :backend_name: perceval backend name
        :repos: list of repositories to be managed
        :conf: conf for the manager
        """
        super().__init__()  # init the Thread
        self.tasks = tasks  # tasks to be executed
        self.backend_name = backend_name
        self.repos = repos
        self.stopper = stopper  # To stop the thread from parent
        self.rounds_limit = 1  # For debugging

    def add_task(self, task):
        self.tasks.append(task)

    def run(self):
        logger.debug('Starting Task Manager thread %s', self.backend_name)

        # Configure the tasks
        for task in self.tasks:
            task.set_repos(self.repos)
            task.set_backend_name(self.backend_name)

        if not self.tasks:
            logger.debug('Task Manager thread %s without tasks', self.backend_name)

        rounds = 0

        while not self.stopper.is_set():
            for task in self.tasks:
                task.run()
            rounds  += 1
            if rounds > self.rounds_limit:
                break

        logger.debug('Exiting Task Manager thread %s', self.backend_name)


class ElasticSearchError(Exception):
    """Exception raised for errors in the list of backends
    """
    def __init__(self, expression):
        self.expression = expression

class Mordred:

    def __init__(self, conf_file):
        self.conf_file = conf_file
        self.conf = None
        logger = self.setup_logs()

    def setup_logs(self):

        # For gelk logging
        logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(message)s')
        # To control requests logging
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("requests").setLevel(logging.WARNING)

        #logging.basicConfig(filename='/tmp/mordred.log'level=logging.DEBUG)
        # logger = logging.getLogger('mordred')
        logger.setLevel(logging.DEBUG)

        fh = logging.FileHandler('spam.log')
        fh.setLevel(logging.DEBUG)
        # create console handler with a higher log level
        ch = logging.StreamHandler()
        ch.setLevel(logging.ERROR)

        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        ch.setFormatter(formatter)
        fh.setFormatter(formatter)
        # add the handlers to logger
        logger.addHandler(ch)
        logger.addHandler(fh)
        #self.projects = None
        return logger

    def update_conf(self, conf):
        self.conf = conf

    def read_conf_files(self):
        conf = {}

        logger.debug("Reading conf files")
        config = configparser.ConfigParser()
        config.read(self.conf_file)
        logger.debug(config.sections())

        try:
            if 'sleep' in config['general'].keys():
                sleep = config.get('general','sleep')
            else:
                sleep = 0
            conf['sleep'] = sleep

        except KeyError:
            logger.error("'general' section is missing from %s " + \
                        "conf file", self.conf_file)

        conf['es_collection'] = config.get('es_collection', 'url')
        conf['es_enrichment'] = config.get('es_enrichment', 'url')

        projects_file = config.get('projects','projects_file')
        conf['projects_file'] = projects_file
        with open(projects_file,'r') as fd:
            projects = json.load(fd)
        conf['projects'] = projects

        conf['sh_database'] = config.get('sortinghat','database')
        conf['sh_host'] = config.get('sortinghat','host')
        conf['sh_user'] = config.get('sortinghat','user')
        conf['sh_password'] = config.get('sortinghat','password')

        for backend in get_connectors().keys():
            try:
                raw = config.get(backend, 'raw_index')
                enriched = config.get(backend, 'enriched_index')
                conf[backend] = {'raw_index':raw, 'enriched_index':enriched}
                if backend == 'github':
                    conf[backend]['token'] = config.get(backend, 'token')
            except configparser.NoSectionError:
                pass

        conf['collection_enabled'] = config.getboolean('phases','collection')
        conf['identities_enabled'] = config.getboolean('phases','identities')
        conf['enrichment_enabled'] = config.getboolean('phases','enrichment')
        conf['studies_enabled'] = config.getboolean('phases','studies')
        conf['panels_enabled'] = config.getboolean('phases','panels')

        return conf

    def check_write_permission(self):
        ##
        ## So far there is no way to distinguish between read and write permission
        ##
        if self.conf['collection_enabled'] or \
            self.conf['enrichment_enabled'] or \
            self.conf['studies_enabled']:
            es = self.conf['es_collection']
            r = requests.get(es, verify=False)
            if r.status_code != 200:
                raise ElasticSearchError('Is the ElasticSearch for data collection accesible?')

        if self.conf['enrichment_enabled'] or \
            self.conf['studies_enabled']:
            es = self.conf['es_enrichment']
            r = requests.get(es, verify=False)
            if r.status_code != 200:
                raise ElasticSearchError('Is the ElasticSearch for data enrichment accesible?')

    def feed_orgs_tables(self):
        print("Not implemented")

    def __get_repos_by_backend(self):
        #
        # return dict with backend and list of repositories
        #
        output = {}
        projects = self.conf['projects']

        for backend in get_connectors().keys():
            for pro in projects:
                if backend in projects[pro]:
                    if not backend in output:
                        output[backend]  = projects[pro][backend]
                    else:
                        output[backend] = output[backend] + projects[pro][backend]

        # backend could be in project/repo file but not enabled in
        # mordred conf file
        enabled = {}
        for k in output:
            if k in self.conf:
                enabled[k] = output[k]

        logger.debug('repos to be retrieved: %s ', enabled)
        return enabled

    def collect_identities(self):
        self.data_enrichment(True)

    def data_enrichment_studies(self):
        logger.info("Not implemented")

    def update_es_aliases(self):
        logger.info("Not implemented")

    def identities_merge(self):
        logger.info("Not implemented")

    def launch_task_manager(self, tasks, timer=0):
        """
        Start a task manger per backend to complete the tasks.

        All the tasks that should be executed according to the config
        must be added to the task manager.
        """

        logger.info('Task Manager starting .. ')

        threads = []
        stopper = threading.Event()
        repos_backend = self.__get_repos_by_backend()
        for backend in repos_backend:
            # Start new Threads and add them to the threads list to complete
            t = TasksManager(tasks, backend, repos_backend[backend], stopper, self.conf)
            # According to the conf we need to add tasks
            threads.append(t)
            t.start()

        logger.info("Waiting for all threads to complete. This could take a while ..")
        if timer:
            logger.info("Waiting %s seconds before stopping", str(timer))
            time.sleep(timer)
        stopper.set()

        # Wait for all threads to complete
        for t in threads:
            t.join()

        logger.debug("Task manager and all its tasks (threads) finished!")

    def run(self):

        while True:

            logger.debug("Starting Mordred engine ...")

            #FIXME with the parallel processes this is won't be read until restart
            self.update_conf(self.read_conf_files())

            # check section enabled
            # check we have access the needed ES
            self.check_write_permission()

            # do we need ad-hoc scripts?

            # projects database, do we need to feed it?
            self.feed_orgs_tables()


            tasks = []

            # phase one
            # we get all the items with Perceval + identites browsing the
            # raw items

            if self.conf['collection_enabled']:
                tasks.append(TaskCollect(self.conf))
                self.launch_task_manager(tasks)

            if self.conf['identities_enabled']:
                tasks.append(TaskSortingHat(self.conf, load=True))
                self.launch_task_manager(tasks)
                # unify + affiliates (phase one and a half)
                # Merge: we unify all the identities and enrol them
                # tasks = [TaskSortingHat(self.conf, unify=True, affiliate=True)]
                # self.launch_task_manager(tasks)

            if self.conf['enrichment_enabled']:
                # raw items + sh database with merged identities + affiliations
                # will used to produce a enriched index
                tasks = [TaskEnrich(self.conf)]
                self.launch_task_manager(tasks)
                break

            if self.conf['studies_enabled']:
                # raw items + sh database with merged identities + affiliations
                # will used to produce a enriched index
                tasks = [TaskStudies(self.conf)]
                self.launch_task_manager(tasks)

            if self.conf['panels_enabled']:
                tasks = [TaskPanels(self.conf)]
                self.launch_task_manager(tasks)

            break

            # phase three
            # for a fixed period of time we:
            # a) update our raw data with Perceval
            # b) get new identities and add them to SH (no merge done)
            # c) convert raw data into enriched data
            tasks = [TaskCollect(self.conf), TaskEnrich(self.conf)]
            a_day = 86400
            self.launch_task_manager(tasks, a_day)

            # FIXME
            # reached this point a new index should be produced
            # or the one we are using should be updated with the Changes
            # for unified identities + affiliations

            # do aliases need to be changed?
            self.update_es_aliases()

def parse_args():

    parser = argparse.ArgumentParser(
        description='Mordred, the friendly friend of p2o',
        epilog='Software metrics for your peace of mind'
        )

    parser.add_argument('-c','--config', help='Configuration file',
        required=True, dest='config_file')

    args = parser.parse_args()
    return args

if __name__ == '__main__':
    args = parse_args()
    obj = Mordred(args.config_file)
    try:
        obj.run()
    except ElasticSearchError as e:
        s = 'Error: %s\n' % str(e)
        sys.stderr.write(s)
        sys.exit(1)
