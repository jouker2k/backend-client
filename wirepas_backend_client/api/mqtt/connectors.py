"""
    Connection
    ==========

    .. Copyright:
        Copyright 2019 Wirepas Ltd under Apache License, Version 2.0.
        See file LICENSE for full license details.
"""


import logging
import os
import ssl
import time
import uuid

import paho
import paho.mqtt.client as mqtt

from ...tools import ExitSignal


class MQTT(object):
    """
    Generic MQTT handler for backend client sessions
    """

    def __init__(
        self,
        username: str,
        password: str,
        hostname: str,
        port: int,
        ca_certs: str,
        cert_required=None,
        tls_version=None,
        certfile=None,
        keyfile=None,
        cert_reqs=None,
        ciphers=None,
        userdata: object = None,
        transport: str = "tcp",
        clean_session: bool = True,
        reconnect_min_delay: int = 10,
        reconnect_max_delay: int = 120,
        allow_untrusted: bool = False,
        force_unsecure: bool = False,
        exit_signal: object = None,
        heartbeat: int = 100,
        keep_alive: int = 120,
        message_subscribe_handlers: dict = None,
        message_publish_handlers: dict = None,
        mqtt_protocol=None,
        logger: logging.Logger = None,
    ):

        super(MQTT, self).__init__()

        self.logger = logger or logging.getLogger(__name__)

        self.running = False
        self.heartbeat = heartbeat
        self.exit_signal = ExitSignal(exit_signal)
        self.id = "wm-gw-cli:{0}".format(uuid.uuid1(clock_seq=0).urn)

        self.username = username
        self.password = password

        if cert_required is None:
            self.cert_reqs = ssl.CERT_REQUIRED

        if tls_version is None:
            self.tls_version = ssl.PROTOCOL_TLSv1_2

        if mqtt_protocol is None:
            self.mqtt_protocol = mqtt.MQTTv311

        self.ca_certs = None
        if ca_certs:
            if os.path.exists(ca_certs):
                self.ca_certs = ca_certs
            else:
                self.logger.error(
                    "Certificate path ({}) does not exist -> attempting host load".format(
                        ca_certs
                    )
                )

        self.certfile = certfile
        self.keyfile = keyfile
        self.ciphers = ciphers

        self.hostname = hostname
        self.port = port

        self.clean_session = clean_session
        self.userdata = userdata
        self.transport = transport

        self.client = mqtt.Client(
            client_id=self.id,
            clean_session=self.clean_session,
            userdata=self.userdata,
            protocol=self.mqtt_protocol,
            transport=self.transport,
        )
        self.client.username_pw_set(self.username, self.password)
        self.client.reconnect_delay_set(
            min_delay=reconnect_min_delay, max_delay=reconnect_max_delay
        )
        self.client.enable_logger(self.logger)

        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.on_publish = self.on_publish
        self.client.on_disconnect = self.on_disconnect
        self.client.on_unsubscribe = self.on_unsubscribe

        self.keep_alive = keep_alive
        self.allow_untrusted = allow_untrusted
        self.force_unsecure = force_unsecure

        self.message_publish_handlers = dict()
        if message_publish_handlers is not None:
            self.message_publish_handlers = message_publish_handlers

        self.message_subscribe_handlers = dict()
        if message_subscribe_handlers is not None:
            self.message_subscribe_handlers = message_subscribe_handlers

        self.subscription = set()

    def serve(self: "MQTT"):
        """
        Connects and serves for ever.

        The loop periodically checks if the client is alive by looking at the
        exit_signal event.
        """

        self.running = True
        try:
            self.connect()
        except Exception as err:
            self.logger.exception("Could not connect due to: {}".format(err))
            self.exit_signal.set()
            raise

        self.subscribe_messages(self.message_subscribe_handlers)
        self.client.loop_start()

        while not self.exit_signal.is_set():
            self.logger.debug("mqtt loop running")
            if len(self.message_publish_handlers) == 0:
                time.sleep(self.heartbeat)
            else:
                for topic, cb in self.message_publish_handlers.items():
                    cb(topic=topic, mqtt_publish=self.send)

        if not self.exit_signal.is_set():
            self.exit_signal.set()

        self.close()
        self.client.loop_stop()

        return self.running

    def connect(self: "MQTT"):
        """ Establishes a connection and service loop. """

        self.logger.info(
            "connecting to %s:%s@%s:%s - %s",
            self.username,
            self.password,
            self.hostname,
            self.port,
            self.ca_certs,
        )

        if self.force_unsecure is False:
            if self.allow_untrusted:
                self.client.tls_insecure_set(self.allow_untrusted)
            else:
                self.client.tls_set(
                    ca_certs=self.ca_certs,
                    certfile=self.certfile,
                    keyfile=self.keyfile,
                    cert_reqs=self.cert_reqs,
                    tls_version=self.tls_version,
                    ciphers=self.ciphers,
                )

        self.client.connect(
            self.hostname, port=self.port, keepalive=self.keep_alive
        )

    def close(self: "MQTT") -> None:
        """ Handles disconnect from the pubsub. """
        if self.running:
            self.running = False
            self.on_close()
            self.client.disconnect()

    def subscribe_messages(self, handlers: dict) -> None:
        """
        Register a set of callbacks with topic handlers

        Handlers is a dictionary with contains as key the topic filter
        and as value the callable who should handle such messages.
        """
        if len(handlers) > 0:
            for topic_filter, cb in handlers.items():
                self.client.message_callback_add(topic_filter, cb)
                self.subscription.add(topic_filter)
                self.logger.info("%s -> %s", topic_filter, cb)

            self.message_subscribe_handlers = handlers

    def on_close(self: "MQTT") -> None:
        """ Override for handling before closing events, like last will"""
        pass

    def on_connect(
        self: "MQTT",
        client: "paho.mqtt.client",
        userdata: object,
        flags: list,
        rc: int,
    ) -> None:
        """
        Callback that is called when connection to MQTT has succeeded.

        Here, we're subscribing to the incoming topics.

        Args:
           client (object): The MQTT client instance for this callback;
           userdata (object): The private user data;
           flags (list): A list of flags;
           rc (int): The connection result.

        """

        # Check the connection result.
        if rc == mqtt.CONNACK_ACCEPTED:
            self.logger.info(
                "connected to MQTT {0} {1}".format(
                    flags, mqtt.connack_string(rc)
                )
            )

            for topic in self.subscription:
                rc, mid = client.subscribe(topic)

                if rc == mqtt.MQTT_ERR_SUCCESS:
                    self.logger.info(
                        "subscribed to topic: %s (%s, %s)", topic, mid, rc
                    )

                elif rc == mqtt.MQTT_ERR_SUCCESS:
                    self.logger.error(
                        "failed topic subscription with " "%s: %s (%s, %s)",
                        topic,
                        mid,
                        rc,
                        mqtt.error_string(rc),
                    )
                    self.client.disconnect()

        else:
            self.logger.error(
                "connection error: %s %s", mqtt.error_string(rc), flags
            )
            self.client.disconnect()

    def on_disconnect(
        self: "MQTT", client: paho.mqtt.client, userdata: object, rc: int
    ):
        """
        Handles a disconnect request.

        If the disconnect reason is unknown the method lets the reconnection
        loop establish the connection to the server once again.

        If the disconnect is due to a call to disconnect, then the

        """
        self.logger.error(
            "disconnect: server is down %s (%s)", mqtt.error_string(rc), rc
        )

        if rc == mqtt.MQTT_ERR_SUCCESS and self.running:
            self.running = False
            if not self.exit_signal.is_set():
                self.exit_signal.set()

            if self.subscription is not None:
                for topic in self.subscription:
                    self.client.unsubscribe(topic)

    def on_subscribe(
        self: "MQTT",
        client: paho.mqtt.client,
        userdata: object,
        mid: int,
        granted_qos: int,
    ):
        """
        Callback generated when the broker acknowledges a subscription event
        """
        self.logger.debug(
            "subscribed with mid: %s / qos: %s", mid, granted_qos
        )

    def on_unsubscribe(
        self: "MQTT", client: paho.mqtt.client, userdata: object, mid: int
    ):
        """
        Callback generated when the broker acknowledges an unsubscribe event
        """
        self.logger.debug("unsubscribed with mid:%s", mid)

    def on_publish(
        self: "MQTT", client: paho.mqtt.client, userdata: object, mid: int
    ):
        """
        Callback generated when the broker acknowledges a pubished message
        """
        self.logger.debug("sent message %s", mid)

    def on_log(
        self: "MQTT",
        client: paho.mqtt.client,
        userdata: object,
        level: int,
        buf: str,
    ):
        """
        Internal mqtt logging where buf is the message being sent
        """
        self.logger.debug("mqtt-log: %s", buf)

    def on_message(
        self: "MQTT", client: paho.mqtt.client, userdata: object, message: str
    ):
        """
        Generic topic to handle message requests

        Args:
            client (object): MQTT client object;
            userdata (object): the private user data;
            message (object): Incoming message.
        """

        self.logger.debug(
            "%s:%s:%s", message.topic, message.payload, message.qos
        )

    def _print(
        self: "MQTT", client: paho.mqtt.client, userdata: object, message: str
    ):
        self.logger.debug(
            "Message print > %s:%s:%s",
            message.topic,
            message.payload,
            message.qos,
        )

    def send(
        self,
        message: str,
        topic: str,
        qos: int = 1,
        retain: bool = False,
        wait_for_publish: bool = False,
    ):
        pubinfo = self.client.publish(
            "{0}".format(topic), message, qos=qos, retain=retain
        )

        if pubinfo.rc != mqtt.MQTT_ERR_SUCCESS:
            self.logger.error(
                "publish: %s (%s)", mqtt.error_string(pubinfo.rc), pubinfo.rc
            )
            self.exit_signal.set()

        elif wait_for_publish:
            try:
                self.logger.info("Waiting for publish.")
                pubinfo.wait_for_publish()  # proper way, but it can hang
            except ValueError:
                self.logger.error("Could not validate publish.")

    def __str__(self):
        return str("{}{}{}", self.username, self.hostname, self.port)