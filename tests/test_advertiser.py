# Copyright 2018 Wirepas Ltd. All Rights Reserved.
#
# See file LICENSE.txt for full license details.

import os
import time
import queue
import random
import pandas
import logging
import datetime
import multiprocessing


import wirepas_backend_client as wm_gwcli
from wirepas_backend_client.tools import Settings, ParserHelper, LoggerHelper
from wirepas_backend_client.messages import AdvertiserMessage
try:
    import MySQLdb
    mysql_enabled = True
    storage_name = "mysql"
except ImportError:
    print('Could not import MySQL module')
    mysql_enabled = False
    storage_name = "mysql"

__test_name__ = 'test_advertiser'


class AdvertiserManager(wm_gwcli.test.TestManager):
    """
    Test Manager for the Advertiser use case

    Attributes:
        tx_queue: where a final report is sent
        rx_queue: where Advertiser messages arrive
        exit_signal: signals an exit request
        inventory_target_nodes: nodes to look for during the inventory
        inventory_target_otap: otap sequence to track during inventory
        delay: amount of seconds to wait before starting test
        duration: maximum duration of the test
        logger: package logger

    """

    def __init__(self,
                 tx_queue: multiprocessing.Queue,
                 rx_queue: multiprocessing.Queue,
                 start_signal: multiprocessing.Event,
                 exit_signal: multiprocessing.Event,
                 storage_queue: multiprocessing.Queue = None,
                 inventory_target_nodes: set = set(),
                 inventory_target_otap: int = None,
                 inventory_target_frequency: int = None,
                 delay: int = 5,
                 duration: int = 5,
                 logger=None):

        super(AdvertiserManager, self).__init__(tx_queue=tx_queue,
                                                rx_queue=rx_queue,
                                                start_signal=start_signal,
                                                exit_signal=exit_signal,
                                                logger=logger)

        self.storage_queue = storage_queue
        self.delay = delay
        self.duration = duration

        self.inventory = wm_gwcli.messages.Inventory(
            target_nodes=inventory_target_nodes,
            target_otap_sequence=inventory_target_otap,
            target_frequency=inventory_target_frequency,
            start_delay=delay,
            maximum_duration=duration,
            logger=self.logger)

        self._test_sequence_number = 0
        self._timeout = 1
        self._tasks = list()

    def inventory_init(self)->None:
        """
        Calculates the event times for:

        * inventory startup
        * inventory start
        * inventory deadline

        and sleeps until it is time to start.

        """
        pass

    def test_inventory(self, test_sequence_number=0)->None:
        """
        Inventory test

        This test starts by calculating the time when it should start counting
        and when it should stop its inventory.

        Afterwards, before the time to start the count is reached, any message
        coming in the queue is discarded. Discarding messages is necessary
        otherwise it would lead to false results.

        """

        self._test_sequence_number = test_sequence_number
        self.inventory.sequence = test_sequence_number
        self.inventory.wait()
        self.start_signal.set()
        self.logger.info('starting inventory #{}'.format(
            test_sequence_number), dict(sequence=self._test_sequence_number))

        AdvertiserMessage.MESSAGE_COUNTER = 0
        empty_counter = 0
        message_index = 0

        while not self.exit_signal.is_set():
            try:
                message = self.rx_queue.get(timeout=self._timeout,
                                            block=True)
                empty_counter = 0
            except queue.Empty:
                empty_counter = empty_counter + 1
                if empty_counter > 10:
                    self.logger.debug('Advertiser messages '
                                      'are not being received')
                    empty_counter = 0

                if self.inventory.is_out_of_time():
                    break
                else:
                    continue

            message.count()
            message.decode()

            self.logger.info('#{} sent@{} received@{} diff: {} ms'.format(
                message.index,
                message.sent_at.isoformat(),
                message.received_at.isoformat(),
                round(message.transport_delay * 1e3, 2)))

            if self.storage_queue:
                self.storage_queue.put(message)
                if self.storage_queue.qsize() > 100:
                    self.logger.critical("storage queue is too big")

            # create map of advertisers
            for node_address, details in message.advertisers.items():
                self.inventory.add(node_address=node_address,
                                   rss=details['rss'],
                                   otap_sequence=details['otap'],
                                   timestamp=details['time'])

            if self.inventory.is_out_of_time():
                break

            if self.inventory.is_complete():
                self.logger.info('inventory completed for all target nodes',
                                 dict(sequence=self._test_sequence_number))
                break

            if self.inventory.is_otaped():
                self.logger.info('inventory completed for all otap targets',
                                 dict(sequence=self._test_sequence_number))
                break

            if self.inventory.is_frequency_reached():
                self.logger.info('inventory completed for frequency target',
                                 dict(sequence=self._test_sequence_number))
                break

            # self.logger.info(str(self.inventory),
            #                 dict(sequence=self._test_sequence_number))

        self.inventory.finish()
        report = self.report()
        self.tx_queue.put(report)
        record = dict(test_sequence_number=self._test_sequence_number,
                      total_nodes=report['observed_total'],
                      inventory_start=report['start'].isoformat('T'),
                      inventory_end=report['end'].isoformat('T'),
                      node_frequency=str(report['node_frequency']),
                      frequency_by_value=str(report['frequency_by_value']),
                      target_nodes=str(self.inventory.target_nodes),
                      target_otap=str(self.inventory.target_otap_sequence),
                      target_frequency=str(self.inventory.target_frequency),
                      difference=str(self.inventory.difference()),
                      elapsed=report['elapsed'])
        record['@timestamp'] = record['inventory_start']

        # if record['total_nodes'] < 100:
        #    for k, v in report['node_frequency'].items():
        #        record[str(k)] = str(v)

        self.logger.info(record, dict(sequence=self._test_sequence_number))

    def report(self) -> dict:
        """
        Returns a string with the gathered results.
        """
        msg = dict(title="{}:{}".format(__test_name__, self._test_sequence_number),
                   start=self.inventory.start,
                   end=self.inventory.finish(),
                   elapsed=self.inventory.elapsed,
                   difference=self.inventory.difference(),
                   inventory_target_nodes=self.inventory.target_nodes,
                   inventory_target_otap=self.inventory.target_otap_sequence,
                   inventory_target_frequency=self.inventory.target_frequency,
                   node_frequency=self.inventory.frequency(),
                   frequency_by_value=self.inventory.frequency_by_value(),
                   observed_total=len(self.inventory.nodes),
                   observed=self.inventory.nodes)
        return msg


