#
# Copyright (c) 2015 ThoughtWorks, Inc.
#
# Pixelated is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Pixelated is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with Pixelated. If not, see <http://www.gnu.org/licenses/>.

import logging

from OpenSSL import SSL
from OpenSSL import crypto
from leap.common.events import (server as events_server,
                                register, catalog as events)
from twisted.cred import portal
from twisted.cred.checkers import AllowAnonymousAccess
from twisted.internet import defer
from twisted.internet import reactor
from twisted.internet import ssl

from pixelated.adapter.welcome_mail import add_welcome_mail
from pixelated.config import arguments
from pixelated.config import logger
from pixelated.config.leap import initialize_leap_single_user, init_monkeypatches, initialize_leap_provider
from pixelated.config import services
from pixelated.config.site import PixelatedSite
from pixelated.resources.auth import LeapPasswordChecker, PixelatedRealm, PixelatedAuthSessionWrapper, SessionChecker
from pixelated.resources.login_resource import LoginResource
from pixelated.resources.root_resource import RootResource

log = logging.getLogger(__name__)

from multiprocessing import Lock # BoundedSemaphore as Lock
from threading import current_thread
import openssl_thread_config


class ServicesFactory(object):

    def __init__(self, mode):
        self._services_by_user = {}
        self.mode = mode
        self._map_email = {}

    def map_email(self, username, user_id):
        self._map_email[username] = user_id

    def is_logged_in(self, user_id):
        return user_id in self._services_by_user

    def services(self, user_id):
        return self._services_by_user[user_id]

    def log_out_user(self, user_id, using_email=False):
        if using_email:
            user_id = self._map_email[user_id.split('@')[0]]

        if self.is_logged_in(user_id):
            _services = self._services_by_user[user_id]
            _services.close()
            del self._services_by_user[user_id]

    def add_session(self, user_id, services):
        self._services_by_user[user_id] = services

    @defer.inlineCallbacks
    def create_services_from(self, leap_session):
        _services = services.Services(leap_session)
        yield _services.setup()
        self._services_by_user[leap_session.user_auth.uuid] = _services


class SingleUserServicesFactory(object):
    def __init__(self, mode):
        self._services = None
        self.mode = mode

    def add_session(self, user_id, services):
        self._services = services

    def services(self, user_id):
        return self._services

    def log_out_user(self, user_id, using_email=False):
        reactor.stop()


class UserAgentMode(object):
    def __init__(self, is_single_user):
        self.is_single_user = is_single_user


@defer.inlineCallbacks
def start_user_agent_in_single_user_mode(root_resource, services_factory, leap_home, leap_session):
    log.info('Bootstrap done, loading services for user %s' % leap_session.user_auth.username)

    _services = services.Services(leap_session)
    yield _services.setup()

    if leap_session.fresh_account:
        yield add_welcome_mail(leap_session.mail_store)

    services_factory.add_session(leap_session.user_auth.uuid, _services)

    root_resource.initialize()

    # soledad needs lots of threads
    reactor.getThreadPool().adjustPoolsize(5, 15)
    log.info('Done, the user agent is ready to be used')


def _ssl_options(sslkey, sslcert):
    with open(sslkey) as keyfile:
        pkey = crypto.load_privatekey(crypto.FILETYPE_PEM, keyfile.read())
    with open(sslcert) as certfile:
        cert = crypto.load_certificate(crypto.FILETYPE_PEM, certfile.read())

    acceptable = ssl.AcceptableCiphers.fromOpenSSLCipherString(
        u'ECDHE-RSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-SHA384:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-SHA256:ECDHE-RSA-AES256-SHA:!RC4:HIGH:!MD5:!aNULL:!EDH')
    options = ssl.CertificateOptions(privateKey=pkey,
                                     certificate=cert,
                                     method=SSL.TLSv1_2_METHOD,
                                     acceptableCiphers=acceptable)
    return options


def _create_service_factory(args):
    if args.single_user:
        return SingleUserServicesFactory(UserAgentMode(is_single_user=True))
    else:
        return ServicesFactory(UserAgentMode(is_single_user=False))

CRYPTO_LOCK=1
CRYPTO_UNLOCK=2
CRYPTO_READ=4
CRYPTO_WRITE=8


