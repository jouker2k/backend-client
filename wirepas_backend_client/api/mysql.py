"""
    MySQL API
    ============

    Contains class to handle MySQL interaction

    .. Copyright:
        Copyright 2018 Wirepas Ltd. All Rights Reserved.
        See file LICENSE.txt for full license details.
"""
import os
from loky import get_reusable_executor

try:
    import MySQLdb
except ImportError:
    pass

import binascii
import datetime
import logging
import multiprocessing
import queue
import time

from .stream import StreamObserver
from ..messages import GenericMessage
from ..messages import AdvertiserMessage
from ..messages import BootDiagnosticsMessage
from ..messages import NeighborDiagnosticsMessage
from ..messages import NodeDiagnosticsMessage
from ..messages import TestNWMessage
from ..messages import TrafficDiagnosticsMessage
from ..tools import Settings


class MySQLSettings(Settings):
    """MySQL Settings"""

    def __init__(self, settings: Settings) -> "MySQLSettings":

        super(MySQLSettings, self).__init__(settings)

        self.username = self.db_username
        self.password = self.db_password
        self.hostname = self.db_hostname
        self.database = self.db_database
        self.port = self.db_port


class MySQLObserver(StreamObserver):
    """ MySQLObserver monitors the internal queues and dumps events to the database """

    def __init__(
        self,
        mysql_settings: Settings,
        start_signal: multiprocessing.Event,
        exit_signal: multiprocessing.Event,
        tx_queue: multiprocessing.Queue,
        rx_queue: multiprocessing.Queue,
        logger=None,
    ) -> "MySQLObserver":
        super(MySQLObserver, self).__init__(
            start_signal=start_signal,
            exit_signal=exit_signal,
            tx_queue=tx_queue,
            rx_queue=rx_queue,
        )

        self.logger = logger or logging.getLogger(__name__)

        self.mysql = MySQL(
            username=mysql_settings.username,
            password=mysql_settings.password,
            hostname=mysql_settings.hostname,
            port=mysql_settings.port,
            database=mysql_settings.database,
            logger=self.logger,
        )
        self.settings = mysql_settings
        self.timeout = 10

    def on_data_received(self):
        """ Monitor inbound queue for messages to be stored in MySQL """

        while not self.exit_signal.is_set():

            try:
                message = self.rx_queue.get(timeout=self.timeout, block=True)
            except queue.Empty:
                message = None
                continue

            self._map_message(self.mysql, message)

    @staticmethod
    def _map_message(mysql, message):
        """ Inserts the message according to its type """
        if isinstance(message, AdvertiserMessage):
            mysql.put_advertiser([message])
        elif isinstance(message, BootDiagnosticsMessage):
            mysql.put_boot_diagnostics(message)
        elif isinstance(message, NeighborDiagnosticsMessage):
            mysql.put_neighbor_diagnostics(message)
        elif isinstance(message, NodeDiagnosticsMessage):
            mysql.put_node_diagnostics(message)
        elif isinstance(message, TestNWMessage):
            mysql.put_testnw_measurements(message)
        elif isinstance(message, TrafficDiagnosticsMessage):
            mysql.put_traffic_diagnostics(message)
        elif isinstance(message, GenericMessage):
            mysql.put_to_received_packets(message)

    def pool_on_data_received(self, n_workers=4):
        """ Monitor inbound queue for messages to be stored in MySQL """

        def work(storage_q, exit_signal, settings, logger):

            mysql = MySQL(
                username=settings.username,
                password=settings.password,
                hostname=settings.hostname,
                port=settings.port,
                database=settings.database,
                logger=logger,
            )

            mysql.connect(table_creation=False)
            pid = os.getpid()

            logger.warning("starting MySQL worker {}".format(pid))
            while not exit_signal.is_set():
                try:
                    message = storage_q.get(block=True, timeout=2)
                except queue.Empty:
                    continue

                MySQLObserver._map_message(mysql, message)

            logger.warning("exiting MySQL worker {}".format(pid))
            return pid

        executor = get_reusable_executor(max_workers=n_workers, timeout=10)

        for n in range(0, n_workers):
            executor.submit(
                work,
                self.rx_queue,
                self.exit_signal,
                self.settings,
                self.logger,
            )
        self._wait_for_exit()

    def run(self, **kwargs):
        """ Runs until asked to exit """
        try:
            self.parallel = kwargs["parallel"]
        except KeyError:
            self.parallel = False
            pass

        try:
            self.mysql.connect()
        except Exception as err:
            self.logger.error("error connecting to database {}".format(err))
            pass

        if self.parallel:
            self.logger.info("Starting // mysql work")
            self.pool_on_data_received()
        else:
            self.logger.info("Starting single threaded mysql work")
            self.on_data_received()

        self.mysql.close()

    def _wait_for_exit(self):
        """ waits until the exit signal is set """
        print("waiting for exit")
        while not self.exit_signal.is_set():
            time.sleep(self.timeout * 10)
            self.logger.debug("MySQL is running")


