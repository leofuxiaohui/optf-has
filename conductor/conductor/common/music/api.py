#
# -------------------------------------------------------------------------
#   Copyright (c) 2015-2017 AT&T Intellectual Property
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
# -------------------------------------------------------------------------
#

"""Music Data Store API"""

import copy
import time

from oslo_config import cfg
from oslo_log import log

from conductor.common import rest
from conductor.i18n import _LE, _LI  # pylint: disable=W0212

LOG = log.getLogger(__name__)

CONF = cfg.CONF

global MUSIC_API

MUSIC_API_OPTS = [
    cfg.StrOpt('server_url',
               default='http://controller:8080/MUSIC/rest',
               help='Base URL for Music REST API without a trailing slash.'),
    cfg.ListOpt('hostnames',
                deprecated_for_removal=True,
                deprecated_reason='Use server_url instead',
                help='List of hostnames (round-robin access)'),
    cfg.IntOpt('port',
               deprecated_for_removal=True,
               deprecated_reason='Use server_url instead',
               help='Port'),
    cfg.StrOpt('path',
               deprecated_for_removal=True,
               deprecated_reason='Use server_url instead',
               help='Path'),
    cfg.FloatOpt('connect_timeout',
                 default=3.05,
                 help='Socket connection timeout'),
    cfg.FloatOpt('read_timeout',
                 default=12.05,
                 help='Socket read timeout'),
    cfg.IntOpt('lock_timeout',
               default=10,
               help='Lock timeout'),
    cfg.IntOpt('replication_factor',
               default=1,
               help='Replication factor'),
    cfg.BoolOpt('debug',
                default=False,
                help='Log debug messages. Default value is False.'),
    cfg.BoolOpt('mock',
                default=False,
                help='Use mock API'),
]

CONF.register_opts(MUSIC_API_OPTS, group='music_api')


