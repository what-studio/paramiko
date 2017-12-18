from os.path import dirname, realpath, join
from time import sleep
import logging
import socket

import pytest

from paramiko import (
    AUTH_FAILED,
    AUTH_PARTIALLY_SUCCESSFUL,
    AUTH_SUCCESSFUL,
    OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED,
    OPEN_SUCCEEDED,
    DSSKey,
    InteractiveQuery,
    ServerInterface,
)
from paramiko.py3compat import builtins, u


def _support(filename):
    return join(dirname(realpath(__file__)), filename)


# TODO: consider using pytest.importorskip('gssapi') instead? We presumably
# still need CLI configurability for the Kerberos parameters, though, so can't
# JUST key off presence of GSSAPI optional dependency...
# TODO: anyway, s/True/os.environ.get('RUN_GSSAPI', False)/ or something.
needs_gssapi = pytest.mark.skipif(True, reason="No GSSAPI to test")


def needs_builtin(name):
    """
    Skip decorated test if builtin name does not exist.
    """
    reason = "Test requires a builtin '{}'".format(name)
    return pytest.mark.skipif(not hasattr(builtins, name), reason=reason)


slow = pytest.mark.slow


utf8_password = u('\u2022')


FINGERPRINTS = {
    'ssh-dss': b'\x44\x78\xf0\xb9\xa2\x3c\xc5\x18\x20\x09\xff\x75\x5b\xc1\xd2\x6c', # noqa
    'ssh-rsa': b'\x60\x73\x38\x44\xcb\x51\x86\x65\x7f\xde\xda\xa2\x2b\x5a\x57\xd5', # noqa
    'ecdsa-sha2-nistp256': b'\x25\x19\xeb\x55\xe6\xa1\x47\xff\x4f\x38\xd2\x75\x6f\xa5\xd5\x60', # noqa
    'ssh-ed25519': b'\xb3\xd5"\xaa\xf9u^\xe8\xcd\x0e\xea\x02\xb9)\xa2\x80',
}


class NullServer(ServerInterface):
    paranoid_did_password = False
    paranoid_did_public_key = False
    paranoid_key = DSSKey.from_private_key_file(_support('test_dss.key'))

    def __init__(self, *args, **kwargs):
        # Allow tests to enable/disable specific key types
        self.__allowed_keys = kwargs.pop('allowed_keys', [])
        # And allow them to set a (single...meh) expected public blob (cert)
        self.__expected_public_blob = kwargs.pop('public_blob', None)
        super(NullServer, self).__init__(*args, **kwargs)
        self.logger = logging.getLogger('test-server')

    def _log(self, message):
        self.logger.debug(message)

    def get_allowed_auths(self, username):
        if username == 'slowdive':
            return 'publickey,password'
        if username == 'paranoid':
            if (
                not self.paranoid_did_password and
                not self.paranoid_did_public_key
            ):
                return 'publickey,password'
            elif self.paranoid_did_password:
                return 'publickey'
            else:
                return 'password'
        if username == 'commie':
            return 'keyboard-interactive'
        if username == 'utf8':
            return 'password'
        if username == 'non-utf8':
            return 'password'
        return 'publickey'

    def check_auth_password(self, username, password):
        if (username == 'slowdive') and (password == 'pygmalion'):
            return AUTH_SUCCESSFUL
        if (username == 'paranoid') and (password == 'paranoid'):
            # 2-part auth (even openssh doesn't support this)
            self.paranoid_did_password = True
            if self.paranoid_did_public_key:
                return AUTH_SUCCESSFUL
            return AUTH_PARTIALLY_SUCCESSFUL
        if (username == 'utf8') and (password == utf8_password):
            return AUTH_SUCCESSFUL
        if (username == 'non-utf8') and (password == '\xff'):
            return AUTH_SUCCESSFUL
        if username == 'bad-server':
            raise Exception("Ack!")
        if (username == 'slowdive') and (password == 'unresponsive-server'):
            sleep(5)
            return AUTH_SUCCESSFUL
        return AUTH_FAILED

    def check_auth_publickey(self, username, key):
        # NOTE: this is for an existing multipart auth test.
        if (username == 'paranoid') and (key == self.paranoid_key):
            # 2-part auth
            self.paranoid_did_public_key = True
            if self.paranoid_did_password:
                return AUTH_SUCCESSFUL
            return AUTH_PARTIALLY_SUCCESSFUL
        # NOTE: these bits below are mostly used by client tests or
        # straightforward key tests.
        try:
            expected = FINGERPRINTS[key.get_name()]
        except KeyError:
            return AUTH_FAILED
        # Base check: allowed auth type & fingerprint matches
        happy = (
            key.get_name() in self.__allowed_keys and
            key.get_fingerprint() == expected
        )
        # Secondary check: if test wants assertions about cert data
        if (
            self.__expected_public_blob is not None and
            key.public_blob != self.__expected_public_blob
        ):
            happy = False
        return AUTH_SUCCESSFUL if happy else AUTH_FAILED

    def check_auth_interactive(self, username, submethods):
        if username == 'commie':
            self.username = username
            return InteractiveQuery(
                'password',
                'Please enter a password.',
                ('Password', False),
            )
        return AUTH_FAILED

    def check_auth_interactive_response(self, responses):
        if self.username == 'commie' and responses == ['cat']:
            return AUTH_SUCCESSFUL
        return AUTH_FAILED

    def check_auth_none(self, username):
        return AUTH_SUCCESSFUL if username == 'nobody' else AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        if kind == 'bogus':
            return OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
        return OPEN_SUCCEEDED

    def check_channel_exec_request(self, channel, command):
        if command != b'yes':
            return False
        return True

    def check_channel_env_request(self, channel, name, value):
        if name == 'INVALID_ENV':
            return False

        if not hasattr(channel, 'env'):
            setattr(channel, 'env', {})

        channel.env[name] = value
        return True

    def check_channel_shell_request(self, channel):
        return True

    def check_global_request(self, kind, msg):
        self._global_request = kind
        return False

    def check_channel_x11_request(
        self,
        channel,
        single_connection,
        auth_protocol,
        auth_cookie,
        screen_number,
    ):
        self._x11_single_connection = single_connection
        self._x11_auth_protocol = auth_protocol
        self._x11_auth_cookie = auth_cookie
        self._x11_screen_number = screen_number
        return True

    def check_port_forward_request(self, addr, port):
        self._listen = socket.socket()
        self._listen.bind(('127.0.0.1', 0))
        self._listen.listen(1)
        return self._listen.getsockname()[1]

    def cancel_port_forward_request(self, addr, port):
        self._listen.close()
        self._listen = None

    def check_channel_direct_tcpip_request(self, chanid, origin, destination):
        self._tcpip_dest = destination
        return OPEN_SUCCEEDED
