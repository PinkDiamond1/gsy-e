import string
from collections import defaultdict

import pytest
from hypothesis import strategies as st
from hypothesis.control import assume
from hypothesis.stateful import Bundle, RuleBasedStateMachine, precondition, rule

from d3a.exceptions import MarketReadOnlyException, OfferNotFoundException, InvalidOffer
from d3a.models.market import Market


@pytest.yield_fixture
def market():
    return Market()


def test_market_offer(market: Market):
    offer = market.offer(20, 10, 'someone')

    assert market.offers[offer.id] == offer
    assert offer.energy == 20
    assert offer.price == 10
    assert offer.seller == 'someone'
    assert len(offer.id) == 36


def test_market_offer_invalid(market: Market):
    with pytest.raises(InvalidOffer):
        market.offer(-1, 10, 'someone')


def test_market_offer_readonly(market: Market):
    market.readonly = True
    with pytest.raises(MarketReadOnlyException):
        market.offer(10, 10, 'A')


def test_market_offer_delete(market: Market):
    offer = market.offer(20, 10, 'someone')
    market.delete_offer(offer)

    assert offer.id not in market.offers


def test_market_offer_delete_id(market: Market):
    offer = market.offer(20, 10, 'someone')
    market.delete_offer(offer.id)

    assert offer.id not in market.offers


def test_market_offer_delete_missing(market: Market):
    with pytest.raises(OfferNotFoundException):
        market.delete_offer("no such offer")


def test_market_offer_delete_readonly(market: Market):
    market.readonly = True
    with pytest.raises(MarketReadOnlyException):
        market.delete_offer("no such offer")


def test_market_trade(market: Market):
    offer = market.offer(20, 10, 'A')

    trade = market.accept_offer(offer, 'B')
    assert trade
    assert trade == market.trades[0]
    assert trade.offer is offer
    assert trade.seller == 'A'
    assert trade.buyer == 'B'


def test_market_trade_by_id(market: Market):
    offer = market.offer(20, 10, 'A')

    trade = market.accept_offer(offer.id, 'B')
    assert trade


def test_market_trade_readonly(market: Market):
    offer = market.offer(20, 10, 'A')
    market.readonly = True
    with pytest.raises(MarketReadOnlyException):
        market.accept_offer(offer, 'B')


def test_market_trade_not_found(market: Market):
    offer = market.offer(20, 10, 'A')

    assert market.accept_offer(offer, 'B')
    with pytest.raises(OfferNotFoundException):
        market.accept_offer(offer, 'B')


def test_market_acct_simple(market: Market):
    offer = market.offer(20, 10, 'A')
    market.accept_offer(offer, 'B')

    assert market.accounting['A'] == -offer.energy
    assert market.accounting['B'] == offer.energy


def test_market_acct_multiple(market: Market):
    offer1 = market.offer(20, 10, 'A')
    offer2 = market.offer(10, 10, 'A')
    market.accept_offer(offer1, 'B')
    market.accept_offer(offer2, 'C')

    assert market.accounting['A'] == -offer1.energy + -offer2.energy == -30
    assert market.accounting['B'] == offer1.energy == 20
    assert market.accounting['C'] == offer2.energy == 10


@pytest.mark.parametrize(
    ('last_offer_size', 'accounting'),
    (
        (20, -10),
        (30, 0),
        (40, 10)
    )
)
def test_market_issuance_acct_reverse(market: Market, last_offer_size, accounting):
    offer1 = market.offer(20, 10, 'A')
    offer2 = market.offer(10, 10, 'A')
    offer3 = market.offer(last_offer_size, 10, 'D')

    market.accept_offer(offer1, 'B')
    market.accept_offer(offer2, 'C')
    market.accept_offer(offer3, 'A')

    assert market.accounting['A'] == accounting


def test_market_iou(market: Market):
    offer = market.offer(20, 10, 'A')
    market.accept_offer(offer, 'B')

    assert market.ious['B']['A'] == 10


class MarketStateMachine(RuleBasedStateMachine):
    offers = Bundle('Offers')
    actors = Bundle('Actors')

    def __init__(self):
        super().__init__()
        self.market = Market()

    @rule(target=actors, actor=st.text(min_size=1, max_size=3,
                                       alphabet=string.ascii_letters + string.digits))
    def new_actor(self, actor):
        return actor

    @rule(target=offers, seller=actors, energy=st.integers(min_value=1), price=st.integers())
    def offer(self, seller, energy, price):
        return self.market.offer(energy, price, seller)

    @rule(offer=offers, buyer=actors)
    def trade(self, offer, buyer):
        assume(offer.id in self.market.offers)
        self.market.accept_offer(offer, buyer)

    @precondition(lambda self: self.market.accounting)
    @rule()
    def check_acct(self):
        actor_sums = defaultdict(int)
        for t in self.market.trades:
            actor_sums[t.seller] -= t.offer.energy
            actor_sums[t.buyer] += t.offer.energy
        for actor, sum_ in actor_sums.items():
            assert self.market.accounting[actor] == sum_
        assert sum(self.market.accounting.values()) == 0

    @precondition(lambda self: self.market.accounting)
    @rule()
    def check_iou_balance(self):
        seller_ious = defaultdict(int)
        buyer_ious = defaultdict(int)
        for t in self.market.trades:
            seller_ious[t.seller] += t.offer.price
            buyer_ious[t.buyer] += t.offer.price
        trade_sum = sum(t.offer.price for t in self.market.trades)

        for seller, iou in seller_ious.items():
            assert iou == sum(ious[seller] for ious in self.market.ious.values())

        for buyer, iou in buyer_ious.items():
            assert iou == sum(self.market.ious[buyer].values())

        assert trade_sum == sum(sum(i.values()) for i in self.market.ious.values())


TestMarketIOU = MarketStateMachine.TestCase