class MusicAPI(object):
    """Wrapper for Music API"""

    lock_ids = None  # Cache of lock ids, indexed by name
    lock_timeout = None  # Maximum time in seconds to acquire a lock

    rest = None  # API Endpoint
    replication_factor = None  # Number of Music nodes to replicate across

    def __init__(self):
        """Initializer."""
        global MUSIC_API

        LOG.info(_LI("Initializing Music API"))
        server_url = CONF.music_api.server_url.rstrip('/')
        if not server_url:
            # host/port/path are deprecated and should not be used anymore.
            # Defaults removed from oslo_config to give more incentive.

            # No more round robin either. Just take the first entry.
            host = next(iter(CONF.music_api.hostnames or []), 'controller')
            port = CONF.music_api.port or 8080
            path = CONF.music_api.path or '/MUSIC/rest'
            server_url = 'http://{}:{}/{}'.format(
                host, port, path.rstrip('/').lstrip('/'))

        kwargs = {
            'server_url': server_url,
            'log_debug': CONF.music_api.debug,
            'connect_timeout': CONF.music_api.connect_timeout,
            'read_timeout': CONF.music_api.read_timeout,
        }
        self.rest = rest.REST(**kwargs)

        self.lock_ids = {}

        # TODO(jdandrea): Allow override at creation time.
        self.lock_timeout = CONF.music_api.lock_timeout
        self.replication_factor = CONF.music_api.replication_factor

        MUSIC_API = self

    def __del__(self):
        """Deletion."""
        if type(self.lock_ids) is dict:
            for lock_name in self.lock_ids.keys():
                self.lock_delete(lock_name)

    @staticmethod
    def _row_url_path(keyspace, table, pk_name, pk_value):
        """Returns a Music-compliant row URL path."""
        path = '/keyspaces/%(keyspace)s/tables/%(table)s/rows' % {
            'keyspace': keyspace,
            'table': table,
        }

        if pk_name and pk_value:
            path += '?%s=%s' % (pk_name, pk_value)
        return path

    @staticmethod
    def _lock_name_generate(keyspace, table, pk_value):
        """Generate a lock name."""

        # The Music API dictates that the lock name must be of the form
        # keyspace.table.primary_key
        lock_name = '%(keyspace)s.%(table)s.%(primary_key)s' % {
            'keyspace': keyspace,
            'table': table,
            'primary_key': pk_value,
        }
        return lock_name

    def _lock_id_create(self, lock_name):
        """Returns the lock id. Use for acquiring and releasing."""
        path = '/locks/create/%s' % lock_name
        response = self.rest.request(method='post',
                                     content_type='text/plain', path=path)
        lock_id = None
        if response and response.ok:
            lock_id = response.text
        return lock_id

    def _lock_id_acquire(self, lock_id):
        """Acquire a lock by id. Returns True if successful."""
        path = '/locks/acquire/%s' % lock_id
        response = self.rest.request(method='get',
                                     content_type='text/plain', path=path)
        status = False
        if response and response.ok:
            status = (response.text.lower() == 'true')
        return status

    def _lock_id_release(self, lock_id):
        """Release a lock by id. Returns True if successful."""
        path = '/locks/release/%s' % lock_id
        response = self.rest.request(method='delete',
                                     content_type='text/plain', path=path)
        return response and response.ok

    def payload_init(self, keyspace=None, table=None,
                     pk_value=None, atomic=False):
        """Initialize payload for Music requests.

        Supports atomic operations.
        Returns a payload of data and lock_name (if any).
        """
        if atomic:
            lock_name = self.lock_create(keyspace, table, pk_value)
        else:
            lock_name = None

        lock_id = self.lock_ids.get(lock_name)
        data = {
            'consistencyInfo': {
                'type': 'atomic' if atomic else 'eventual',
                'lockId': lock_id,
            }
        }
        return {'data': data, 'lock_name': lock_name}

    def payload_delete(self, payload):
        """Delete payload for Music requests. Cleans up atomic operations."""

        # Doesn't actually delete the payload.
        # We just delete the lock inside of it!
        # This way payload_init/payload_delete is paired up neatly.
        lock_name = payload.get('lock_name')
        if lock_name:
            self.lock_delete(lock_name)

    def keyspace_create(self, keyspace):
        """Creates a keyspace."""
        payload = self.payload_init()
        data = payload.get('data')
        data['durabilityOfWrites'] = True
        data['replicationInfo'] = {
            'class': 'SimpleStrategy',
            'replication_factor': self.replication_factor,
        }

        path = '/keyspaces/%s' % keyspace
        if CONF.music_api.debug:
            LOG.debug("Creating keyspace {}".format(keyspace))
        response = self.rest.request(method='post', path=path, data=data)
        return response and response.ok

    def keyspace_delete(self, keyspace):
        """Drops a keyspace."""
        payload = self.payload_init()
        data = payload.get('data')

        path = '/keyspaces/%s' % keyspace
        if CONF.music_api.debug:
            LOG.debug("Deleting keyspace {}".format(keyspace))
        response = self.rest.request(method='delete', path=path, data=data)
        return response and response.ok

    def lock_create(self, keyspace, table, pk_value):
        """Create and acquire a lock. Returns a lock name."""

        # Generate the lock name, then create/acquire the lock id.
        lock_name = self._lock_name_generate(keyspace, table, pk_value)
        if CONF.music_api.debug:
            LOG.debug("Creating lock {}".format(lock_name))
        lock_id = self._lock_id_create(lock_name)
        time_now = time.time()
        while not self._lock_id_acquire(lock_id):
            if time.time() - time_now > self.lock_timeout:
                raise IndexError(
                    _LE('Lock id acquire timeout: %s') % lock_name)

        # Cache the lock name/id.
        self.lock_ids[lock_name] = lock_id
        return lock_name

    def lock_release(self, lock_name):
        """Release lock by name. Returns True if successful"""

        # No need to delete the lock. lock_create() will not complain
        # if a lock with the same name is created later.
        if CONF.music_api.debug:
            LOG.debug("Releasing lock {}".format(lock_name))
        if lock_name:
            return self._lock_id_release(self.lock_ids.get(lock_name))

    def lock_delete(self, lock_name):
        """Delete a lock by name. Returns True if successful."""
        path = '/locks/delete/%s' % lock_name
        if CONF.music_api.debug:
            LOG.debug("Deleting lock {}".format(lock_name))
        response = self.rest.request(content_type='text/plain',
                                     method='delete', path=path)
        if response and response.ok:
            del self.lock_ids[lock_name]
        return response and response.ok

    def row_create(self, keyspace, table,  # pylint: disable=R0913
                   pk_name, pk_value, values, atomic=False):
        """Create a row."""
        payload = self.payload_init(keyspace, table, pk_value, atomic)
        data = payload.get('data')
        data['values'] = values

        path = '/keyspaces/%(keyspace)s/tables/%(table)s/rows' % {
            'keyspace': keyspace,
            'table': table,
        }
        if CONF.music_api.debug:
            LOG.debug("Creating row with pk_value {} in table "
                      "{}, keyspace {}".format(pk_value, table, keyspace))
        response = self.rest.request(method='post', path=path, data=data)
        self.payload_delete(payload)
        return response and response.ok

    def row_update(self, keyspace, table,  # pylint: disable=R0913
                   pk_name, pk_value, values, atomic=False):
        """Update a row."""
        payload = self.payload_init(keyspace, table, pk_value, atomic)
        data = payload.get('data')
        data['values'] = values

        path = self._row_url_path(keyspace, table, pk_name, pk_value)
        if CONF.music_api.debug:
            LOG.debug("Updating row with pk_value {} in table "
                      "{}, keyspace {}".format(pk_value, table, keyspace))
        response = self.rest.request(method='put', path=path, data=data)
        self.payload_delete(payload)
        return response and response.ok

    def row_read(self, keyspace, table, pk_name=None, pk_value=None):
        """Read one or more rows. Not atomic."""
        path = self._row_url_path(keyspace, table, pk_name, pk_value)
        if CONF.music_api.debug:
            LOG.debug("Reading row with pk_value {} from table "
                      "{}, keyspace {}".format(pk_value, table, keyspace))
        response = self.rest.request(path=path)
        return response and response.json()

    def row_delete(self, keyspace, table, pk_name, pk_value, atomic=False):
        """Delete a row."""
        payload = self.payload_init(keyspace, table, pk_value, atomic)
        data = payload.get('data')

        path = self._row_url_path(keyspace, table, pk_name, pk_value)
        if CONF.music_api.debug:
            LOG.debug("Deleting row with pk_value {} from table "
                      "{}, keyspace {}".format(pk_value, table, keyspace))
        response = self.rest.request(method='delete', path=path, data=data)
        self.payload_delete(payload)
        return response and response.ok

    @staticmethod
    def _table_path_generate(keyspace, table):
        path = '/keyspaces/%(keyspace)s/tables/%(table)s/' % {
            'keyspace': keyspace,
            'table': table,
        }
        return path

    def table_create(self, keyspace, table, schema):
        """Creates a table."""
        payload = self.payload_init()
        data = payload.get('data')
        data['fields'] = schema

        path = self._table_path_generate(keyspace, table)
        if CONF.music_api.debug:
            LOG.debug("Creating table {}, keyspace {}".format(table, keyspace))
        response = self.rest.request(method='post', path=path, data=data)
        return response and response.ok

    def table_delete(self, keyspace, table):
        """Creates a table."""
        payload = self.payload_init()
        data = payload.get('data')

        path = self._table_path_generate(keyspace, table)
        if CONF.music_api.debug:
            LOG.debug("Deleting table {}, keyspace {}".format(table, keyspace))
        response = self.rest.request(method='delete', path=path, data=data)
        return response and response.ok

    def version(self):
        """Returns version string."""
        path = '/version'
        if CONF.music_api.debug:
            LOG.debug("Requesting version info")
        response = self.rest.request(method='get',
                                     content_type='text/plain', path=path)
        return response and response.text


