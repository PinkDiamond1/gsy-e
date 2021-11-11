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
from collections import namedtuple
from typing import Dict, Set  # noqa
from gsy_e.constants import FLOATING_POINT_TOLERANCE
from gsy_framework.constants_limits import ConstSettings
from gsy_e.gsy_e_core.util import short_offer_bid_log_str
from gsy_e.gsy_e_core.exceptions import MarketException, OfferNotFoundException
from gsy_framework.data_classes import Offer
from gsy_framework.enums import SpotMarketTypeEnum


OfferInfo = namedtuple('OfferInfo', ('source_offer', 'target_offer'))
Markets = namedtuple('Markets', ('source', 'target'))
ResidualInfo = namedtuple('ResidualInfo', ('forwarded', 'age'))


class IAAEngine:
    def __init__(self, name: str, market_1, market_2, min_offer_age: int,
                 owner):
        self.name = name
        self.markets = Markets(market_1, market_2)
        self.min_offer_age = min_offer_age
        self.owner = owner

        self.offer_age = {}  # type: Dict[str, int]
        # Offer.id -> OfferInfo
        self.forwarded_offers = {}  # type: Dict[str, OfferInfo]
        self.trade_residual = {}  # type Dict[str, Offer]
        self.ignored_offers = set()  # type: Set[str]

    def __repr__(self):
        return "<IAAEngine [{s.owner.name}] {s.name} {s.markets.source.time_slot:%H:%M}>".format(
            s=self
        )

    def _offer_in_market(self, offer):
        updated_price = self.markets.target.fee_class.update_forwarded_offer_with_fee(
            offer.energy_rate, offer.original_price / offer.energy) * offer.energy

        kwargs = {
            "price": updated_price,
            "energy": offer.energy,
            "seller": self.owner.name,
            "original_price": offer.original_price,
            "dispatch_event": False,
            "seller_origin": offer.seller_origin,
            "seller_origin_id": offer.seller_origin_id,
            "seller_id": self.owner.uuid
        }

        return self.owner.post_offer(market=self.markets.target, replace_existing=False, **kwargs)

    def _forward_offer(self, offer):
        # TODO: This is an ugly solution. After the december release this check needs to
        #  implemented after grid fee being incorporated while forwarding in target market
        if offer.price < 0.0:
            self.owner.log.debug("Offer is not forwarded because price < 0")
            return
        try:
            forwarded_offer = self._offer_in_market(offer)
        except MarketException:
            self.owner.log.debug("Offer is not forwarded because grid fees of the target market "
                                 "lead to a negative offer price.")
            return

        self._add_to_forward_offers(offer, forwarded_offer)
        self.owner.log.trace(f"Forwarding offer {offer} to {forwarded_offer}")
        # TODO: Ugly solution, required in order to decouple offer placement from
        # new offer event triggering
        self.markets.target.dispatch_market_offer_event(forwarded_offer)
        return forwarded_offer

    def _delete_forwarded_offer_entries(self, offer):
        offer_info = self.forwarded_offers.pop(offer.id, None)
        if not offer_info:
            return
        self.forwarded_offers.pop(offer_info.target_offer.id, None)
        self.forwarded_offers.pop(offer_info.source_offer.id, None)

    def tick(self, *, area):
        self.propagate_offer(area.current_tick)

    def propagate_offer(self, current_tick):
        # Store age of offer
        for offer in self.markets.source.offers.values():
            if offer.id not in self.offer_age:
                self.offer_age[offer.id] = current_tick

        # Use `list()` to avoid in place modification errors
        for offer_id, age in list(self.offer_age.items()):
            if offer_id in self.forwarded_offers:
                continue
            if current_tick - age < self.min_offer_age:
                continue
            offer = self.markets.source.offers.get(offer_id)
            if not offer:
                # Offer has gone - remove from age dict
                # Because an offer forwarding might trigger a trade event, the offer_age dict might
                # be modified, thus causing a removal from the offer_age dict. In such a case, even
                # if the offer is no longer in the offer_age dict, the execution should continue
                # normally.
                self.offer_age.pop(offer_id, None)
                continue
            if not self.owner.usable_offer(offer):
                # Forbidden offer (i.e. our counterpart's)
                self.offer_age.pop(offer_id, None)
                continue

            # Should never reach this point.
            # This means that the IAA is forwarding offers with the same seller and buyer name.
            # If we ever again reach a situation like this, we should never forward the offer.
            if self.owner.name == offer.seller:
                self.offer_age.pop(offer_id, None)
                continue

            forwarded_offer = self._forward_offer(offer)
            if forwarded_offer:
                self.owner.log.debug(f"Forwarded offer to {self.markets.source.name} "
                                     f"{self.owner.name}, {self.name} {forwarded_offer}")

    def event_offer_traded(self, *, trade):
        offer_info = self.forwarded_offers.get(trade.offer_bid.id)
        if not offer_info:
            # Trade doesn't concern us
            return

        if trade.offer_bid.id == offer_info.target_offer.id:
            # Offer was accepted in target market - buy in source
            source_rate = offer_info.source_offer.energy_rate
            target_rate = offer_info.target_offer.energy_rate
            assert abs(source_rate) <= abs(target_rate) + FLOATING_POINT_TOLERANCE, \
                f"offer: source_rate ({source_rate}) is not lower than target_rate ({target_rate})"

            try:
                if ConstSettings.IAASettings.MARKET_TYPE == SpotMarketTypeEnum.ONE_SIDED.value:
                    # One sided market should subtract the fees
                    trade_offer_rate = trade.offer_bid.energy_rate - \
                                       trade.fee_price / trade.offer_bid.energy
                else:
                    # trade_offer_rate not used in two sided markets, trade_bid_info used instead
                    trade_offer_rate = None
                updated_trade_bid_info = \
                    self.markets.source.fee_class.update_forwarded_offer_trade_original_info(
                        trade.offer_bid_trade_info, offer_info.source_offer)

                trade_source = self.owner.accept_offer(
                    market=self.markets.source,
                    offer=offer_info.source_offer,
                    energy=trade.offer_bid.energy,
                    buyer=self.owner.name,
                    trade_rate=trade_offer_rate,
                    trade_bid_info=updated_trade_bid_info,
                    buyer_origin=trade.buyer_origin,
                    buyer_origin_id=trade.buyer_origin_id,
                    buyer_id=self.owner.uuid
                )

            except OfferNotFoundException:
                raise OfferNotFoundException()
            self.owner.log.debug(
                f"[{self.markets.source.time_slot_str}] Offer accepted {trade_source}")

            self._delete_forwarded_offer_entries(offer_info.source_offer)
            self.offer_age.pop(offer_info.source_offer.id, None)

        elif trade.offer_bid.id == offer_info.source_offer.id:
            # Offer was bought in source market by another party
            try:
                self.owner.delete_offer(self.markets.target, offer_info.target_offer)
            except OfferNotFoundException:
                pass
            except MarketException as ex:
                self.owner.log.error("Error deleting InterAreaAgent offer: {}".format(ex))

            self._delete_forwarded_offer_entries(offer_info.source_offer)
            self.offer_age.pop(offer_info.source_offer.id, None)
        else:
            raise RuntimeError("Unknown state. Can't happen")

        assert offer_info.source_offer.id not in self.forwarded_offers
        assert offer_info.target_offer.id not in self.forwarded_offers

    def event_offer_deleted(self, *, offer):
        if offer.id in self.offer_age:
            # Offer we're watching in source market was deleted - remove
            del self.offer_age[offer.id]

        offer_info = self.forwarded_offers.get(offer.id)
        if not offer_info:
            # Deletion doesn't concern us
            return

        if offer_info.source_offer.id == offer.id:
            # Offer in source market of an offer we're already offering in the target market
            # was deleted - also delete in target market
            try:
                self.owner.delete_offer(self.markets.target, offer_info.target_offer)
                self._delete_forwarded_offer_entries(offer_info.source_offer)
            except MarketException:
                self.owner.log.exception("Error deleting InterAreaAgent offer")
        # TODO: Should potentially handle the flip side, by not deleting the source market offer
        # but by deleting the offered_offers entries

    def event_offer_split(self, *, market_id, original_offer, accepted_offer, residual_offer):
        market = self.owner._get_market_from_market_id(market_id)
        if market is None:
            return

        if market == self.markets.target and accepted_offer.id in self.forwarded_offers:
            # offer was split in target market, also split in source market

            local_offer = self.forwarded_offers[original_offer.id].source_offer
            original_price = local_offer.original_price \
                if local_offer.original_price is not None else local_offer.price

            local_split_offer, local_residual_offer = \
                self.markets.source.split_offer(local_offer, accepted_offer.energy,
                                                original_price)

            #  add the new offers to forwarded_offers
            self._add_to_forward_offers(local_residual_offer, residual_offer)
            self._add_to_forward_offers(local_split_offer, accepted_offer)

        elif market == self.markets.source and accepted_offer.id in self.forwarded_offers:
            # offer was split in source market, also split in target market
            if not self.owner.usable_offer(accepted_offer) or \
                    self.owner.name == accepted_offer.seller:
                return

            local_offer = self.forwarded_offers[original_offer.id].source_offer

            original_price = local_offer.original_price \
                if local_offer.original_price is not None else local_offer.price

            local_split_offer, local_residual_offer = \
                self.markets.target.split_offer(local_offer, accepted_offer.energy,
                                                original_price)

            #  add the new offers to forwarded_offers
            self._add_to_forward_offers(residual_offer, local_residual_offer)
            self._add_to_forward_offers(accepted_offer, local_split_offer)

        else:
            return

        if original_offer.id in self.offer_age:
            self.offer_age[residual_offer.id] = self.offer_age.pop(original_offer.id)

        self.owner.log.debug(f"Offer {short_offer_bid_log_str(local_offer)} was split into "
                             f"{short_offer_bid_log_str(local_split_offer)} and "
                             f"{short_offer_bid_log_str(local_residual_offer)}")

    def _add_to_forward_offers(self, source_offer, target_offer):
        offer_info = OfferInfo(Offer.copy(source_offer), Offer.copy(target_offer))
        self.forwarded_offers[source_offer.id] = offer_info
        self.forwarded_offers[target_offer.id] = offer_info


class BalancingEngine(IAAEngine):

    def _forward_offer(self, offer):
        forwarded_balancing_offer = self.markets.target.balancing_offer(
            offer.price,
            offer.energy,
            self.owner.name,
            from_agent=True
        )
        self._add_to_forward_offers(offer, forwarded_balancing_offer)
        self.owner.log.trace(f"Forwarding balancing offer {offer} to {forwarded_balancing_offer}")
        return forwarded_balancing_offer