def idfunc():
    id = current_thread().ident
    print 'sent thread id:  %d' % id
    print type(id)
    return int(id)

locks = [Lock() for i in range(1, 500)]


def lockfunc(mode, n, file, line):
    print 'mode: %d and n %d' % (mode, n)
    if mode & CRYPTO_LOCK == CRYPTO_LOCK:
        print "acquire lock %d" % n
        locks[n].acquire()
    elif mode & CRYPTO_UNLOCK == CRYPTO_UNLOCK:
        print "release lock %d" % n
        locks[n].release()
    else:
        print "unexpected call with mode %d and n %d" % (mode, n)


def initialize():
    openssl_thread_config.enable_mutexes(idfunc, lockfunc)

    log.info('Starting the Pixelated user agent')
    args = arguments.parse_user_agent_args()
    logger.init(debug=args.debug)
    services_factory = _create_service_factory(args)
    resource = RootResource(services_factory)

    start_async = _start_mode(args, resource, services_factory)
    add_top_level_system_callbacks(start_async, services_factory)
    log.info('Running the reactor')
    reactor.run()


def add_top_level_system_callbacks(deferred, services_factory):

    def _quit_on_error(failure):
        failure.printTraceback()
        reactor.stop()

    def _log_user_out(event, user_data):
        log.info('Invalid soledad token, logging out %s' % user_data)
        user_data = {'user_id': user_data['uuid']} if 'uuid' in user_data else {'user_id': user_data, 'using_email': True}
        services_factory.log_out_user(**user_data)

    def _log_user_out_on_token_expire(leap_session):
        register(events.SOLEDAD_INVALID_AUTH_TOKEN, _log_user_out)
        return leap_session

    deferred.addCallback(_log_user_out_on_token_expire)
    deferred.addErrback(_quit_on_error)


def _start_mode(args, resource, services_factory):
    if services_factory.mode.is_single_user:
        deferred = _start_in_single_user_mode(args, resource, services_factory)
    else:
        deferred = _start_in_multi_user_mode(args, resource, services_factory)
    return deferred


def _start_in_multi_user_mode(args, root_resource, services_factory):
    if args.provider is None:
        raise ValueError('provider name is required')

    init_monkeypatches()
    events_server.ensure_server()

    config, provider = initialize_leap_provider(args.provider, args.leap_provider_cert, args.leap_provider_cert_fingerprint, args.leap_home)
    protected_resource = set_up_protected_resources(root_resource, provider, services_factory, banner=args.banner)
    start_site(args, protected_resource)
    reactor.getThreadPool().adjustPoolsize(5, 15)
    return defer.succeed(None)


def set_up_protected_resources(root_resource, provider, services_factory, checker=None, banner=None):
    if not checker:
        checker = LeapPasswordChecker(provider)
    session_checker = SessionChecker(services_factory)
    anonymous_resource = LoginResource(services_factory, disclaimer_banner=banner)

    realm = PixelatedRealm(root_resource, anonymous_resource)
    _portal = portal.Portal(realm, [checker, session_checker, AllowAnonymousAccess()])

    protected_resource = PixelatedAuthSessionWrapper(_portal, root_resource, anonymous_resource, [])
    anonymous_resource.set_portal(_portal)
    root_resource.initialize(_portal, disclaimer_banner=banner)
    return protected_resource


def _start_in_single_user_mode(args, resource, services_factory):
    start_site(args, resource)
    deferred = initialize_leap_single_user(args.leap_provider_cert,
                                           args.leap_provider_cert_fingerprint,
                                           args.credentials_file,
                                           args.leap_home)
    deferred.addCallback(
        lambda leap_session: start_user_agent_in_single_user_mode(
            resource,
            services_factory,
            args.leap_home,
            leap_session))
    return deferred


def start_site(config, resource):
    log.info('Starting the API on port %s' % config.port)
    if config.sslkey and config.sslcert:
        reactor.listenSSL(config.port, PixelatedSite(resource), _ssl_options(config.sslkey, config.sslcert),
                          interface=config.host)
    else:
        reactor.listenTCP(config.port, PixelatedSite(resource), interface=config.host)
