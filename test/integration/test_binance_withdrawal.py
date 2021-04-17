from os.path import join, realpath
import sys
import asyncio
import conf
import contextlib
from decimal import Decimal
import logging
import os
import time
import math
from typing import (
    List,
    Dict,
    Optional
)
import unittest
from unittest.mock import patch

from hummingbot.core.clock import (
    Clock,
    ClockMode
)
from hummingbot.core.event.events import (
    BuyOrderCompletedEvent,
    BuyOrderCreatedEvent,
    MarketEvent,
    OrderCancelledEvent,
    OrderFilledEvent,
    OrderType,
    SellOrderCompletedEvent,
    SellOrderCreatedEvent,
    MarketOrderFailureEvent,
    TradeFee,
    TradeType,
)
from hummingbot.core.event.event_logger import EventLogger
from hummingbot.core.utils.async_utils import (
    safe_ensure_future,
    safe_gather,
)
from hummingbot.logger.struct_logger import METRICS_LOG_LEVEL
from hummingbot.connector.exchange.binance.binance_exchange import (
    BinanceExchange,
    BinanceTime,
    binance_client_module
)
from hummingbot.connector.exchange.binance.binance_utils import convert_to_exchange_trading_pair
from hummingbot.connector.markets_recorder import MarketsRecorder
from hummingbot.model.market_state import MarketState
from hummingbot.model.order import Order
from hummingbot.model.sql_connection_manager import (
    SQLConnectionManager,
    SQLConnectionType
)
from hummingbot.model.trade_fill import TradeFill
from hummingbot.client.config.fee_overrides_config_map import fee_overrides_config_map
from test.integration.assets.mock_data.fixture_binance import FixtureBinance
from test.integration.humming_web_app import HummingWebApp
from unittest import mock
import requests
from test.integration.humming_ws_server import HummingWsServerFactory
sys.path.insert(0, realpath(join(__file__, "../../bin")))

MAINNET_RPC_URL = "http://mainnet-rpc.mainnet:8545"
logging.basicConfig(level=METRICS_LOG_LEVEL)
API_MOCK_ENABLED = conf.mock_api_enabled is not None and conf.mock_api_enabled.lower() in ['true', 'yes', '1']
API_KEY = "XXX" if API_MOCK_ENABLED else conf.binance_api_key
API_SECRET = "YYY" if API_MOCK_ENABLED else conf.binance_api_secret


