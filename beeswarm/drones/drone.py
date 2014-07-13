# Copyright (C) 2014 Johnny Vestergaard <jkv@unixcluster.dk>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
import logging
import requests
import socket
import json
from requests.exceptions import Timeout, ConnectionError
import gevent
from beeswarm.shared.asciify import asciify
from beeswarm.shared.message_enum import Messages
from beeswarm.shared.helpers import extract_keys, send_zmq_push
from beeswarm.drones.honeypot.honeypot import Honeypot
from beeswarm.drones.client.client import Client
import zmq.green as zmq
import zmq.auth
from zmq.utils.monitor import recv_monitor_message

logger = logging.getLogger(__name__)


class Drone(object):

    """ Aggregates a honeypot or client. """

    def __init__(self, work_dir, config, key='server.key', cert='server.crt', **kwargs):
        """

        :param work_dir: Working directory (usually the current working directory)
        :param config: Beeswarm configuration dictionary, None if no configuration was supplied.
        :param key: Key file used for SSL enabled capabilities
        :param cert: Cert file used for SSL enabled capabilities
        """

        # write ZMQ keys to files - as expected by pyzmq
        extract_keys(work_dir, config)
        self.work_dir = work_dir
        self.config = config
        self.key = key
        self.cert = cert
        self.id = self.config['general']['id']

        # Honeypot / Client
        self.drone = None
        self.drone_greenlet = None
        self.outgoing_msg_greenlet = None
        self.incoming_msg_greenlet = None

        # messages from server relayed to internal listeners
        ctx = zmq.Context()
        self.internal_server_relay = ctx.socket(zmq.PUSH)
        self.internal_server_relay.bind('ipc://serverCommands')

        if self.config['general']['fetch_ip']:
            try:
                url = 'http://api.externalip.net/ip'
                req = requests.get(url)
                self.ip = req.text
                logger.info('Fetched {0} as external ip for Honeypot.'.format(self.ip))
            except (Timeout, ConnectionError) as e:
                logger.warning('Could not fetch public ip: {0}'.format(e))
        else:
            self.ip = ''

    def start(self):
        """ Starts services. """

        cert_path = os.path.join(self.work_dir, 'certificates')
        public_keys_dir = os.path.join(cert_path, 'public_keys')
        private_keys_dir = os.path.join(cert_path, 'private_keys')

        client_secret_file = os.path.join(private_keys_dir, "client.key")
        client_public, client_secret = zmq.auth.load_certificate(client_secret_file)
        server_public_file = os.path.join(public_keys_dir, "server.key")
        server_public, _ = zmq.auth.load_certificate(server_public_file)

        self.outgoing_msg_greenlet = gevent.spawn(self.incoming_server_comms, server_public,
                                                  client_public,client_secret)
        self.incoming_msg_greenlet = gevent.spawn(self.outgoing_server_comms, server_public,
                                                  client_public, client_secret)

        self._start_drone()

        #drop_privileges()
        logger.info('Drone running using id: {0}'.format(self.id))
        gevent.joinall([self.incoming_msg_greenlet])

    def _start_drone(self):
        """
        Tears down the drone and restarts it.
        """

        mode = None
        if self.config['general']['mode'] == '':
            logger.info('Drone has not been configured, awaiting configuration from Beeswarm server.')
        elif self.config['general']['mode'] == 'honeypot':
            mode = Honeypot
        elif self.config['general']['mode'] == 'client':
            mode = Client

        if mode:
            self.drone = mode(self.work_dir, self.config)
            self.drone_greenlet = gevent.spawn(self.drone.start)

    def stop(self):
        """Stops services"""
        logging.debug('Stopping drone, hang on.')
        if self.drone is not None:
            self.drone.stop()
        # just some time for the drone to powerdown to be nice.
        gevent.sleep(2)
        if self.drone_greenlet is not None:
            self.drone_greenlet.kill(timeout=5)

    # command from server
    def incoming_server_comms(self, server_public, client_public, client_secret):
        context = zmq.Context()
        # data (commands) received from server
        receiving_socket = context.socket(zmq.SUB)

        # setup receiving tcp socket
        receiving_socket.curve_secretkey = client_secret
        receiving_socket.curve_publickey = client_public
        receiving_socket.curve_serverkey = server_public
        receiving_socket.setsockopt(zmq.RECONNECT_IVL, 2000)
        # messages to this specific drone
        receiving_socket.setsockopt(zmq.SUBSCRIBE, self.id)
        # broadcasts to all drones
        receiving_socket.setsockopt(zmq.SUBSCRIBE, Messages.IP)

        receiving_socket.connect(self.config['beeswarm_server']['zmq_command_url'])
        gevent.spawn(self.monitor_worker, receiving_socket.get_monitor_socket(), 'incomming socket ({0}).'
                     .format(self.config['beeswarm_server']['zmq_url']))
        logger.debug('Connected receiving socket to server on {0}'.format(self.config['beeswarm_server']['zmq_command_url']))

        poller = zmq.Poller()
        poller.register(receiving_socket, zmq.POLLIN)

        while True:
            # .recv() gives no context switch - why not? using poller with timeout instead
            socks = dict(poller.poll(1))
            gevent.sleep()

            if receiving_socket in socks and socks[receiving_socket] == zmq.POLLIN:
                message = receiving_socket.recv()
                # expected format for drone commands are:
                # DRONE_ID COMMAND OPTIONAL_DATA
                # DRONE_ID and COMMAND must not contain spaces
                drone_id, command, data = message.split(' ', 2)
                logger.debug('Received {0} command.'.format(command))
                assert(drone_id == self.id)
                # if we receive a configuration we restart the drone
                if command == Messages.CONFIG:
                    # additional payload
                    self.config = json.loads(data)
                    with open('beeswarmcfg.json', 'w') as local_config:
                        local_config.write(json.dumps(self.config, indent=4))
                    self.stop()
                    self._start_drone()
                else:
                    self.internal_server_relay.send('{0} {1}'.format(command, data))
        logger.warn('Command listener exiting.')

    def outgoing_server_comms(self, server_public, client_public, client_secret):
        context = zmq.Context()
        sending_socket = context.socket(zmq.PUSH)

        # setup sending tcp socket
        sending_socket.curve_secretkey = client_secret
        sending_socket.curve_publickey = client_public
        sending_socket.curve_serverkey = server_public
        sending_socket.setsockopt(zmq.RECONNECT_IVL, 2000)
        sending_socket.connect(self.config['beeswarm_server']['zmq_url'])
        gevent.spawn(self.monitor_worker, sending_socket.get_monitor_socket(), 'outgoing socket ({0}).'
                     .format(self.config['beeswarm_server']['zmq_url']))

        # retransmits everything received to beeswarm server using sending_socket
        internal_server_relay = context.socket(zmq.PULL)
        internal_server_relay.bind('ipc://serverRelay')

        poller = zmq.Poller()
        poller.register(internal_server_relay, zmq.POLLIN)

        while True:
            # .recv() gives no context switch - why not? using poller with timeout instead
            socks = dict(poller.poll(1))
            gevent.sleep()
            if internal_server_relay in socks and socks[internal_server_relay] == zmq.POLLIN:
                message = internal_server_relay.recv()
                logger.debug('Relaying {0} message to server.'.format(message.split(' ')[0]))
                sending_socket.send(message)

        logger.warn('Command sender exiting.')

    def monitor_worker(self, monitor_socket, log_name):
        monitor_socket.linger = 0
        poller = zmq.Poller()
        poller.register(monitor_socket, zmq.POLLIN)
        while True:
            socks = poller.poll(0)
            if len(socks) > 0:
                data = recv_monitor_message(monitor_socket)
                event = data['event']
                value = data['value']
                if event == zmq.EVENT_CONNECTED:
                    logger.info('Connected to {0}'.format(log_name))
                    send_zmq_push('ipc://serverRelay', '{0} {1}'.format(Messages.PING, self.id))
                    send_zmq_push('ipc://serverRelay', '{0} {1} {2}'.format(Messages.IP,
                                                                        self.id,
                                                                        socket.gethostbyname(socket.gethostname())))
                elif event == zmq.EVENT_DISCONNECTED:
                    logger.warning('Disconnected from {0}, will reconnect in {1} seconds.'.format(log_name, 5))
            gevent.sleep()