class MySQL(object):
    """
    MySQL connection handler
    """

    def __init__(
        self,
        username: str,
        password: str,
        hostname: str,
        database: str,
        port: int,
        logger: logging.Logger = None,
    ):

        super(MySQL, self).__init__()

        self.logger = logger or logging.getLogger(__name__)

        self.hostname = hostname
        self.username = username
        self.password = password
        self.database_name = database
        self.database = None
        self.port = port
        self.cursor = None

    def connect(self, table_creation=True) -> None:
        """ Establishes a connection and service loop. """
        self.logger.info(
            "MySQL connection to {user}:{password}"
            "@{host}:{port}".format(
                user=self.username,
                password=self.password,
                host=self.hostname,
                port=self.port,
            )
        )
        self.database = MySQLdb.connect(
            host=self.hostname,
            user=self.username,
            passwd=self.password,
            database=self.database_name,
            port=self.port,
        )

        self.cursor = self.database.cursor()
        if table_creation:
            self.create_tables()

    def close(self: "MySQL") -> None:
        """ Handles disconnect from database object """
        self.cursor.close()
        self.database.close()

    def write(self, message):
        pass

    def create_tables(self):
        """
        Create tables if they do not exist
        """

        query = (
            "CREATE TABLE IF NOT EXISTS known_nodes ("
            "  network_address BIGINT UNSIGNED NOT NULL,"
            "  node_address INT UNSIGNED NOT NULL,"
            "  last_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,"
            "  voltage DOUBLE NULL,"
            "  node_role SMALLINT UNSIGNED NULL,"
            "  firmware_version INT UNSIGNED NULL,"
            "  scratchpad_seq INT UNSIGNED NULL,"
            "  hw_magic INT UNSIGNED NULL,"
            "  stack_profile INT UNSIGNED NULL,"
            "  boot_count INT UNSIGNED NULL,"
            "  file_line_num INT UNSIGNED NULL,"
            "  file_name_hash INT UNSIGNED NULL,"
            "  UNIQUE INDEX node (network_address, node_address)"
            ") ENGINE = MYISAM;"
        )
        self.cursor.execute(query)

        query = (
            "CREATE TABLE IF NOT EXISTS received_packets ("
            "  id BIGINT NOT NULL AUTO_INCREMENT UNIQUE,"
            "  logged_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,"
            "  launch_time TIMESTAMP NULL,"
            "  path_delay_ms BIGINT UNSIGNED NOT NULL,"
            "  network_address BIGINT UNSIGNED NOT NULL,"
            "  sink_address INT UNSIGNED NOT NULL,"
            "  source_address INT UNSIGNED NOT NULL,"
            "  dest_address INT UNSIGNED NOT NULL,"
            "  source_endpoint SMALLINT UNSIGNED NOT NULL,"
            "  dest_endpoint SMALLINT UNSIGNED NOT NULL,"
            "  qos SMALLINT UNSIGNED NOT NULL,"
            "  num_bytes SMALLINT UNSIGNED NOT NULL,"
            "  hop_count SMALLINT UNSIGNED DEFAULT NULL,"
            "  PRIMARY KEY (id),"
            "  INDEX (logged_time),"
            "  INDEX (launch_time),"
            "  INDEX (source_address),"
            "  INDEX packets_from_node (network_address, source_address)"
            ") ENGINE = MYISAM;"
        )
        self.cursor.execute(query)

        # See if we need to expand the old received_packets table with
        # the hop_count column.
        query = "SHOW COLUMNS FROM received_packets;"
        self.cursor.execute(query)
        self.database.commit()
        values = self.cursor.fetchall()
        column_names = map(lambda x: x[0], values)
        if "hop_count" not in column_names:
            # hop_count was not in the table so add it.
            query = (
                "ALTER TABLE received_packets\n"
                "ADD COLUMN hop_count SMALLINT UNSIGNED DEFAULT NULL;"
            )
            self.cursor.execute(query)
            self.database.commit()

        query = (
            "CREATE TABLE IF NOT EXISTS diagnostic_traffic ("
            "  received_packet BIGINT NOT NULL,"
            "  access_cycles INT UNSIGNED NOT NULL,"
            "  cluster_channel SMALLINT UNSIGNED NOT NULL,"
            "  channel_reliability SMALLINT UNSIGNED NOT NULL,"
            "  rx_count INT UNSIGNED NOT NULL,"
            "  tx_count INT UNSIGNED NOT NULL,"
            "  aloha_rxs SMALLINT UNSIGNED NOT NULL,"
            "  resv_rx_ok SMALLINT UNSIGNED NOT NULL,"
            "  data_rxs SMALLINT UNSIGNED NOT NULL,"
            "  dup_rxs SMALLINT UNSIGNED NOT NULL,"
            "  cca_ratio SMALLINT UNSIGNED NOT NULL,"
            "  bcast_ratio SMALLINT UNSIGNED NOT NULL,"
            "  tx_unicast_fail SMALLINT UNSIGNED NOT NULL,"
            "  resv_usage_max SMALLINT UNSIGNED NOT NULL,"
            "  resv_usage_avg SMALLINT UNSIGNED NOT NULL,"
            "  aloha_usage_max SMALLINT UNSIGNED NOT NULL,"
            "  FOREIGN KEY (received_packet) REFERENCES received_packets(id)"
            ") ENGINE = MYISAM;"
        )
        self.cursor.execute(query)

        query = (
            "CREATE TABLE IF NOT EXISTS diagnostic_neighbor ("
            "  received_packet BIGINT NOT NULL,"
            "  node_address INT UNSIGNED NOT NULL,"
            "  cluster_channel SMALLINT UNSIGNED NOT NULL,"
            "  radio_power SMALLINT UNSIGNED NOT NULL,"
            "  device_info SMALLINT UNSIGNED NOT NULL,"
            "  norm_rssi SMALLINT UNSIGNED NOT NULL,"
            "  FOREIGN KEY (received_packet) REFERENCES received_packets(id)"
            ") ENGINE = MYISAM;"
        )
        self.cursor.execute(query)

        query = (
            "CREATE TABLE IF NOT EXISTS diagnostic_node ("
            "  received_packet BIGINT NOT NULL,"
            "  access_cycle_ms INT UNSIGNED NOT NULL,"
            "  node_role SMALLINT UNSIGNED NOT NULL,"
            "  voltage DOUBLE NOT NULL,"
            "  buf_usage_max SMALLINT UNSIGNED NOT NULL,"
            "  buf_usage_avg SMALLINT UNSIGNED NOT NULL,"
            "  mem_alloc_fails SMALLINT UNSIGNED NOT NULL,"
            "  tc0_delay SMALLINT UNSIGNED NOT NULL,"
            "  tc1_delay SMALLINT UNSIGNED NOT NULL,"
            "  network_scans SMALLINT UNSIGNED NOT NULL,"
            "  downlink_delay_avg_0 INT UNSIGNED NOT NULL,"
            "  downlink_delay_min_0 INT UNSIGNED NOT NULL,"
            "  downlink_delay_max_0 INT UNSIGNED NOT NULL,"
            "  downlink_delay_samples_0 INT UNSIGNED NOT NULL,"
            "  downlink_delay_avg_1 INT UNSIGNED NOT NULL,"
            "  downlink_delay_min_1 INT UNSIGNED NOT NULL,"
            "  downlink_delay_max_1 INT UNSIGNED NOT NULL,"
            "  downlink_delay_samples_1 INT UNSIGNED NOT NULL,"
            "  dropped_packets_0 SMALLINT UNSIGNED NOT NULL,"
            "  dropped_packets_1 SMALLINT UNSIGNED NOT NULL,"
            "  route_address INT UNSIGNED NOT NULL,"
            "  next_hop_address_0 INT UNSIGNED NOT NULL,"
            "  cost_0 SMALLINT UNSIGNED NOT NULL,"
            "  quality_0 SMALLINT UNSIGNED NOT NULL,"
            "  next_hop_address_1 INT UNSIGNED NOT NULL,"
            "  cost_1 SMALLINT UNSIGNED NOT NULL,"
            "  quality_1 SMALLINT UNSIGNED NOT NULL,"
            "  FOREIGN KEY (received_packet) REFERENCES received_packets(id)"
            ") ENGINE = MYISAM;"
        )
        self.cursor.execute(query)

        query = (
            "CREATE TABLE IF NOT EXISTS diagnostic_event ("
            "  received_packet BIGINT NOT NULL,"
            "  position SMALLINT NOT NULL,"
            "  event SMALLINT NOT NULL,"
            "  FOREIGN KEY (received_packet) REFERENCES received_packets(id),"
            "  UNIQUE INDEX event_id (received_packet, position)"
            ") ENGINE = MYISAM;"
        )
        self.cursor.execute(query)

        query = (
            "CREATE TABLE IF NOT EXISTS diagnostic_boot ("
            "  received_packet BIGINT NOT NULL,"
            "  boot_count INT UNSIGNED NOT NULL,"
            "  node_role SMALLINT UNSIGNED NOT NULL,"
            "  firmware_version INT UNSIGNED NOT NULL,"
            "  scratchpad_seq INT UNSIGNED NOT NULL,"
            "  hw_magic INT UNSIGNED NOT NULL,"
            "  stack_profile INT UNSIGNED NOT NULL,"
            "  otap_enabled BOOL NOT NULL,"
            "  file_line_num INT UNSIGNED NOT NULL,"
            "  file_name_hash INT UNSIGNED NOT NULL,"
            "  stack_trace_0 INT UNSIGNED NOT NULL,"
            "  stack_trace_1 INT UNSIGNED NOT NULL,"
            "  stack_trace_2 INT UNSIGNED NOT NULL,"
            "  current_seq INT UNSIGNED DEFAULT NULL,"
            "  FOREIGN KEY (received_packet) REFERENCES received_packets(id)"
            ") ENGINE = MYISAM;"
        )
        self.cursor.execute(query)

        # See if we need to expand the old diagnostic_boot table with
        # the current_seq.
        query = "SHOW COLUMNS FROM diagnostic_boot;"
        self.cursor.execute(query)

        values = self.cursor.fetchall()
        column_names = map(lambda x: x[0], values)
        if "current_seq" not in column_names:
            # current_seq was not in the table so add it.
            query = (
                "ALTER TABLE diagnostic_boot\n"
                "ADD COLUMN current_seq INT UNSIGNED DEFAULT NULL;"
            )
            self.cursor.execute(query)

        query = (
            "CREATE TABLE IF NOT EXISTS sink_command ( "
            "id BIGINT NOT NULL AUTO_INCREMENT UNIQUE, "
            "address INT UNSIGNED NOT NULL, "
            "command varchar(255), "
            "param LONGBLOB, "
            "launch_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "ready_time TIMESTAMP NULL, "
            "result INT UNSIGNED, "
            "PRIMARY KEY (id), "
            "INDEX(address) "
            ") ENGINE = MYISAM;"
        )
        self.cursor.execute(query)

        # Create table for received remote statuses
        createtable = (
            "CREATE TABLE IF NOT EXISTS remote_status ( "
            "id BIGINT NOT NULL AUTO_INCREMENT UNIQUE, "
            "address INT UNSIGNED NOT NULL, "
            "sink_address INT UNSIGNED NOT NULL, "
            "reception_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "crc INT UNSIGNED, "
            "otap_seq INT UNSIGNED NOT NULL, "
            "scratchpad_type INT UNSIGNED, "
            "scratchpad_status INT UNSIGNED, "
            "processed_length INT UNSIGNED, "
            "processed_crc INT UNSIGNED, "
            "processed_seq INT UNSIGNED, "
            "fw_mem_area_id INT UNSIGNED, "
            "fw_major_version INT UNSIGNED, "
            "fw_minor_version INT UNSIGNED, "
            "fw_maintenance_version INT UNSIGNED, "
            "fw_development_version INT UNSIGNED, "
            "seconds_until_update INT UNSIGNED, "
            "legacy_status INT UNSIGNED, "
            "PRIMARY KEY (id), "
            "INDEX(address), "
            "INDEX(sink_address) "
            ") ENGINE = MYISAM;"
        )
        self.cursor.execute(createtable)

        # Create advertiser
        createtable = (
            "CREATE TABLE IF NOT EXISTS advertiser (   "
            "id BIGINT NOT NULL AUTO_INCREMENT UNIQUE,   "
            "logged_time DATETIME(6) NULL,   "
            "measured_time DATETIME(6) NULL,   "
            "source_address DOUBLE NULL, "
            "destination_address INT NULL, "
            "source_endpoint INT NULL, "
            "destination_endpoint INT NULL, "
            "travel_time_ms BIGINT UNSIGNED NOT NULL,   "
            "qos INT UNSIGNED NOT NULL,   "
            "message_type BIGINT UNSIGNED NOT NULL,   "
            "reserved BIGINT UNSIGNED NOT NULL,   "
            "node_address INT UNSIGNED NOT NULL,   "
            "value BIGINT SIGNED NOT NULL,   "
            "payload BLOB, "
            "PRIMARY KEY (id),   "
            "INDEX (logged_time),  "
            "INDEX (measured_time),   "
            "INDEX (message_type), "
            "INDEX (node_address) "
            ") ENGINE=InnoDB; "
        )

        self.cursor.execute(createtable)

        # Create event codes
        createtable = (
            "CREATE TABLE IF NOT EXISTS diagnostic_event_codes ( "
            "code SMALLINT UNSIGNED UNIQUE NOT NULL, "
            "name TEXT, "
            "description TEXT, "
            "PRIMARY KEY (code) "
            ") ENGINE = MYISAM;"
        )
        self.cursor.execute(createtable)

        # Populate event codes
        createtable = (
            "REPLACE INTO diagnostic_event_codes "
            "(code, name, description) VALUES "
            '(0x08, "role_change_to_subnode", '
            '"Role change: change to subnode"),'
            '(0x09, "role_change_to_headnode", '
            '"Role change: change to headnode"),'
            '(0x10, "route_change_unknown", '
            '"Route change: unknown reason"),'
            '(0x18, "scan_ftdma_adjust",'
            '"Scan: changing channel or no cluster channel selected"),'
            '(0x19, "scan_f_confl_near_nbor",'
            '"Scan: FTDMA conflict with cluster"),'
            '(0x1a, "scan_f_confl_far_nbor",'
            '"Scan: FTDMA conflict with neighbor\'s neighbor"),'
            '(0x1b, "scan_t_confl_nbor",'
            '"Scan: timing conflict with cluster"),'
            '(0x1c, "scan_t_confl_between_nbors",'
            '"Scan: timing conflict between two or more clusters"),'
            '(0x1d, "scan_need_nbors",'
            '"Scan: need more clusters"),'
            '(0x1e, "scan_periodic",'
            '"Scan: periodic scan"),'
            '(0x1f, "scan_role_change",'
            '"Scan: role change"),'
            '(0x20, "boot_por",'
            '"Boot: power-on reset"),'
            '(0x21, "boot_intentional",'
            '"Boot: reboot requested"),'
            '(0x22, "boot_assert",'
            '"Boot: software assert"),'
            '(0x23, "boot_fault",'
            '"Boot: fault handler"),'
            '(0x24, "boot_wdt",'
            '"Boot: watchdog timer"),'
            '(0x25, "boot_unknown",'
            '"Boot: unknown reason"),'
            '(0x28, "sync_lost_synced",'
            '"Sync lost: lost sync to synced cluster"),'
            '(0x29, "sync_lost_joined",'
            '"Sync lost: lost sync to next hop cluster"),'
            '(0x30, "tdma_adjust_minor_boundary",'
            '"TDMA adjust: minor boundary adjust"),'
            '(0x31, "tdma_adjust_major_boundary",'
            '"TDMA adjust: not in slot boundary"),'
            '(0x32, "tdma_adjust_next_hop",'
            '"TDMA adjust: FTDMA conflict with next hop"),'
            '(0x33, "tdma_adjust_cluster",'
            '"TDMA adjust: FTDMA conflict with neighboring cluster"),'
            '(0x34, "tdma_adjust_neighbor",'
            '"TDMA adjust: FTDMA conflict with neighbor"),'
            '(0x35, "tdma_adjust_no_channel",'
            '"TDMA adjust: no channel"),'
            '(0x36, "tdma_adjust_blacklist",'
            '"TDMA adjust: channel change due to blacklisting"),'
            '(0x37, "tdma_adjust_unknown",'
            '"TDMA adjust: unknown reason"),'
            '(0x38, "peripheral_fail_unknown",'
            '"Peripheral failure: unknown reason"),'
            '(56, "sink_changed",'
            '"Changed routing to another sink"),'
            '(57, "fhma_adjust",'
            '"FHMA adjust event"),'
            '(0x40, "routing_loop_unknown",'
            '"Routing loop: unknown reason"), '
            '(72, "subnode_removed",'
            '"Removed subnode member in favour of headnode"),'
            '(73, "ll_dl_fail_chead",'
            '"Cluster head: removed member due to failing LL downklink"), '
            '(74, "ll_dl_fail_member", '
            '"Member: removed from the cluster head due to failing'
            ' LL downlink"), '
            '(75, "ll_ul_fail",'
            '"Cluster removed due to failing LL communication"),'
            '(76, "scan too many results",'
            '"Too many scan results to process (could also be temporal)"),'
            '(77, "own_active_late",'
            '"Own active start was late");'
        )

        self.cursor.execute(createtable)

        # Create file name hashes table
        # Note: population of this cannot be done here. Content is due to
        # change

        createtable = (
            "CREATE TABLE IF NOT EXISTS file_name_hashes ("
            "id SMALLINT UNSIGNED NOT NULL UNIQUE,"
            "name TEXT,"
            "PRIMARY KEY (id)"
            ") ENGINE = MYISAM;"
        )

        self.cursor.execute(createtable)

        # Create trigger for updating known nodes when received packets is
        # inserted
        trigger = "DROP TRIGGER IF EXISTS after_received_packets_insert;"
        self.cursor.execute(trigger)

        # Note, there is no DELIMITER here
        trigger = """CREATE
                       TRIGGER after_received_packets_insert AFTER INSERT
                       ON received_packets
                       FOR EACH ROW BEGIN
                           INSERT INTO known_nodes (network_address, node_address, last_time)
                           VALUES
                           (new.network_address,new.source_address, CURRENT_TIMESTAMP)
                           ON DUPLICATE KEY UPDATE last_time=CURRENT_TIMESTAMP;
                       END;"""
        self.cursor.execute(trigger)

        # Create a trigger for updating diagnostics node information to known
        # nodes
        trigger = "DROP TRIGGER IF EXISTS after_diagnostics_node_insert;"
        self.cursor.execute(trigger)

        trigger = """CREATE TRIGGER `after_diagnostics_node_insert` AFTER INSERT
                     ON `diagnostic_node`
                     FOR EACH ROW BEGIN
                        INSERT INTO known_nodes (network_address, node_address, voltage, node_role)
                        SELECT rp.network_address,
                            rp.source_address,
                            new.voltage,
                            new.node_role
                        FROM received_packets rp
                        WHERE rp.id = new.received_packet
                        ON DUPLICATE KEY UPDATE voltage=new.voltage,node_role=new.node_role;
                     END;"""
        self.cursor.execute(trigger)

        # Create a trigger for updating boot info to known nodes
        trigger = "DROP TRIGGER IF EXISTS after_diagnostic_boot_insert;"
        self.cursor.execute(trigger)

        trigger = """CREATE TRIGGER `after_diagnostic_boot_insert` AFTER INSERT ON `diagnostic_boot` FOR EACH ROW BEGIN
        INSERT INTO known_nodes (
            network_address,
            node_address,
             node_role,
              firmware_version,
              scratchpad_seq,
              hw_magic,
              stack_profile,
              boot_count,
              file_line_num,
              file_name_hash)
        SELECT rp.network_address,
            rp.source_address,
            new.node_role,
            new.firmware_version,
            new.scratchpad_seq,
            new.hw_magic,
            new.stack_profile,
            new.boot_count,
            new.file_line_num,
            new.file_name_hash
        FROM received_packets rp
        WHERE rp.id=new.received_packet
        ON DUPLICATE KEY UPDATE
        node_role=new.node_role,
        firmware_version=new.firmware_version,
        scratchpad_seq=new.scratchpad_seq,
        hw_magic=new.hw_magic,
        stack_profile=new.stack_profile,
        boot_count=new.boot_count,
        file_line_num=new.file_line_num,
        file_name_hash=new.file_name_hash;
        END;"""
        self.cursor.execute(trigger)

        # Create the debug log table
        createtable = (
            "CREATE TABLE IF NOT EXISTS log("
            "  recordtime text,"
            "  debuglog text"
            "  ) ENGINE = MYISAM;"
        )
        self.cursor.execute(createtable)

        self.database.commit()

    def put_advertiser(self, messages):
        """ Writes the advertiser message to the table """
        values = list()

        for message in messages:
            for node_address, fields in message.advertisers.items():
                self.logger.debug(
                    "inserting advertiser message in mysql: {}".format(message)
                )
                for value in fields["value"]:
                    values.append(
                        (
                            datetime.datetime.utcnow().isoformat(),
                            datetime.datetime.utcfromtimestamp(
                                message.timestamp / 1e3
                            ).isoformat(),
                            message.source_address,
                            message.destination_address,
                            message.source_endpoint,
                            message.destination_endpoint,
                            message.travel_time_ms,
                            message.qos,
                            message.apdu_message_type,
                            message.apdu_reserved_field,
                            node_address,
                            value,
                            str(binascii.hexlify(message.payload)),
                        )
                    )

        query = (
            "INSERT INTO advertiser("
            "logged_time,"
            "measured_time,"
            "source_address,"
            "destination_address,"
            "source_endpoint,"
            "destination_endpoint,"
            "travel_time_ms,"
            "qos,"
            "message_type,"
            "reserved,"
            "node_address,"
            "value,"
            "payload) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s);"
        )

        self.cursor.executemany(query, values)
        self.database.commit()

    def put_to_received_packets(self, message):
        # Insert received packet to the database
        try:
            hop_count = message.hop_count
        except:
            hop_count = 0
        query = (
            "INSERT INTO received_packets (logged_time, launch_time, path_delay_ms, network_address, sink_address, source_address, "
            "dest_address, source_endpoint, dest_endpoint, qos, num_bytes, hop_count) "
            "VALUES (from_unixtime({}), from_unixtime({}), {}, {}, {}, {}, {}, {}, {}, {}, {}, {});".format(
                message.rx_time_ms_epoch / 1000,
                (message.rx_time_ms_epoch - message.travel_time_ms) / 1000,
                message.travel_time_ms,
                message.network_id,
                message.destination_address,
                message.source_address,
                message.destination_address,
                message.source_endpoint,
                message.destination_endpoint,
                message.qos,
                len(message.data_payload),
                hop_count,
            )
        )
        self.cursor.execute(query)
        self.database.commit()

    def put_traffic_diagnostics(self, message):
        # Put first to received packets to get the received packet id
        self.put_to_received_packets(message)

        query = (
            "INSERT INTO diagnostic_traffic "
            "(received_packet, access_cycles, cluster_channel, "
            "channel_reliability, rx_count, tx_count, aloha_rxs, resv_rx_ok, "
            "data_rxs, dup_rxs, cca_ratio, bcast_ratio, tx_unicast_fail, "
            "resv_usage_max, resv_usage_avg, aloha_usage_max)"
            "VALUES (LAST_INSERT_ID(),{},{},{},{},{},{},{},{},{},{},{},{},{},{},{});".format(
                message.apdu["access_cycles"],
                message.apdu["cluster_channel"],
                message.apdu["channel_reliability"],
                message.apdu["rx_amount"],
                message.apdu["tx_amount"],
                message.apdu["aloha_rx_ratio"],
                message.apdu["reserved_rx_success_ratio"],
                message.apdu["data_rx_ratio"],
                message.apdu["rx_duplicate_ratio"],
                message.apdu["cca_success_ratio"],
                message.apdu["broadcast_ratio"],
                message.apdu["failed_unicast_ratio"],
                message.apdu["max_reserved_slot_usage"],
                message.apdu["average_reserved_slot_usage"],
                message.apdu["max_aloha_slot_usage"],
            )
        )

        self.cursor.execute(query)
        self.database.commit()

    def put_neighbor_diagnostics(self, message):
        # Put first to received packets to get the received packet id
        self.put_to_received_packets(message)

        # See if any neighbors, do not do insert
        try:
            if message.neighbor[0]["address"] == 0:
                return
        except KeyError:
            return

        # Insert all neighbors at once
        values = []
        for i in range(0, 14):
            try:
                if message.neighbor[i]["address"] == 0:
                    break
            except KeyError:
                # Number of neighbors depends on profile and can be less than
                # 14
                break

            values.append(
                "(LAST_INSERT_ID(),{},{},{},{},{})".format(
                    message.neighbor[i]["address"],
                    message.neighbor[i]["cluster_channel"],
                    message.neighbor[i]["radio_power"],
                    message.neighbor[i]["node_info"],
                    message.neighbor[i]["rssi"],
                )
            )

        query = (
            "INSERT INTO diagnostic_neighbor "
            "(received_packet, node_address, cluster_channel, "
            "radio_power, device_info, norm_rssi) "
            "VALUES {};".format(",".join(values))
        )

        self.cursor.execute(query)
        self.database.commit()

    def put_boot_diagnostics(self, message):
        # Put first to received packets to get the received packet id
        self.put_to_received_packets(message)

        # Firmware version contains values of major.minor.maint.devel, all 8
        # bytes
        firmware_version = message.apdu["sw_major_version"]
        firmware_version = (
            firmware_version * 256 + message.apdu["sw_minor_version"]
        )
        firmware_version = (
            firmware_version * 256 + message.apdu["sw_maint_version"]
        )
        firmware_version = (
            firmware_version * 256 + message.apdu["sw_dev_version"]
        )

        query = (
            "INSERT INTO diagnostic_boot "
            "(received_packet, boot_count, node_role, firmware_version, "
            "scratchpad_seq, hw_magic, stack_profile, otap_enabled, "
            "file_line_num, file_name_hash, stack_trace_0, stack_trace_1, "
            "stack_trace_2, current_seq) "
            "VALUES (LAST_INSERT_ID(), {}, {}, {}, {}, {}, {}, {}, {}, "
            "{}, {}, {}, {}, {});".format(
                message.apdu["boot_count"],
                message.apdu["node_role"],
                firmware_version,
                message.apdu["scratchpad_sequence"],
                message.apdu["hw_magic"],
                message.apdu["stack_profile"],
                message.apdu["otap_enabled"],
                message.apdu["boot_line_number"],
                message.apdu["file_hash"],
                message.apdu["stack_trace_0"],
                message.apdu["stack_trace_1"],
                message.apdu["stack_trace_2"],
                message.apdu["cur_seq"],
            )
        )
        self.cursor.execute(query)
        self.database.commit()

    def put_node_diagnostics(self, message):
        # Put first to received packets to get the received packet id
        self.put_to_received_packets(message)

        # Remember the last received packet (that was received_packets)
        last_received_packet = self.cursor.lastrowid

        voltage = float(message.apdu["voltage"]) / 100.0 + 2.0
        query = (
            "INSERT INTO diagnostic_node "
            "(received_packet, access_cycle_ms, node_role, voltage, "
            "buf_usage_max, buf_usage_avg, mem_alloc_fails, "
            "tc0_delay, tc1_delay, network_scans, "
            "downlink_delay_avg_0, downlink_delay_min_0, "
            "downlink_delay_max_0, downlink_delay_samples_0, "
            "downlink_delay_avg_1, downlink_delay_min_1, "
            "downlink_delay_max_1, downlink_delay_samples_1, "
            "dropped_packets_0, dropped_packets_1, route_address, "
            "next_hop_address_0, cost_0, quality_0, "
            "next_hop_address_1, cost_1, quality_1) "
            "VALUES (LAST_INSERT_ID(),{},{},{},{},{},{},{},{},{},{},{},{},{},"
            "{},{},{},{},{},{},{},{},{},{},{},{},{});".format(
                message.apdu["access_cycle"],
                message.apdu["role"],
                voltage,
                message.apdu["max_buffer_usage"],
                message.apdu["average_buffer_usage"],
                message.apdu["mem_alloc_fails"],
                message.apdu["normal_priority_buf_delay"],
                message.apdu["high_priority_buf_delay"],
                message.apdu["scans"],
                message.apdu["dl_delay_avg_0"],
                message.apdu["dl_delay_min_0"],
                message.apdu["dl_delay_max_0"],
                message.apdu["dl_delay_samples_0"],
                message.apdu["dl_delay_avg_1"],
                message.apdu["dl_delay_min_1"],
                message.apdu["dl_delay_max_1"],
                message.apdu["dl_delay_samples_1"],
                message.apdu["dropped_packets_0"],
                message.apdu["dropped_packets_1"],
                message.apdu["route_address"],
                message.apdu["cost_info_next_hop_0"],
                message.apdu["cost_info_cost_0"],
                message.apdu["cost_info_link_quality_0"],
                message.apdu["cost_info_next_hop_1"],
                message.apdu["cost_info_cost_1"],
                message.apdu["cost_info_link_quality_1"],
            )
        )

        self.cursor.execute(query)
        self.database.commit()

        # Create events
        events = []
        for i in range(0, 15):
            event = message.apdu["events_{}".format(i)]
            if event != 0:
                events.append(
                    "({},{},{})".format(last_received_packet, i, event)
                )

        if len(events) > 0:
            query = (
                "INSERT INTO diagnostic_event "
                "(received_packet, position, event) "
                "VALUES {};".format(",".join(events))
            )
            self.cursor.execute(query)
            self.database.commit()

    def put_testnw_measurements(self, message):
        self.put_to_received_packets(message)

        for row in range(message.row_count):
            table_name = "TestData_ID_" + str(message.testdata_id[row])

            self._create_testnw_mysql_table(
                self.cursor, self.database, table_name
            )

            data_column_names = ",".join(
                map(
                    lambda x: "DataCol_" + str(x),
                    range(1, message.number_of_fields[row] + 1),
                )
            )

            data_column_values = ",".join(map(str, message.datafields[row]))

            query = (
                "INSERT INTO "
                + table_name
                + " "
                + "(received_packet,"
                + "logged_time,"
                + "launch_time,"
                + "field_count,"
                + "ID_ctrl,"
                + data_column_names
                + ")"
                + " VALUES ("
                + "LAST_INSERT_ID(),"
                + "{0:.32f}".format(message.rx_time_ms_epoch / 1000, ".32f")
                + ","
                + "{0:.32f}".format(
                    (message.rx_time_ms_epoch - message.travel_time_ms) / 1000,
                    ".32f",
                )
                + ","
                + str(message.number_of_fields[row])
                + ","
                + str(message.id_ctrl[row])
                + ","
                + data_column_values
                + ")"
            )

            self.cursor.execute(query)
            self.database.commit()

    def _create_testnw_mysql_table(self, cursor, database, table_name):
        default_column_count = 30

        query = """
CREATE TABLE IF NOT EXISTS `{}` (
`received_packet` int(11) DEFAULT NULL,
`logged_time` double DEFAULT NULL,
`launch_time` double DEFAULT NULL,
`ID_ctrl` INT UNSIGNED DEFAULT NULL,
`field_count` int DEFAULT 0""".format(
            table_name
        )

        for i in range(1, default_column_count + 1):
            query += ",\n`DataCol_{}` INT UNSIGNED DEFAULT NULL".format(i)

        query += "\n) ENGINE=MyISAM DEFAULT CHARSET=utf8;"

        cursor.execute(query)
        database.commit()