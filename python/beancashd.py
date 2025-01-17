#!/usr/bin/env python
#
# Copyright (c) 2012 Dave Pifke.
# Copyright (c) 2021 Bean Core www.beancash.org
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to
# deal in the Software without restriction, including without limitation the
# rights to use, copy, modify, merge, publish, distribute, sublicense, and/or
# sell copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
# IN THE SOFTWARE.
#

"""JSON-RPC implementation for talking to Beancashd."""

import base64
import collections
import decimal
try:
    import http.client as httplib
except ImportError:
    import httplib
import json
import logging
import os
import socket
import sys
import time

logger = logging.getLogger('beancash')


class BeancashdException(Exception):
    """Exception thrown for errors talking to Beancashd."""

    def __init__(self, value):
        """Constructor which also logs the exception."""

        super(BeancashdException, self).__init__(value)
        logger.error(value)


class BeancashdCommand(object):
    """Callable object representing a Beancashd JSON-RPC method."""

    def __init__(self, method, server=None):
        """Constructor."""

        self.method = method.lower()
        self.server = server

    def __call__(self, *args):
        """JSON-RPC wrapper."""

        server = self.server
        if not server:
            server = Beancashd()

        return server._rpc_call(self.method, *args)


class Beancashd(object):
    """
    JSON-RPC wrapper for talking to Beancashd.  Methods of instances of this
    object correspond to server commands, e.g. ``beancashd().getnewaddress()``.
    """

    DEFAULT_CONFIG_FILENAME = '~/.BitBean/Beancash.conf'

    def _parse_config(self, filename=DEFAULT_CONFIG_FILENAME, no_cache=False, **options):
        """
        Returns an OrderedDict with the Beancash server configuration.

        Errors are logged; if the configuration file does not exist or could
        not be read, an empty dictionary will be returned; it's up to the
        caller whether or not this is a fatal error.

        :param filename:
            The filename from which the configuration should be read.  Defaults
            to ``~/.BitBean/Beancash.conf``.

        :param no_cache:
            If :const:`True`, the configuration will not be memoized and any
            previously memoized configuration will be ignored.

        Any value from the configuration file can also be passed as an
        argument in order to override the value read from disk.

        """

        if filename in getattr(type(self), '_config_cache', {}) and not no_cache:
            config = type(self)._config_cache[filename].copy()
        else:
            # Note: I would have loved to use Python's ConfigParser for this,
            # but it requires .ini-style section headings.
            config = collections.OrderedDict()
            try:
                with open(os.path.expanduser(filename)) as conf:
                    for lineno, line in enumerate(conf):
                        comment = line.find('#')
                        if comment != -1:
                            line = line[:comment]
                        line = line.strip()
                        if not line:
                            continue

                        try:
                            (var, val) = line.split('=')
                        except ValueError:
                            logger.warning('Could not parse line %d of %s', lineno, filename)
                            continue

                        var = var.rstrip().lower()

                        val = val.lstrip()
                        if val[0] in ('"', "'") and val[1] in ('"', "'"):
                            val = val[1:-1]

                        config[var] = val

                    conf.close()

            except Exception as e:
                logger.error('%s reading %s: %s', type(e).__name__, filename, str(e))

            logger.debug('Read %d parameters from %s', len(config), filename)

            if config and not no_cache:
                # At least one parameter was read; memoize the results:
                if not hasattr(type(self), '_config_cache'):
                    type(self)._config_cache = {}
                type(self)._config_cache[filename] = config.copy()

        config.update(options)
        return config

    def __init__(self, config_filename=DEFAULT_CONFIG_FILENAME, **config_options):
        """
        Constructor.  Parses RPC communication details from ``Beancash.conf``
        and opens a connection to the server.
        """

        config = self._parse_config(config_filename, **config_options)

        try:
            self._rpc_auth = base64.b64encode(':'.join((config['rpcuser'], config['rpcpassword'])).encode('utf8')).decode('utf8')
        except:
            raise BeancashdException('Unable to read RPC credentials from %s' % config_filename)

        self._rpc_host = config.get('rpcserver', '127.0.0.1')
        try:
            socket.gethostbyname(self._rpc_host)
        except socket.error as e:
            raise BeancashdException('Invalid RPC server %s: %s' % (self._rpc_host, str(e)))

        try:
            self._rpc_port = int(config.get('rpcport', 22461))
            timeout = int(config.get('rpctimeout', 30))
        except ValueError:
            raise BeancashdException('Error parsing RPC connection information from %s' % config_filename)

        if config.get('rpcssl', '').lower() in ('1', 'yes', 'true', 'y', 't'):
            logger.debug('Making HTTPS connection to %s:%d', self._rpc_host, self._rpc_port)
            self._rpc_conn = httplib.HTTPSConnection(self._rpc_host, self._rpc_port, timeout=timeout)
        else:
            logger.debug('Making HTTP connection to %s:%d', self._rpc_host, self._rpc_port)
            self._rpc_conn = httplib.HTTPConnection(self._rpc_host, self._rpc_port, timeout=timeout)

        self._rpc_id = 0

    def __getattr__(self, method):
        """
        Attribute getter.  Assumes the attribute being fetched is the name
        of a JSON-RPC method.
        """

        return BeancashdCommand(method, self)

    def _rpc_call(self, method, *args):
        """Performs a JSON-RPC command on the server and returns the result."""

        # The Bean Cash protocol specifies a incremental sequence for each
        # command.
        self._rpc_id += 1

        logger.debug('Starting "%s" JSON-RPC request', method)
        self._rpc_conn.connect()
        self._rpc_conn.request(
            method='POST',
            url='/',
            body=json.dumps({
                'version': '1.1',
                'method': method,
                'params': args,
                'id': self._rpc_id,
            }),
            headers={
                'Host': '%s:%d' % (self._rpc_host, self._rpc_port),
                'Authorization': ''.join(('Basic ', self._rpc_auth)),
                'Content-Type': 'application/json',
            }
        )

        start = time.time()
        response = self._rpc_conn.getresponse()
        if not response:
            raise BeancashdException('No response from Beancashd')
        if response.status != 200:
            raise BeancashdException('%d (%s) response from Beancashd' % (response.status, response.reason))

        response_body = response.read().decode('utf8')
        logger.debug('Got %d byte response from server in %d ms', len(response_body), (time.time() - start) * 1000.0)
        try:
            response_json = json.loads(response_body, parse_float=decimal.Decimal)
        except ValueError as e:
            raise BeancashdException('Error parsing Beancashd response: %s' % str(e))

        if response_json.get('error'):
            raise BeancashdException(response_json['error'])
        elif 'result' in response_json:
            return response_json['result']
        else:
            raise BeancashdException('Invalid response from Beancashd')


