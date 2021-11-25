"""
Copyright 2018 Grid Singularity
This file is part of Grid Singularity Exchange.

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
import uuid
from typing import Union, Dict, List, Optional  # noqa
from pendulum import DateTime
from logging import getLogger

from gsy_framework.data_classes import Offer
from gsy_e.models.market.one_sided import OneSidedMarket
from gsy_e.events.event_structures import MarketEvent
from gsy_framework.data_classes import BalancingOffer, BalancingTrade
from gsy_e.gsy_e_core.exceptions import InvalidOffer, MarketReadOnlyException, \
    OfferNotFoundException, InvalidBalancingTradeException, \
    DeviceNotInRegistryError
from gsy_e.gsy_e_core.util import short_offer_bid_log_str
from gsy_e.gsy_e_core.device_registry import DeviceRegistry
from gsy_e.constants import FLOATING_POINT_TOLERANCE
from gsy_framework.constants_limits import ConstSettings

log = getLogger(__name__)


class BalancingMarket(OneSidedMarket):
    def __init__(self, time_slot=None, bc=None, notification_listener=None, readonly=False,
                 grid_fee_type=ConstSettings.MASettings.GRID_FEE_TYPE,
                 grid_fees=None, name=None, in_sim_duration=True):
        self.unmatched_energy_upward = 0
        self.unmatched_energy_downward = 0
        self.accumulated_supply_balancing_trade_price = 0
        self.accumulated_supply_balancing_trade_energy = 0
        self.accumulated_demand_balancing_trade_price = 0
        self.accumulated_demand_balancing_trade_energy = 0

        super().__init__(time_slot, bc, notification_listener, readonly, grid_fee_type,
                         grid_fees, name, in_sim_duration=in_sim_duration)

    def offer(self, price: float, energy: float, seller: str, seller_origin: str,
              offer_id: Optional[str] = None,
              original_price: Optional[float] = None,
              dispatch_event: bool = True,
              adapt_price_with_fees: bool = True,
              add_to_history: bool = True,
              seller_origin_id: Optional[str] = None,
              seller_id: Optional[str] = None,
              attributes: Optional[Dict] = None,
              requirements: Optional[List[Dict]] = None,
              time_slot: Optional[DateTime] = None):
        assert False

    def balancing_offer(self, price: float, energy: float, seller: str,
                        original_price=None, offer_id=None, from_agent: bool = False,
                        adapt_price_with_fees: bool = False, dispatch_event=True,
                        seller_origin=None, attributes: Dict = None,
                        requirements: List[Dict] = None) -> BalancingOffer:
        if seller not in DeviceRegistry.REGISTRY.keys() and not from_agent:
            raise DeviceNotInRegistryError(f"Device {seller} "
                                           f"not in registry ({DeviceRegistry.REGISTRY}).")
        if self.readonly:
            raise MarketReadOnlyException()
        if energy == 0:
            raise InvalidOffer()
        if adapt_price_with_fees:
            if self._is_constant_fees:
                price = price + self.fee_class.grid_fee_rate * energy
            else:
                price = price * (1 + self.fee_class.grid_fee_rate)

        if offer_id is None:
            offer_id = str(uuid.uuid4())

        offer = BalancingOffer(
            offer_id, self.now, price, energy, seller,
            seller_origin=seller_origin, attributes=attributes,
            requirements=requirements, time_slot=self.time_slot)
        self.offers[offer.id] = offer

        self.offer_history.append(offer)
        log.debug(f"[BALANCING_OFFER][NEW][{self.time_slot_str}] {offer}")
        if dispatch_event is True:
            self._notify_listeners(MarketEvent.BALANCING_OFFER, offer=offer)
        return offer

    def split_offer(self, original_offer, energy, orig_offer_price=None):

        self.offers.pop(original_offer.id, None)
        # same offer id is used for the new accepted_offer

        accepted_offer = self.balancing_offer(offer_id=original_offer.id,
                                              price=original_offer.price *
                                              (energy / original_offer.energy),
                                              energy=energy,
                                              seller=original_offer.seller,
                                              dispatch_event=False,
                                              seller_origin=original_offer.seller_origin,
                                              from_agent=True,
                                              attributes=original_offer.attributes,
                                              requirements=original_offer.requirements)

        residual_price = (1 - energy / original_offer.energy) * original_offer.price
        residual_energy = original_offer.energy - energy
        if orig_offer_price is None:
            orig_offer_price = self._calculate_original_prices(original_offer)
        original_residual_price = (
                ((original_offer.energy - energy) / original_offer.energy) * orig_offer_price)

        residual_offer = self.balancing_offer(price=residual_price,
                                              energy=residual_energy,
                                              seller=original_offer.seller,
                                              original_price=original_residual_price,
                                              dispatch_event=False,
                                              seller_origin=original_offer.seller_origin,
                                              adapt_price_with_fees=False,
                                              from_agent=True,
                                              attributes=original_offer.attributes,
                                              requirements=original_offer.requirements)

        log.debug(f"[BALANCING_OFFER][SPLIT][{self.time_slot_str}, {self.name}] "
                  f"({short_offer_bid_log_str(original_offer)} into "
                  f"{short_offer_bid_log_str(accepted_offer)} and "
                  f"{short_offer_bid_log_str(residual_offer)}")

        self.bc_interface.change_offer(accepted_offer, original_offer, residual_offer)

        self._notify_listeners(
            MarketEvent.BALANCING_OFFER_SPLIT,
            original_offer=original_offer,
            accepted_offer=accepted_offer,
            residual_offer=residual_offer)

        return accepted_offer, residual_offer

    def determine_offer_price(self, energy_portion, energy, trade_rate,
                              trade_bid_info, orig_offer_price):
        return self._update_offer_fee_and_calculate_final_price(
            energy, trade_rate, energy_portion, orig_offer_price
        )

    def accept_offer(self, offer_or_id: Union[str, BalancingOffer], buyer: str, *,
                     energy: int = None,
                     already_tracked: bool = False, trade_rate: float = None,
                     trade_bid_info: float = None,
                     buyer_origin=None, buyer_origin_id=None, buyer_id=None) -> BalancingTrade:
        if self.readonly:
            raise MarketReadOnlyException()

        if isinstance(offer_or_id, Offer):
            offer_or_id = offer_or_id.id
        offer = self.offers.pop(offer_or_id, None)
        if offer is None:
            raise OfferNotFoundException()

        if (offer.energy > 0 > energy) or (offer.energy < 0 < energy):
            raise InvalidBalancingTradeException("BalancingOffer and energy "
                                                 "are not compatible")
        if energy is None:
            energy = offer.energy

        original_offer = offer
        residual_offer = None

        if trade_rate is None:
            trade_rate = offer.energy_rate

        orig_offer_price = self._calculate_original_prices(offer)

        try:
            if energy == 0:
                raise InvalidBalancingTradeException("Energy can not be zero.")
            elif abs(energy) < abs(offer.energy):
                # partial energy is requested
                assert trade_rate + FLOATING_POINT_TOLERANCE >= offer.energy_rate

                original_offer = offer

                accepted_offer, residual_offer = self.split_offer(offer, energy, orig_offer_price)

                fees, trade_price = self.determine_offer_price(
                    energy / offer.energy, energy, trade_rate, trade_bid_info, orig_offer_price)
                offer = accepted_offer
                offer.update_price(trade_price)

            elif abs(energy) > abs(offer.energy):
                raise InvalidBalancingTradeException("Energy can't be greater than offered energy")
            else:
                # Requested energy is equal to offer's energy - just proceed normally
                fees, trade_price = self._update_offer_fee_and_calculate_final_price(
                    energy, trade_rate, 1, orig_offer_price
                ) if already_tracked is False else energy * trade_rate
                offer.update_price(trade_price)

        except Exception:
            # Exception happened - restore offer
            self.offers[offer.id] = offer
            raise

        # Delete the accepted offer from self.offers:
        self.offers.pop(offer.id, None)

        trade_id, residual_offer = \
            self.bc_interface.handle_blockchain_trade_event(
                offer, buyer, original_offer, residual_offer
            )
        trade = BalancingTrade(trade_id, self.now, offer, offer.seller, buyer,
                               residual_offer, seller_origin=offer.seller_origin,
                               buyer_origin=buyer_origin, fee_price=fees,
                               seller_origin_id=offer.seller_origin_id,
                               seller_id=offer.seller_id,
                               buyer_origin_id=buyer_origin_id,
                               buyer_id=buyer_id, time_slot=offer.time_slot)
        self.bc_interface.track_trade_event(self.time_slot, trade)

        if already_tracked is False:
            self._update_stats_after_trade(trade, offer)
            log.info(f"[BALANCING_TRADE] [{self.time_slot_str}] {trade}")

        # TODO: Use non-blockchain non-event-driven version for now for both blockchain and
        # normal runs.
        self._notify_listeners(MarketEvent.BALANCING_TRADE, trade=trade)
        return trade

    def delete_balancing_offer(self, offer_or_id: Union[str, BalancingOffer]):
        if self.readonly:
            raise MarketReadOnlyException()
        if isinstance(offer_or_id, Offer):
            offer_or_id = offer_or_id.id
        offer = self.offers.pop(offer_or_id, None)

        self._update_min_max_avg_offer_prices()
        if not offer:
            raise OfferNotFoundException()
        log.debug(f"[BALANCING_OFFER][DEL][{self.time_slot_str}] {offer}")
        self._notify_listeners(MarketEvent.BALANCING_OFFER_DELETED, offer=offer)

    def _update_accumulated_trade_price_energy(self, trade):
        if trade.offer_bid.energy > 0:
            self.accumulated_supply_balancing_trade_price += trade.offer_bid.price
            self.accumulated_supply_balancing_trade_energy += trade.offer_bid.energy
        elif trade.offer_bid.energy < 0:
            self.accumulated_demand_balancing_trade_price += trade.offer_bid.price
            self.accumulated_demand_balancing_trade_energy += abs(trade.offer_bid.energy)

    @property
    def avg_supply_balancing_trade_rate(self):
        price = self.accumulated_supply_balancing_trade_price
        energy = self.accumulated_supply_balancing_trade_energy
        return round(price / energy, 4) if energy else 0

    @property
    def avg_demand_balancing_trade_rate(self):
        price = self.accumulated_demand_balancing_trade_price
        energy = self.accumulated_demand_balancing_trade_energy
        return round(price / energy, 4) if energy else 0