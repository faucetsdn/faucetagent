#!/usr/bin/env python3
r"""

FAUCET configuration agent

Provides a simple gNMI/gRPC interface to support
updating FAUCET's configuration file. This file
must be accessible to both FAUCET and the agent.

By default, a HUP signal is sent to FAUCET to
trigger a config file reload.

If FAUCET is run with FAUCET_CONFIG_STAT_RELOAD=1,
then it should reload automatically, and the agent
may be run with the --nohup option.

This allows FAUCET and the agent to be run in separate
containers/pid namespaces if desired, though they must
share the directory where the config file is located.

---
EXAMPLES

Starting the Agent:

The agent must be invoked with a TLS certificate and private key,
and the path to FAUCET's configuration file.

./faucetagent.py --cert agent.crt --key agent.key \
    --configfile /etc/faucet.yaml  >& faucetagent.log &

Talking to the Agent with gNMI:

It should be possible to talk to the agent using
gNMI utilities such as gnmi_{capabilities,get,set}.

# TLS authentication (client auth is ignored by agent atm)
# this must be set appropriately for your environment
AUTH="-ca ca.crt -cert client.crt -key client.key -target_name localhost"

# Extract string_val from gnmi_get output
string_val() { grep string_val: | awk -F 'string_val: "' '{printf $2;}'  |
               sed -e 's/"$//' | xargs -0 printf; }

# Fetch information about configuration schema
gnmi_capabilities $AUTH

# Fetch current configuration
gnmi_get $AUTH -xpath=/ | string_val

# Send a configuration file to FAUCET
gnmi_set $AUTH -replace=/:"$(<faucet.yaml)"

"""

from argparse import ArgumentParser, RawDescriptionHelpFormatter
from collections import namedtuple
from concurrent import futures
from logging import basicConfig as logConfig, getLogger, DEBUG, INFO
from os.path import abspath
from shutil import which
from subprocess import run
from time import sleep, time
import hashlib
import sys

import requests
import grpc
from grpc import ssl_server_credentials
from prometheus_client.parser import text_string_to_metric_families
from gnmi_pb2_grpc import gNMIServicer, add_gNMIServicer_to_server
from gnmi_pb2 import (CapabilityResponse, GetResponse, SetResponse, ModelData,
                      UpdateResult, JSON)

# Semantic version of this FAUCET configuration agent
VERSION = "0.1"

# Utility functions


def pathtostr(path):
    """Return string for simple Path /a/b/c"""
    return '/' + '/'.join(e.name for e in path.elem)


def timestamp():
    """Return a gNMI timestamp (int nanoseconds)"""
    seconds = time()
    return int(seconds * 1e9)


def checkdeps():
    """Check external dependencies"""
    cmds = [('fuser', 'psmisc')]
    for cmd, pkg in cmds:
        if not which(cmd):
            error('Missing "%s" command from %s; exiting', cmd, pkg)
            sys.exit(1)


# Logging

LOG = getLogger('faucetagent')
# pylint: disable=invalid-name
debug, info, warning, error = LOG.debug, LOG.info, LOG.warning, LOG.error

# Interface to FAUCET