class BinanceExchangeUnitTest(unittest.TestCase):
    events: List[MarketEvent] = [
        MarketEvent.ReceivedAsset,
        MarketEvent.BuyOrderCompleted,
        MarketEvent.SellOrderCompleted,
        MarketEvent.OrderFilled,
        MarketEvent.TransactionFailure,
        MarketEvent.BuyOrderCreated,
        MarketEvent.SellOrderCreated,
        MarketEvent.OrderCancelled,
        MarketEvent.OrderFailure
    ]

    market: BinanceExchange
    market_logger: EventLogger
    stack: contextlib.ExitStack
    base_api_url = "api.binance.com"

    @classmethod
    def setUpClass(cls):
        global MAINNET_RPC_URL

        cls.ev_loop = asyncio.get_event_loop()

        if API_MOCK_ENABLED:
            cls.web_app = HummingWebApp.get_instance()
            cls.web_app.add_host_to_mock(cls.base_api_url, ["/api/v1/ping", "/api/v1/time", "/api/v1/ticker/24hr"])
            cls.web_app.start()
            cls.ev_loop.run_until_complete(cls.web_app.wait_til_started())
            cls._patcher = mock.patch("aiohttp.client.URL")
            cls._url_mock = cls._patcher.start()
            cls._url_mock.side_effect = cls.web_app.reroute_local

            cls._req_patcher = unittest.mock.patch.object(requests.Session, "request", autospec=True)
            cls._req_url_mock = cls._req_patcher.start()
            cls._req_url_mock.side_effect = HummingWebApp.reroute_request
            cls.web_app.update_response("get", cls.base_api_url, "/api/v3/account", FixtureBinance.BALANCES)
            cls.web_app.update_response("get", cls.base_api_url, "/api/v1/exchangeInfo",
                                        FixtureBinance.MARKETS)
            cls.web_app.update_response("get", cls.base_api_url, "/wapi/v3/tradeFee.html",
                                        FixtureBinance.TRADE_FEES)
            cls.web_app.update_response("post", cls.base_api_url, "/api/v1/userDataStream",
                                        FixtureBinance.LISTEN_KEY)
            cls.web_app.update_response("put", cls.base_api_url, "/api/v1/userDataStream",
                                        FixtureBinance.LISTEN_KEY)
            cls.web_app.update_response("get", cls.base_api_url, "/api/v1/depth",
                                        FixtureBinance.LINKETH_SNAP, params={'symbol': 'LINKETH'})
            cls.web_app.update_response("get", cls.base_api_url, "/api/v1/depth",
                                        FixtureBinance.ZRXETH_SNAP, params={'symbol': 'ZRXETH'})
            cls.web_app.update_response("get", cls.base_api_url, "/api/v3/myTrades",
                                        {}, params={'symbol': 'ZRXETH'})
            cls.web_app.update_response("get", cls.base_api_url, "/api/v3/myTrades",
                                        {}, params={'symbol': 'LINKETH'})
            ws_base_url = "wss://stream.binance.com:9443/ws"
            cls._ws_user_url = f"{ws_base_url}/{FixtureBinance.LISTEN_KEY['listenKey']}"
            HummingWsServerFactory.start_new_server(cls._ws_user_url)
            HummingWsServerFactory.start_new_server(f"{ws_base_url}/linketh@depth/zrxeth@depth")
            cls._ws_patcher = unittest.mock.patch("websockets.connect", autospec=True)
            cls._ws_mock = cls._ws_patcher.start()
            cls._ws_mock.side_effect = HummingWsServerFactory.reroute_ws_connect

            cls._t_nonce_patcher = unittest.mock.patch(
                "hummingbot.connector.exchange.binance.binance_exchange.get_tracking_nonce")
            cls._t_nonce_mock = cls._t_nonce_patcher.start()
        cls.current_nonce = 1000000000000000
        cls.clock: Clock = Clock(ClockMode.REALTIME)
        cls.market: BinanceExchange = BinanceExchange(API_KEY, API_SECRET, ["LINK-ETH", "ZRX-ETH"], True)
        print("Initializing Binance market... this will take about a minute.")
        cls.ev_loop: asyncio.BaseEventLoop = asyncio.get_event_loop()
        cls.clock.add_iterator(cls.market)
        cls.stack: contextlib.ExitStack = contextlib.ExitStack()
        cls._clock = cls.stack.enter_context(cls.clock)
        cls.ev_loop.run_until_complete(cls.wait_til_ready())
        print("Ready.")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.stack.close()
        if API_MOCK_ENABLED:
            cls.web_app.stop()
            cls._patcher.stop()
            cls._req_patcher.stop()
            cls._ws_patcher.stop()
            cls._t_nonce_patcher.stop()

    @classmethod
    async def wait_til_ready(cls):
        while True:
            now = time.time()
            next_iteration = now // 1.0 + 1
            if cls.market.ready:
                break
            else:
                await cls._clock.run_til(next_iteration)
            await asyncio.sleep(1.0)

    def setUp(self):
        self.db_path: str = realpath(join(__file__, "../binance_test.sqlite"))
        try:
            os.unlink(self.db_path)
        except FileNotFoundError:
            pass

        self.market_logger = EventLogger()
        self.market._current_trade_fills = set()
        self.market._exchange_order_ids = dict()
        self.ev_loop.run_until_complete(self.wait_til_ready())
        for event_tag in self.events:
            self.market.add_listener(event_tag, self.market_logger)

    def tearDown(self):
        for event_tag in self.events:
            self.market.remove_listener(event_tag, self.market_logger)
        self.market_logger = None

    async def run_parallel_async(self, *tasks):
        future: asyncio.Future = safe_ensure_future(safe_gather(*tasks))
        while not future.done():
            now = time.time()
            next_iteration = now // 1.0 + 1
            await self._clock.run_til(next_iteration)
            await asyncio.sleep(1.0)
        return future.result()

    def run_parallel(self, *tasks):
        return self.ev_loop.run_until_complete(self.run_parallel_async(*tasks))

    @classmethod
    def get_current_nonce(cls):
        cls.current_nonce += 1
        return cls.current_nonce

    
    def test_withdraw(self):
        self.assertEqual(self.market.withdrawal("ETH","0x165b9d9e8b96c54099aa8b9e81b4b30760ab7f2e",0.001,""),1)

if __name__ == "__main__":
    logging.getLogger("hummingbot.core.event.event_reporter").setLevel(logging.WARNING)
    unittest.main()
