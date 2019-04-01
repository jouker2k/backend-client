"""
    Metadata
    ===========

    .. Copyright:
        Copyright 2018 Wirepas Ltd. All Rights Reserved.
        See file LICENSE.txt for full license details.

"""

from wirepas_messaging.wnt import ApplicationConfigurationMessages
from wirepas_messaging.wnt import AreaMessages
from wirepas_messaging.wnt import FloorPlanMessages
from wirepas_messaging.wnt import BuildingMessages
from wirepas_messaging.wnt import NetworkMessages
from wirepas_messaging.wnt import NodeMessages

from ..sock import WNTSocket
from .manager import Manager


class MetadataManager(Manager):
    """MetadataManager

    This class handles the metadata connection and defines the runtime
    behaviour associated with the metadata.

    Attributes:
        hostname
        protocol_version
        port
        name
        messages
    """

    def __init__(
        self,
        hostname,
        protocol_version,
        port=None,
        name="Medatada",
        logger=None,
        **kwargs
    ):

        super(MetadataManager, self).__init__(
            name=name,
            hostname=hostname,
            port=port or WNTSocket.METADATA_PORT,
            on_open=self.on_open,
            logger=logger,
            kwargs=kwargs,
        )

        self.logger = logger or logging.getLogger(__name__)

        self.messages = dict()
        self.messages["building"] = BuildingMessages(
            self.logger, protocol_version
        )
        self.messages["application"] = ApplicationConfigurationMessages(
            self.logger, protocol_version
        )
        self.messages["area"] = AreaMessages(self.logger, protocol_version)
        self.messages["floorplan"] = FloorPlanMessages(
            self.logger, protocol_version
        )
        self.messages["building"] = BuildingMessages(
            self.logger, protocol_version
        )
        self.messages["network"] = NetworkMessages(
            self.logger, protocol_version
        )
        self.messages["node"] = NodeMessages(self.logger, protocol_version)

    def on_open(self, websocket) -> None:
        """Websocket callback when the authentication websocket has been opened

        Args:
            websocket (Websocket): communication socket
        """
        super().on_open(websocket)
        self.wait_for_session()