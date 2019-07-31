"""
    Settings
    ==========

    .. Copyright:
        Copyright 2019 Wirepas Ltd under Apache License, Version 2.0.
        See file LICENSE for full license details.
"""

import json

from ...tools import Settings


class MQTTSettings(Settings):
    """MQTTSettings"""

    def __init__(self, settings: Settings) -> "MQTTSettings":

        self.mqtt_username = None
        self.mqtt_password = None
        self.mqtt_hostname = None
        self.mqtt_port = None
        self.mqtt_persist_session = None
        self.mqtt_subscribe_network_id = None
        self.mqtt_subscribe_sink_id = None
        self.mqtt_subscribe_gateway_id = None
        self.mqtt_subscribe_source_endpoint = None
        self.mqtt_subscribe_destination_endpoint = None
        self.mqtt_ca_certs = None
        self.mqtt_ciphers = None
        self.mqtt_topic = "#"

        super(MQTTSettings, self).__init__(settings)

        self.username = self.mqtt_username
        self.password = self.mqtt_password
        self.hostname = self.mqtt_hostname
        self.port = self.mqtt_port
        self.clean_session = not self.mqtt_persist_session
        self.network_id = self.mqtt_subscribe_network_id
        self.sink_id = self.mqtt_subscribe_sink_id
        self.gateway_id = self.mqtt_subscribe_gateway_id
        self.source_endpoint = self.mqtt_subscribe_source_endpoint
        self.destination_endpoint = self.mqtt_subscribe_destination_endpoint
        self.ca_certs = self.mqtt_ca_certs

        self.allow_untrusted = self.mqtt_allow_untrusted
        self.force_unsecure = self.mqtt_force_unsecure

        self.userdata = None
        self.transport = "tcp"
        self.reconnect_min_delay = 10
        self.reconnect_max_delay = 120

        self.heartbeat = 2
        self.keep_alive = 60
        self.ciphers = self.mqtt_ciphers
        self.topic = self.mqtt_topic

    def sanity(self) -> bool:
        """ Checks if connection parameters are valid """

        is_valid = (
            self.username is not None
            and self.password is not None
            and self.hostname is not None
            and self.port is not None
        )

        return is_valid

    def set_defaults(self) -> None:
        """ Sets common settings for the MQTT client connection """

    def to_dict(self):
        return self.__dict__

    def __str__(self):
        return json.dumps(self.__dict__)