def fetch_report(args, rx_queue, timeout, report_output, number_of_runs,
                 exit_signal):
    """ Reporting loop executed between test runs """
    reports = {}
    for run in range(0, number_of_runs):
        try:
            report = rx_queue.get(timeout=timeout, block=True)
            reports[run] = report
        except queue.Empty:
            report = None
            logger.warning('timed out waiting for report')

        if exit_signal.is_set():
            raise RuntimeError

    df = pandas.DataFrame.from_dict(reports)
    if args.output_time:
        filepath = "{}_{}".format(
            datetime.datetime.now().isoformat(), args.output)
    else:
        filepath = "{}".format(args.output)

    df.to_json(filepath)


def main(args, logger):
    """ Main loop """

    # process management
    daemon = wm_gwcli.management.Daemon(logger=logger)

    if args.db_hostname:
        mysql_enabled = True
        mysql_process = daemon.build(
            storage_name,
            wm_gwcli.api.MySQLObserver,
            dict(mysql_settings=wm_gwcli.api.MySQLSettings(args)))

        daemon.set_run(storage_name,
                       task_kwargs=dict(
                           parallel=True),
                       task_as_daemon=False)

    else:
        mysql_enabled = False
        logger.info('Skipping Storage module')

    mqtt_process = daemon.build("mqtt",
                                wm_gwcli.api.MQTTObserver,
                                dict(mqtt_settings=wm_gwcli.api.MQTTSettings(args),
                                     message_publish_handlers=dict(),
                                     logger=logger,
                                     allowed_endpoints=set(
                                    [wm_gwcli.messages.AdvertiserMessage.ADVERTISER_SRC_EP])))

    daemon.set_run("mqtt",
                   task=mqtt_process.run,
                   task_kwargs=dict(message_subscribe_handlers={
                       "gw-event/received_data/#": mqtt_process.generate_data_received_cb()}))

    # build each process and set the communication
    adv_manager = daemon.build("adv_manager",
                               AdvertiserManager,
                               dict(inventory_target_nodes=args.target_nodes,
                                    inventory_target_otap=args.target_otap,
                                    inventory_target_frequency=args.target_frequency,
                                    logger=logger,
                                    delay=args.delay,
                                    duration=args.duration),
                               receive_from="mqtt",
                               storage=mysql_enabled,
                               storage_name=storage_name)

    adv_manager.execution_jitter(min=args.jitter_minimum,
                                 max=args.jitter_maximum)
    adv_manager.register_task(adv_manager.test_inventory,
                              number_of_runs=args.number_of_runs)

    daemon.set_loop(fetch_report,
                    dict(args=args,
                         rx_queue=adv_manager.tx_queue,
                         timeout=args.delay + args.duration + 60,
                         report_output=args.output,
                         number_of_runs=args.number_of_runs,
                         exit_signal=daemon.exit_signal))
    daemon.start()


if __name__ == "__main__":

    parse = wm_gwcli.tools.ParserHelper(description="Default arguments")

    parse.add_mqtt()
    parse.add_test()
    parse.add_database()
    parse.add_fluentd()

    settings = parse.settings(skip_undefined=False)

    try:
        debug_level = os.environ['DEBUG_LEVEL']
    except KeyError:
        debug_level = 'debug'

    my_log = wm_gwcli.tools.LoggerHelper(module_name=__test_name__,
                                         args=settings,
                                         level=debug_level)
    logger = my_log.setup(level=debug_level)

    try:
        inventory_target_otap = settings.target_otap
    except AttributeError:
        settings.target_otap = None

    try:
        inventory_target_frequency = settings.target_frequency
    except AttributeError:
        settings.target_frequency = None

    if settings.delay is None:
        settings.delay = random.randrange(0, 60)

    try:
        nodes = set(eval(settings.nodes))
    except NameError:
        settings.target_nodes = set([int(line)
                                     for line in open(settings.nodes, 'r')])
    else:
        settings.target_nodes = set()

    if settings.jitter_minimum > settings.jitter_maximum:
        settings.jitter_maximum = settings.jitter_minimum

    logger.info({
        'test_suite_start': datetime.datetime.utcnow().isoformat('T'),
        'run_arguments': str(settings)})
    main(settings, logger)

    parse.dump("run_information_{}.txt".format(
        datetime.datetime.now().isoformat()))


# import atexit
#@atexit.register