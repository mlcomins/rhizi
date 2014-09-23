#!/usr/bin/python

import os
import json
import re
import logging
import traceback

import urllib2

import neo4j_util as dbu

log = logging.getLogger('rhizi')

class DB_op(object):
    """
    tx wrapped DB operation possibly composing multiple DB queries
    """

    def __init__(self):
        self.s_id = 0  # statement id counter
        self.id_to_statement_map = {}  # zero based id to statement map
        self.tx_id = None
        self.tx_commit_url = None  # cached from response to tx begin

    def begin(self, tx_open_url):
        try:
            #
            # [!] neo4j seems picky about receiving an additional empty statement list
            #
            data = data = dbu.statement_set_to_REST_form([])
            ret = dbu.post_neo4j(tx_open_url, data)
            tx_commit_url = ret['commit']
            self.parse_tx_id(tx_commit_url)
            self.tx_commit_url = tx_commit_url

            log.debug('tx-open: id: {0}, commit-url: {1}'.format(self.tx_id, tx_commit_url))
        except Exception as e:
            raise Exception('failed to open transaction:' + e.message)

    def parse_tx_id(self, tx_commit_url):
        m = re.search('/(?P<id>\d+)/commit$', tx_commit_url)
        id_str = m.group('id')
        self.tx_id = int(id_str)

    def commit(self):
        try:
            #
            # [!] neo4j seems picky about receiving an additional empty statement list
            #
            data = dbu.statement_set_to_REST_form([])
            ret = dbu.post(self.tx_commit_url, data)
        except Exception as e:
            raise Exception('failed to commit transaction:' + e.message)

        log.debug('tx-commit: id: {0}, commit-url: {1}'.format(self.tx_id, self.tx_commit_url))

    def add_statement(self, cypher_query, params={}):
        """
        add a DB query language statement
        @return: statement id
        """
        ret = self.s_id
        self.id_to_statement_map[self.s_id] = dbu.statement_to_REST_form(cypher_query, params)
        self.s_id = self.s_id + 1
        return ret

    @property
    def statement_set(self):
        return self.id_to_statement_map.values()

    def on_success(self, data):
        pass

    def on_error(self):
        pass

class DBO_add_node_set(DB_op):
    """
    DB op: add node set
    
    @param node_map: node-type to node list map
    @input_to_DB_property_map: optional function which takes a map of input properties and returns a map of DB properties - use to map input schemas to DB schemas
    
    """
    def __init__(self, node_map, input_to_DB_property_map=lambda _: _):
        super(DBO_add_node_set, self).__init__()
        self.node_map = node_map

        for type, n_set in self.node_map.items():
            q = "create (n:{0} {{prop_dict}}) return id(n)".format(type)
            for n_prop_dict in n_set:
                p = {'prop_dict' : input_to_DB_property_map(n_prop_dict)}
                self.add_statement(q, p)

    def on_success(self, data):
        # [!] fragile - parse results
        # sample input: dict: {u'errors': [], u'results': [{u'data': [{u'row': [20]}], u'columns': [u'id(n)']}]}
        id_set = []
        for r in data['results']:
            columns = r['columns']
            for k in r['data']:
                nid = k['row'][0]
                id_set.append(nid)

        log.debug('node-set added: ids: ' + str(id_set))
        return id_set

class DBO_load_node_id_set(DB_op):
    """
    load node id set, filter by type / properties
    """
    def __init__(self, filter_type, filter_prop=None):
        super(DBO_load_node_id_set, self).__init__()

        # build where clause if necessary
        filter_prop_str = ""
        if filter_prop:
            filter_prop_arr = []
            for k, v in filter_prop:
                v_str = str(v)
                if isinstance(v, str):
                    # quote string values
                    v_str = "'{0}'".format(v_str)
                filter_prop_arr.append("n.{0} = {1} and ".format(k, v_str))
            filter_prop_str = " where " + " and ".join(filter_prop_arr)

        q = "match (n:{0}){1} return id(n)".format(filter_type, filter_prop_str)
        self.add_statement(q)

    def on_success(self, data):
        # [!] fragile - parse results
        # sample input: dict: {u'errors': [], u'results': [{u'data': [{u'row': [20]}], u'columns': [u'id(n)']}]}
        id_set = []
        for r in data['results']:
            columns = r['columns']
            for k in r['data']:
                nid = k['row'][0]
                id_set.append(nid)

        log.debug('loaded node id set: ' + str(id_set))
        return id_set

class DB_Controller:
    """
    neo4j DB controller
    """
    def __init__(self, config):
        self.config = config

    def log_committed_queries(self, statement_set):
        for sp_dict in statement_set['statements']:
            log.debug('\tq: {0}'.format(sp_dict['statement']))

    def exec_op(self, op):
        """
        execute operation within a DB transaction
        """
        tx_base_url = self.config.db_base_url + '/db/data/transaction'
        statement_set = dbu.statement_set_to_REST_form(op.statement_set)

        try:
            op.begin(tx_base_url)
            tx_url = "{0}/{1}".format(tx_base_url, op.tx_id)
            ret_tx = dbu.post_neo4j(tx_url, statement_set)
            op.commit()
            self.log_committed_queries(statement_set)
            return op.on_success(ret_tx)
        except Exception as e:
            log.error(e.message)
            log.error(traceback.print_exc())
            op.on_error()

    def create_db_op(self, f_work, f_cont):
        ret = DB_op(f_work, f_cont)
        return ret

    def exec_cypher_query(self, q):
        """
        @deprecated: use transaction based api
        """
        self.post_neo4j('/db/data/cypher', {"query" : q})