# There are two ways to use this module: either instantiate a Beancashd and
# call the JSON-RPC methods as methods of the instance, or use the
# module-level shortcuts below.  The former reuses the same connection,
# while the latter creates a new connection on each call.  The latter is also
# dependent upon the following list being up-to-date.
addmultisigaddress = BeancashdCommand('addmultisigaddress')
backupwallet = BeancashdCommand('backupwallet')
dumpprivkey = BeancashdCommand('dumpprivkey')
encryptwallet = BeancashdCommand('encryptwallet')
getaccount = BeancashdCommand('getaccount')
getaccountaddress = BeancashdCommand('getaccountaddress')
getaddressesbyaccount = BeancashdCommand('getaddressesbyaccount')
getbalance = BeancashdCommand('getbalance')
getblock = BeancashdCommand('getblock')
getblockcount = BeancashdCommand('getblockcount')
getblockhash = BeancashdCommand('getblockhash')
getconnectioncount = BeancashdCommand('getconnectioncount')
getdifficulty = BeancashdCommand('getdifficulty')
# getgenerate = BeancashdCommand('getgenerate')
# gethashespersec = BeancashdCommand('gethashespersec')
getinfo = BeancashdCommand('getinfo')
# getmemorypool = BeancashdCommand('getmemorypool')
getmininginfo = BeancashdCommand('getmininginfo')
getnewaddress = BeancashdCommand('getnewaddress')
getreceivedbyaccount = BeancashdCommand('getreceivedbyaccount')
getreceivedbyaddress = BeancashdCommand('getreceivedbyaddress')
gettransaction = BeancashdCommand('gettransaction')
getwork = BeancashdCommand('getwork')
help = BeancashdCommand('help')
importprivkey = BeancashdCommand('importprivkey')
keypoolrefill = BeancashdCommand('keypoolrefill')
listaccounts = BeancashdCommand('listaccounts')
listreceivedbyaccount = BeancashdCommand('listreceivedbyaccount')
listreceivedbyaddress = BeancashdCommand('listreceivedbyaddress')
listsinceblock = BeancashdCommand('listsinceblock')
listtransactions = BeancashdCommand('listtransactions')
move = BeancashdCommand('move')
sendfrom = BeancashdCommand('sendfrom')
sendmany = BeancashdCommand('sendmany')
sendtoaddress = BeancashdCommand('sendtoaddress')
setaccount = BeancashdCommand('setaccount')
# setgenerate = BeancashdCommand('setgenerate')
settxfee = BeancashdCommand('settxfee')
signmessage = BeancashdCommand('signmessage')
stop = BeancashdCommand('stop')
validateaddress = BeancashdCommand('validateaddress')
verifymessage = BeancashdCommand('verifymessage')


if __name__ == '__main__':
    logging.basicConfig()
    # Un-comment for verbosity:
    #logging.getLogger().setLevel(logging.DEBUG)

    if len(sys.argv) > 1:
        method_name = sys.argv[1]
    else:
        method_name = 'help'

    try:
        print(getattr(Beancashd(), method_name)(*sys.argv[2:]))
    except BeancashdException:
        sys.exit(1)
    else:
        sys.exit(0)

# eof