class MockAPI(object):
    """Wrapper for Music API"""

    # Mock state for Music
    music = {
        'keyspaces': {}
    }

    def __init__(self):
        """Initializer."""
        LOG.info(_LI("Initializing Music Mock API"))

        global MUSIC_API

        self.music['keyspaces'] = {}

        MUSIC_API = self

    @property
    def _keyspaces(self):
        return self.music.get('keyspaces')

    def _set_keyspace(self, keyspace):
        self._keyspaces[keyspace] = {}

    def _unset_keyspace(self, keyspace):
        self._keyspaces.pop(keyspace)

    def _set_table(self, keyspace, table):
        self._keyspaces[keyspace][table] = {}

    def _unset_table(self, keyspace, table):
        self._keyspaces[keyspace].pop(table)

    def _get_row(self, keyspace, table, key=None):
        rows = {}
        row_num = 0
        for row_key, row in self._keyspaces[keyspace][table].items():
            if not key or key == row_key:
                row_num += 1
                rows['row {}'.format(row_num)] = copy.deepcopy(row)
        return rows

    def _set_row(self, keyspace, table, key, row):
        self._keyspaces[keyspace][table][key] = row

    def _unset_row(self, keyspace, table, row):
        self._keyspaces[keyspace][table].pop(row)

    def keyspace_create(self, keyspace):
        """Creates a keyspace."""
        if CONF.music_api.debug:
            LOG.debug("Creating keyspace {}".format(keyspace))
        self._set_keyspace(keyspace)
        return True

    def keyspace_delete(self, keyspace):
        """Drops a keyspace."""
        if CONF.music_api.debug:
            LOG.debug("Deleting keyspace {}".format(keyspace))
        self._unset_keyspace(keyspace)
        return True

    def row_create(self, keyspace, table,  # pylint: disable=R0913
                   pk_name, pk_value, values, atomic=False):
        """Create a row."""
        if CONF.music_api.debug:
            LOG.debug("Creating row with pk_value {} in table "
                      "{}, keyspace {}".format(pk_value, table, keyspace))
        self._set_row(keyspace, table, pk_value, values)
        return True

    def row_update(self, keyspace, table,  # pylint: disable=R0913
                   pk_name, pk_value, values, atomic=False):
        """Update a row."""
        if CONF.music_api.debug:
            LOG.debug("Updating row with pk_value {} in table "
                      "{}, keyspace {}".format(pk_value, table, keyspace))
        self._set_row(keyspace, table, pk_value, values)
        return True

    def row_read(self, keyspace, table, pk_name=None, pk_value=None):
        """Read one or more rows. Not atomic."""
        if CONF.music_api.debug:
            LOG.debug("Reading row with pk_value {} from table "
                      "{}, keyspace {}".format(pk_value, table, keyspace))
        values = self._get_row(keyspace, table, pk_value)
        return values

    def row_delete(self, keyspace, table, pk_name, pk_value, atomic=False):
        """Delete a row."""
        if CONF.music_api.debug:
            LOG.debug("Deleting row with pk_value {} from table "
                      "{}, keyspace {}".format(pk_value, table, keyspace))
        self._unset_row(keyspace, table, pk_value)
        return True

    def table_create(self, keyspace, table, schema):
        """Creates a table."""
        if CONF.music_api.debug:
            LOG.debug("Creating table {}, keyspace {}".format(table, keyspace))
        self._set_table(keyspace, table)
        return True

    def table_delete(self, keyspace, table):
        """Creates a table."""
        if CONF.music_api.debug:
            LOG.debug("Deleting table {}, keyspace {}".format(table, keyspace))
        self._unset_table(keyspace, table)
        return True

    def version(self):
        """Returns version string."""
        if CONF.music_api.debug:
            LOG.debug("Requesting version info")
        return "v1-mock"


def API():
    """Wrapper for Music and Music Mock API"""

    # FIXME(jdandrea): Follow more formal practices for defining/using mocks
    if CONF.music_api.mock:
        return MockAPI()
    return MusicAPI()