class FaucetProxy:
    """Abstraction for communicating with FAUCET"""

    # pylint: disable=too-many-arguments
    def __init__(self,
                 path='/etc/faucet.yaml',
                 prometheus_addr='http://localhost',
                 prometheus_port=9302,
                 timeout=120,
                 dp_wait_fraction=0.0,
                 nohup=False):
        """path: path to FAUCET's config file ('/etc/faucet.yaml')
           prometheus_addr: FAUCET's prometheus address (http://localhost)
           prometheus_port: FAUCET's local prometheus port (9302)
           timeout: config reload timeout in seconds (120)
           dp_wait_fraction: fraction of DP updates to wait for (0.0)
           nohup: don't send HUP to reload FAUCET config? (False)"""
        self.path = abspath(path)
        self.prometheus_port = prometheus_port
        self.prometheus_url = '{0}:{1}'.format(prometheus_addr,
                                               prometheus_port)
        self.timeout = timeout
        self.dp_wait_fraction = dp_wait_fraction
        self.nohup = nohup

    def read_config(self):
        """Return FAUCET config file contents and timestamp"""
        now = timestamp()
        with open(self.path) as config:
            data = config.read()
        return data, now

    # FAUCET status fields we care about
    labelFields = ('faucet_config_hash_info', 'faucet_config_hash_func')
    valueFields = ('faucet_config_load_error', 'faucet_config_applied')
    statusFields = labelFields + valueFields
    StatusTuple = namedtuple('StatusTuple', statusFields)

    # Default values to use if a field is missing
    defaultValues = {
        'faucet_config_applied': 1.0,
        'faucet_config_hash_info': {},
    }

    @staticmethod
    def parse_line(line):
        "Parse single line of prometheus output"
        for family in text_string_to_metric_families(line):
            for sample in family.samples:
                # Return first (and only) sample
                return sample

    def fetch_status(self):
        """Fetch and return FAUCET status via prometheus port"""
        try:
            request = requests.get(self.prometheus_url)
        except ConnectionError:
            return None

        # pylint: disable=no-member
        if request.status_code != requests.codes.ok:
            raise IOError('Error %d fetching FAUCET status from %s' %
                          request.status_code, self.prometheus_url)

        sdict = {}
        for line in request.text.split('\n'):
            if line.startswith('#') or 'faucet_config_' not in line:
                continue
            sample = self.parse_line(line)
            if not sample:
                continue
            name, labels, value = sample.name, sample.labels, sample.value
            if name in self.statusFields:
                sdict[name] = labels if name in self.labelFields else value

        # Handle missing values
        for field, value in self.defaultValues.items():
            if field not in sdict:
                debug('%s not found (possibly unsupported?)', field)
                sdict[field] = value

        status = self.StatusTuple(**sdict)
        debug('fetch_status: %s', status)
        return status

    def _check_hash(self, status, config):
        """Return True if FAUCET's config hash == our config hash"""
        # Get FAUCET's config file name and hash value
        files = status.faucet_config_hash_info['config_files'].split(',')
        hashes = status.faucet_config_hash_info['hashes'].split(',')
        if len(files) != 1 or len(hashes) != 1:
            return False
        config_file, hash_value = files[0], hashes[0]
        if abspath(config_file) != self.path:
            warning("FAUCET config file %s may not be %s", config_file,
                    self.path)
        # Get hash function
        hash_funcs = list(status.faucet_config_hash_func.values())
        if len(hash_funcs) != 1:
            return False
        hash_func = getattr(hashlib, hash_funcs[0], None)
        if not hash_func:
            return False
        # Verify config file hash matches value from FAUCET
        if hash_value == hash_func(config.encode('utf-8')).hexdigest():
            debug('_check_hash: hashes match')
            return True
        return False

    def _check_applied(self, status):
        """Return True if fraction of applied datapaths exceeds threshold"""
        # Optionally wait for "applied" (aka enqueued) fraction
        debug('%.0f%% >= %.0f%% of datapaths configured',
              status.faucet_config_applied * 100.0,
              self.dp_wait_fraction * 100.0)
        return status.faucet_config_applied >= self.dp_wait_fraction

    def _check_status(self, status, config):
        """Return True if config has been successfully (re)loaded"""
        return (status and self._check_hash(status, config)
                and not status.faucet_config_load_error
                and self._check_applied(status))

    def reload(self, config):
        """Signal FAUCET and wait for it to load our config"""
        debug('Reloading FAUCET config')

        if not self.nohup:
            # Send HUP to tell FAUCET to reload the config file
            debug('Sending HUP (config reload signal) to FAUCET')
            cmd = 'fuser -k -HUP %d/tcp' % self.prometheus_port
            run(cmd.split(), check=True)

        # Wait for FAUCET to reload config
        debug('Waiting for FAUCET config (re)load')
        start = time()
        while time() - start < self.timeout:
            status = self.fetch_status()
            if self._check_status(status, config):
                # Success!
                return
            # Wait a bit before trying again
            sleep(1)
        raise RuntimeError('Timeout during FAUCET config reload')

    def write_config(self, data):
        """Write FAUCET config file and tell FAUCET to reload it"""
        debug('Writing FAUCET config')
        # Write configuration file
        with open(self.path, 'w') as config:
            config.write(data)
        # Verify it was written properly
        with open(self.path) as config:
            newdata = config.read()
        if newdata != data:
            raise IOError(
                'Configuration file %s not written properly.' % self.path)
        # Tell FAUCET to reload its configuration
        self.reload(config=data)


# Interface to gNMI


