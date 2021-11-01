"""
Copyright 2018 Grid Singularity
This file is part of D3A.

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

from typing import Dict, Union
from unittest.mock import MagicMock, Mock, call

import pytest
from d3a_interface.constants_limits import ConstSettings, GlobalConfig
from d3a_interface.enums import SpotMarketTypeEnum
from pendulum import DateTime, datetime
from pendulum import duration

from d3a.events.event_structures import MarketEvent, AreaEvent
from d3a.models.area import Area
from d3a.models.area.event_dispatcher import AreaDispatcher
from d3a.models.market import Market
from d3a.models.market.balancing import BalancingMarket
from d3a.models.market.future import FutureMarkets
from d3a.models.market.market_structures import AvailableMarketTypes
from d3a.models.market.one_sided import OneSidedMarket
from d3a.models.market.settlement import SettlementMarket
from d3a.models.market.two_sided import TwoSidedMarket
from d3a.models.strategy.area_agents.balancing_agent import BalancingAgent
from d3a.models.strategy.area_agents.future_agent import FutureAgent
from d3a.models.strategy.area_agents.inter_area_agent import InterAreaAgent
from d3a.models.strategy.area_agents.one_sided_agent import OneSidedAgent
from d3a.models.strategy.area_agents.settlement_agent import SettlementAgent
from d3a.models.strategy.area_agents.two_sided_agent import TwoSidedAgent


# pylint: disable=W0212


@pytest.fixture(name="area_dispatcher")
def area_dispatcher_fixture():
    """Return fixture for AreaDispatcher object."""
    area = Area("name")
    area.children = [Area("child1"), Area("child2")]
    area.parent = Area("parent")
    return AreaDispatcher(area)


@pytest.fixture(name="area_dispatcher_area_markets")
def area_dispatcher_area_markets_fixture():
    """Return fixture for AreaDispatcher object."""
    config = Mock()
    config.slot_length = duration(minutes=15)
    config.tick_length = duration(seconds=15)
    config.ticks_per_slot = 60
    config.start_date = GlobalConfig.start_date
    config.grid_fee_type = ConstSettings.IAASettings.GRID_FEE_TYPE
    config.end_date = GlobalConfig.start_date + duration(days=1)

    area = Area("name", config=config)
    area.children = [Area("child1"), Area("child2")]
    area.parent = Area("parent")
    area.activate()
    area.cycle_markets()
    area._markets.settlement_markets = {config.start_date: MagicMock(autospec=SettlementMarket)}
    area._markets.balancing_markets = {config.start_date: MagicMock(autospec=BalancingMarket)}
    area._markets.update_area_market_id_lists()

    return AreaDispatcher(area)


class TestAreaDispatcher:
    """Collection of tests for AreaDispatcher."""

    @staticmethod
    def _get_agents_for_market_type(
            dispatcher_object, market_type: AvailableMarketTypes
    ) -> Dict[DateTime, Union[OneSidedAgent, BalancingAgent, SettlementAgent]]:
        """Select the correct IAA dict in the AreaDispatcher depending on the market_type"""
        if market_type == AvailableMarketTypes.SPOT:
            return dispatcher_object.interarea_agents
        if market_type == AvailableMarketTypes.BALANCING:
            return dispatcher_object.balancing_agents
        if market_type == AvailableMarketTypes.SETTLEMENT:
            return dispatcher_object.settlement_agents
        assert False, f"Market type not supported {market_type}"

    @staticmethod
    def _create_iaa_and_markets_for_time_slot(dispatcher_object: AreaDispatcher,
                                              time_slot: DateTime,
                                              market_class: Market,
                                              market_type: AvailableMarketTypes):
        """Helps to create iaas for testing."""
        first_time_slot = time_slot
        lower_market = MagicMock(autospec=market_class)
        lower_market.time_slot = first_time_slot
        higher_market = MagicMock(autospec=market_class)
        higher_market.time_slot = first_time_slot

        dispatcher_object.area.parent.get_market_instances_from_class_type = Mock(
            return_value={first_time_slot: higher_market})

        dispatcher_object.create_area_agents(market_type, lower_market)

    # pylint: disable=too-many-arguments
    @pytest.mark.parametrize("market_type, spot_market_type, market_class, expected_agent_type", [
        [AvailableMarketTypes.SPOT, SpotMarketTypeEnum.ONE_SIDED.value, OneSidedMarket,
         OneSidedAgent],
        [AvailableMarketTypes.SPOT, SpotMarketTypeEnum.TWO_SIDED.value, TwoSidedMarket,
         TwoSidedAgent],
        [AvailableMarketTypes.SETTLEMENT, SpotMarketTypeEnum.TWO_SIDED.value, SettlementMarket,
         SettlementAgent],
        [AvailableMarketTypes.BALANCING, SpotMarketTypeEnum.TWO_SIDED.value, BalancingMarket,
         BalancingAgent]
    ])
    def test_create_area_agents_creates_correct_objects(self, market_type: AvailableMarketTypes,
                                                        spot_market_type: SpotMarketTypeEnum,
                                                        market_class: Market,
                                                        expected_agent_type: InterAreaAgent,
                                                        area_dispatcher):
        """Test if create_area_agents creates correct objects in the agent dicts."""
        original_matching_type = ConstSettings.IAASettings.MARKET_TYPE
        ConstSettings.IAASettings.MARKET_TYPE = spot_market_type

        lower_market = MagicMock(autospec=market_class)
        higher_market = MagicMock(autospec=market_class)
        area_dispatcher.area.parent.get_market_instances_from_class_type = Mock(
            return_value={lower_market.time_slot: higher_market})

        area_dispatcher.create_area_agents(market_type, lower_market)

        agent_dict = self._get_agents_for_market_type(area_dispatcher, market_type)

        assert lower_market.time_slot in agent_dict
        agent = agent_dict[lower_market.time_slot]
        assert isinstance(agent, expected_agent_type)
        assert agent.higher_market == higher_market
        assert agent.lower_market == lower_market

        ConstSettings.IAASettings.MARKET_TYPE = original_matching_type

    @staticmethod
    def test_create_area_agents_for_future_markets(area_dispatcher):
        """Test if the future agent is correctly created."""
        area_dispatcher.area.parent = Mock()
        area_dispatcher.area.parent.future_markets = MagicMock(autospec=FutureMarkets)
        area_dispatcher.create_area_agents_for_future_markets(MagicMock(autospec=FutureMarkets))
        assert isinstance(area_dispatcher.future_agent, FutureAgent)

    @pytest.mark.parametrize("market_type, market_class", [
        [AvailableMarketTypes.SPOT, OneSidedMarket],
        [AvailableMarketTypes.SPOT, TwoSidedMarket],
        [AvailableMarketTypes.SETTLEMENT, SettlementMarket],
        [AvailableMarketTypes.BALANCING, BalancingMarket],
                             ])
    def test_event_market_cycle_deletes_all_old_iaas(self, market_type: AvailableMarketTypes,
                                                     market_class: Market,
                                                     area_dispatcher):
        """Test whether iaas are deleted from the agent dicts."""

        first_time_slot = datetime(2021, 10, 27)
        self._create_iaa_and_markets_for_time_slot(area_dispatcher, first_time_slot,
                                                   market_class, market_type)
        second_time_slot = first_time_slot.add(minutes=15)
        self._create_iaa_and_markets_for_time_slot(area_dispatcher, second_time_slot,
                                                   market_class, market_type)

        agent_dict = self._get_agents_for_market_type(area_dispatcher, market_type)
        assert first_time_slot in agent_dict
        assert second_time_slot in agent_dict

        area_dispatcher.area = Mock()
        area_dispatcher.area.current_market.time_slot = second_time_slot
        area_dispatcher.event_market_cycle()
        assert first_time_slot not in agent_dict
        assert second_time_slot in agent_dict

    @staticmethod
    @pytest.mark.parametrize("event_type", [
        AreaEvent.TICK,
        AreaEvent.MARKET_CYCLE,
        AreaEvent.ACTIVATE,
        AreaEvent.BALANCING_MARKET_CYCLE,
    ])
    def test_broadcast_notification_triggers_correct_methods_area_events(
            event_type: Union[AreaEvent, MarketEvent], area_dispatcher_area_markets):
        """Test if broadcast_notification triggers correct dispatcher methods for area events."""
        kwargs = {"market_id": area_dispatcher_area_markets.area.spot_market.id}
        for child in area_dispatcher_area_markets.area.children:
            child.dispatcher.event_listener = Mock()
        area_dispatcher_area_markets._broadcast_notification_to_area_and_child_agents = Mock()

        area_dispatcher_area_markets.broadcast_notification(event_type, **kwargs)

        for child in area_dispatcher_area_markets.area.children:
            child.dispatcher.event_listener.assert_called_once()

        (area_dispatcher_area_markets._broadcast_notification_to_area_and_child_agents.
            assert_has_calls([
                call(AvailableMarketTypes.SPOT, event_type, **kwargs),
                call(AvailableMarketTypes.BALANCING, event_type, **kwargs),
                call(AvailableMarketTypes.SETTLEMENT, event_type, **kwargs),
                call(AvailableMarketTypes.FUTURE, event_type, **kwargs)]))

    @staticmethod
    @pytest.mark.parametrize("event_type, expected_market_type", [
        [MarketEvent.OFFER, AvailableMarketTypes.SPOT],
        [MarketEvent.BID_TRADED, AvailableMarketTypes.SPOT],
        [MarketEvent.OFFER, AvailableMarketTypes.FUTURE],
        [MarketEvent.BID_TRADED, AvailableMarketTypes.FUTURE],
        [MarketEvent.OFFER, AvailableMarketTypes.SETTLEMENT],
        [MarketEvent.BID_TRADED, AvailableMarketTypes.SETTLEMENT],
        [MarketEvent.OFFER, AvailableMarketTypes.BALANCING],
        [MarketEvent.BID_TRADED, AvailableMarketTypes.BALANCING],
    ])
    def test_broadcast_notification_triggers_correct_methods_market_events(
            event_type: Union[AreaEvent, MarketEvent],
            expected_market_type: AvailableMarketTypes,
            area_dispatcher_area_markets):
        """Test if broadcast_notification triggers correct dispatcher methods for market events."""

        market_id = None
        if expected_market_type == AvailableMarketTypes.SPOT:
            market_id = area_dispatcher_area_markets.area.spot_market.id
        if expected_market_type == AvailableMarketTypes.FUTURE:
            market_id = area_dispatcher_area_markets.area.future_markets.id
        if expected_market_type == AvailableMarketTypes.SETTLEMENT:
            market_id = list(area_dispatcher_area_markets.area.settlement_markets.values())[0].id
        if expected_market_type == AvailableMarketTypes.BALANCING:
            market_id = area_dispatcher_area_markets.area.balancing_markets[0].id

        kwargs = {"market_id": market_id}

        for child in area_dispatcher_area_markets.area.children:
            child.dispatcher.event_listener = Mock()
        area_dispatcher_area_markets._broadcast_notification_to_area_and_child_agents = Mock()

        area_dispatcher_area_markets.broadcast_notification(event_type, **kwargs)

        for child in area_dispatcher_area_markets.area.children:
            child.dispatcher.event_listener.assert_called_once()

        (area_dispatcher_area_markets._broadcast_notification_to_area_and_child_agents.
            assert_called_once_with(expected_market_type, event_type, **kwargs))