class FaucetAgent(gNMIServicer):
    """Faucet gNMI agent"""

    def __init__(self, faucetProxy):
        """faucetProxy: FaucetProxy() object"""
        gNMIServicer.__init__(self)
        self.faucet = faucetProxy

    def Capabilities(self, request, context):
        """Return gNMI schema information"""
        debug('Capabilities()')
        response = CapabilityResponse()
        # Configuration schema (aka "model") that we support
        model = ModelData(
            name='FAUCET', organization='faucet.nz', version='1.0')
        # pylint: disable=no-member
        response.supported_models.extend([model])
        response.supported_encodings.extend(JSON)
        # Version of this gNMI agent
        response.gNMI_version = VERSION
        return response

    @staticmethod
    def validate(path, context):
        """Validate that path is /"""
        path = pathtostr(path)
        if path != '/':
            context.set_code(grpc.StatusCode.NOT_FOUND)
            message = 'path "%s" not found: should be "/"' % path
            context.set_details(message)
            raise ValueError(message)

    def Get(self, request, context):
        """Return FAUCET configuration"""
        debug('Get(%s)', request)
        # We support a single request for now
        self.validate(request.path[0], context)
        response = GetResponse()
        data, now = self.faucet.read_config()
        # pylint: disable=no-member
        notification = response.notification.add(timestamp=now)
        update = notification.update.add()
        update.val.string_val = data
        return response

    def Set(self, request, context):
        """Write FAUCET configuration"""
        debug('Set(%s)', request)
        # We do not support delete/update/extension operations
        for field in 'delete', 'update', 'extension':
            if getattr(request, field):
                context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
                message = ('"%s" unsupported - should be "replace"' % field)
                context.set_details(message)
                raise ValueError(message)
        # We support a single replace request
        if len(request.replace) != 1:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            message = ('single replace request required')
            context.set_details(message)
            raise ValueError(message)
        replace = request.replace[0]
        self.validate(replace.path, context)
        if not hasattr(replace.val, 'string_val'):
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            message = 'string value (configuration file data) required'
            context.set_details(message)
            raise ValueError(message)
        # Write FAUCET configuration
        result = UpdateResult(
            timestamp=timestamp(), path=replace.path, op='REPLACE')
        try:
            self.faucet.write_config(replace.val.string_val)
        except ConnectionError:
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            result.message = ('failed to connect to FAUCET at %s' %
                              self.faucet.prometheus_url)
            # pylint: disable=no-member
            context.set_details(result.message)
        return SetResponse(response=[result])


# Do it!


def serve(cert_file, key_file, gnmi_url, servicer, max_workers=10):
    """Create and run a gNMI service"""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
    add_gNMIServicer_to_server(servicer, server)
    with open(cert_file) as certs:
        cert = certs.read().encode('utf8')
    with open(key_file) as keys:
        key = keys.read().encode('utf8')
    credentials = ssl_server_credentials([(key, cert)])
    server.add_secure_port(gnmi_url, credentials)
    server.start()
    try:
        while True:
            sleep(3600)  # how long should we sleep? Hmm
    except KeyboardInterrupt:
        server.stop(0)


def parse():
    """Parse command line arguments"""
    # Use docstring at the top of this file
    desc, examples = globals()['__doc__'].split('---')
    parser = ArgumentParser(
        description=desc,
        epilog=examples,
        formatter_class=RawDescriptionHelpFormatter)
    arg = parser.add_argument
    arg('--cert', required=True, help='certificate file')
    arg('--key', required=True, help='private key file')
    arg('--configfile', required=True, help='FAUCET config file')
    arg('--gnmiaddr', default='[::]', help='gNMI address to listen on ([::])')
    arg('--gnmiport', type=int, default=9339, help='gNMI port (9339)')
    arg('--promaddr',
        default='http://localhost',
        help='FAUCET prometheus address (http://localhost)')
    arg('--promport',
        type=int,
        default=9302,
        help='FAUCET prometheus port (9302)')
    arg('--dpwait',
        metavar='fraction',
        type=float,
        default=0.0,
        help='Wait for FAUCET to attempt to update this fraction of DPs')
    arg('--timeout', default=120, help='max time(s) to wait for config reload')
    arg('--nohup',
        action='store_true',
        help='Do not use HUP to reload FAUCET config file on Set operation')
    arg('-v', '--version', action='version', version=VERSION)
    return parser.parse_args()


def main():
    """Parse arguments and run FAUCET gNMI agent"""
    args = parse()
    checkdeps()

    # FaucetProxy talks to FAUCET and manages configfile
    proxy = FaucetProxy(
        path=args.configfile,
        prometheus_addr=args.promaddr,
        prometheus_port=args.promport,
        dp_wait_fraction=args.dpwait,
        timeout=args.timeout,
        nohup=args.nohup)

    # FaucetAgent handles gNMI requests
    agent = FaucetAgent(proxy)

    # Start the FAUCET gNMI service
    gnmi_url = '{0}:{1}'.format(args.gnmiaddr, args.gnmiport)
    info('Starting FAUCET gNMI configuration agent on %s', gnmi_url)
    serve(
        cert_file=args.cert,
        key_file=args.key,
        gnmi_url=gnmi_url,
        servicer=agent)

    info('FAUCET gNMI configuration agent exiting')
    sys.exit(0)


if __name__ == '__main__':
    logConfig(level=DEBUG)
    getLogger('urllib3.connectionpool').setLevel(INFO)
    main()
