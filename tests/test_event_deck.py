"""Tests for EventDeckConfig, custom event decks, NEWS, NEWS_BULLETIN,
DRAW_BUILDING_CARD, PATENT_AUCTION, and the redraw cascade mechanic."""

from my_project.models import Resource
from my_project.simulation import (
    EventCard,
    EventDeckConfig,
    EventType,
    GameState,
    NewsCard,
    NewsEffect,
    _ec,
    build_event_deck,
    do_news,
    execute_event,
    execute_event_with_redraws,
)
from my_project.parsing import parse_cards, parse_contracts
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "my_project" / "data"


def _load():
    return parse_cards(DATA / "Cards.csv"), parse_contracts(DATA / "Contracts.csv")


def _count(deck, et):
    return sum(1 for ec in deck if ec.type == et)


# --- EventDeckConfig ---


class TestEventDeckConfig:
    def test_default_config_uses_csv_defaults(self):
        """Default EventDeckConfig draws counts from default_event_counts(num_players)."""
        deck = build_event_deck(3, EventDeckConfig(), num_rounds=1)
        # 3P CSV defaults: 3 news, 2 debt, 1 power, 2 futures, 4 patent (3+1 conditional)
        assert _count(deck, EventType.NEWS_BULLETIN) == 2
        assert _count(deck, EventType.DEBT_COLLECTION) == 2
        assert _count(deck, EventType.POWER_BILL) == 1
        assert _count(deck, EventType.FUTURES_TRADING) == 2
        assert _count(deck, EventType.PATENT_AUCTION) == 4
        assert _count(deck, EventType.END_GAME) == 1

    def test_fixed_counts_override_defaults(self):
        """Fixed int counts override the CSV defaults."""
        cfg = EventDeckConfig(
            power_bill_count=5,
            debt_collection_count=0,
            futures_trading_count=3,
        )
        deck = build_event_deck(3, cfg, num_rounds=1)
        assert _count(deck, EventType.POWER_BILL) == 5
        assert _count(deck, EventType.DEBT_COLLECTION) == 0
        assert _count(deck, EventType.FUTURES_TRADING) == 3

    def test_news_events_inserted(self):
        """When news_pool is provided and news_count > 0, news events appear in the deck."""
        news = EventCard(type=EventType.NEWS, label="FOOD spike", payload={"market_deltas": {"FOOD": 3}})
        cfg = EventDeckConfig(
            news_pool=[news],
            news_count=2,
        )
        deck = build_event_deck(3, cfg, num_rounds=1)
        assert _count(deck, EventType.NEWS) == 2

    def test_news_count_zero_by_default(self):
        """Default config produces no direct-NEWS events even with a non-empty pool."""
        news = EventCard(type=EventType.NEWS, label="test", payload={"market_deltas": {"FE": 1}})
        cfg = EventDeckConfig(news_pool=[news])  # news_count defaults to 0
        deck = build_event_deck(3, cfg, num_rounds=1)
        assert _count(deck, EventType.NEWS) == 0

    def test_no_config_uses_defaults(self):
        """Passing config=None is equivalent to passing EventDeckConfig()."""
        deck = build_event_deck(3, None, num_rounds=1)
        assert _count(deck, EventType.NEWS_BULLETIN) == 2
        assert _count(deck, EventType.PATENT_AUCTION) == 4

    def test_zero_count_disables_event(self):
        """Setting a count to 0 removes that event type from the deck."""
        cfg = EventDeckConfig(
            news_bulletin_count=0,
            patent_auction_count=0,
            draw_building_count=0,
            draw_building_redraw_count=0,
        )
        deck = build_event_deck(3, cfg, num_rounds=1)
        assert _count(deck, EventType.NEWS_BULLETIN) == 0
        assert _count(deck, EventType.PATENT_AUCTION) == 0
        assert _count(deck, EventType.DRAW_BUILDING_CARD) == 0


# --- Custom event deck override ---


class TestCustomEventDeck:
    def test_explicit_deck_overrides_config(self):
        """GameState.create with an explicit event_deck ignores build_event_deck entirely."""
        cards, contracts = _load()
        custom = [_ec(EventType.POWER_BILL)] * 5 + [_ec(EventType.END_GAME)]
        state = GameState.create(cards, contracts, event_deck=custom)
        assert len(state.event_deck) == 6
        assert _count(state.event_deck, EventType.POWER_BILL) == 5
        assert _count(state.event_deck, EventType.END_GAME) == 1

    def test_explicit_deck_with_news(self):
        """Custom deck can contain NEWS events."""
        cards, contracts = _load()
        news = EventCard(type=EventType.NEWS, label="H2O crisis", payload={"market_deltas": {"H2O": 5}})
        custom = [news, _ec(EventType.END_GAME)]
        state = GameState.create(cards, contracts, event_deck=custom)
        assert _count(state.event_deck, EventType.NEWS) == 1


# --- NEWS event execution ---


class TestNewsEvent:
    def test_news_moves_market(self):
        """A news event with market_deltas adjusts the market price."""
        cards, contracts = _load()
        state = GameState.create(cards, contracts)
        player = state.players[0]
        price_before = state.market.price(Resource.FOOD)
        news = EventCard(type=EventType.NEWS, label="FOOD shortage", payload={"market_deltas": {"FOOD": 4}})
        detail = execute_event(state, news, player)
        price_after = state.market.price(Resource.FOOD)
        assert price_after > price_before
        assert "NEWS" in detail
        assert "FOOD" in detail

    def test_news_multiple_resources(self):
        """A news event can move multiple resources at once."""
        cards, contracts = _load()
        state = GameState.create(cards, contracts)
        player = state.players[0]
        food_before = state.market.price(Resource.FOOD)
        h2o_before = state.market.price(Resource.H2O)
        news = EventCard(
            type=EventType.NEWS,
            label="Supply shock",
            payload={"market_deltas": {"FOOD": 3, "H2O": -2}},
        )
        execute_event(state, news, player)
        assert state.market.price(Resource.FOOD) > food_before
        assert state.market.price(Resource.H2O) < h2o_before

    def test_news_no_payload_is_noop(self):
        """A news event with empty/missing payload is harmless."""
        cards, contracts = _load()
        state = GameState.create(cards, contracts)
        player = state.players[0]
        prices_before = {r: state.market.price(r) for r in Resource}
        news = EventCard(type=EventType.NEWS, label="Nothing happened")
        detail = execute_event(state, news, player)
        prices_after = {r: state.market.price(r) for r in Resource}
        assert prices_before == prices_after
        assert "NEWS" in detail

    def test_news_display_label(self):
        """The detail string includes the event's custom label."""
        cards, contracts = _load()
        state = GameState.create(cards, contracts)
        player = state.players[0]
        news = EventCard(type=EventType.NEWS, label="Iron Rush", payload={"market_deltas": {"FE": 2}})
        detail = execute_event(state, news, player)
        assert "Iron Rush" in detail


# --- NEWS_BULLETIN (data-driven via news deck) ---


class TestNewsBulletin:
    def test_rate_all_effect(self):
        """A 'rate_all' news card mutates every player's rates."""
        cards, contracts = _load()
        # Custom news deck with one card that drops everyone's PWR by 1
        news_deck = [NewsCard(name="Test", effects=[NewsEffect("rate_all", {"PWR": -1})])]
        state = GameState.create(cards, contracts, num_players=3, news_deck=news_deck)
        rates_before = [p.rate(Resource.PWR) for p in state.players]
        bulletin = _ec(EventType.NEWS_BULLETIN)
        execute_event(state, bulletin, state.players[0])
        rates_after = [p.rate(Resource.PWR) for p in state.players]
        for before, after in zip(rates_before, rates_after):
            assert after == before - 1

    def test_market_random_effect(self):
        """A 'market_random' news card adjusts the market price by some delta."""
        cards, contracts = _load()
        news_deck = [NewsCard(name="Test", effects=[NewsEffect("market_random", {"resources": ["FE"], "rolls": 1})])]
        state = GameState.create(cards, contracts, news_deck=news_deck)
        price_before = state.market.price(Resource.FE)
        bulletin = _ec(EventType.NEWS_BULLETIN)
        execute_event(state, bulletin, state.players[0])
        # Price should have changed (the d20 distribution includes 0 with weight 1/20,
        # so allow no-change as a rare valid outcome; just check it's not crazy)
        price_after = state.market.price(Resource.FE)
        assert abs(price_after - price_before) <= 6

    def test_trigger_power_bill(self):
        """A 'trigger' news card with event=power_bill fires a power bill."""
        cards, contracts = _load()
        news_deck = [NewsCard(name="Trigger", effects=[NewsEffect("trigger", {"event": "power_bill"})])]
        state = GameState.create(cards, contracts, num_players=3, news_deck=news_deck)
        # Set positive PWR rate for player 0 so the bill should pay them
        state.players[0].rates[Resource.PWR] = 3
        money_before = state.players[0].money
        bulletin = _ec(EventType.NEWS_BULLETIN)
        execute_event(state, bulletin, state.players[0])
        assert state.players[0].money > money_before

    def test_news_deck_consumes_in_order(self):
        """The news deck is consumed sequentially; news_idx advances by 1 per draw."""
        cards, contracts = _load()
        news_deck = [
            NewsCard(name="First"),
            NewsCard(name="Second"),
        ]
        state = GameState.create(cards, contracts, news_deck=news_deck)
        bulletin = _ec(EventType.NEWS_BULLETIN)
        d1 = execute_event(state, bulletin, state.players[0])
        d2 = execute_event(state, bulletin, state.players[0])
        assert "First" in d1
        assert "Second" in d2

    def test_news_deck_reshuffles_when_exhausted(self):
        """When the news deck runs out, it reshuffles and starts over."""
        cards, contracts = _load()
        news_deck = [NewsCard(name="Only")]
        state = GameState.create(cards, contracts, news_deck=news_deck)
        bulletin = _ec(EventType.NEWS_BULLETIN)
        execute_event(state, bulletin, state.players[0])  # consume the only card
        # Drawing again should reshuffle and give the same card
        detail = execute_event(state, bulletin, state.players[0])
        assert "Only" in detail


# --- DRAW_BUILDING_CARD ---


class TestDrawBuildingCard:
    def test_draw_building_replaces_matching_slot_in_place(self):
        """A draw_building_card event replaces an existing pool card with
        the SAME slot as the drawn card, in place. With a controlled
        pool and a stacked deck we can verify the exact slot-based
        eviction behavior and the structured debug data."""
        from my_project.models import Card as _Card
        cards, contracts = _load()
        state = GameState.create(cards, contracts)

        # Build a controlled pool: [slot1, slot2, slot3, slot4] so each
        # slot is represented exactly once.
        def _mk(slot: int, name: str) -> _Card:
            return _Card(
                alternate="H2O/FE", slot=slot, building=name,
                costs=[], rates=[], effect="",
                can_sell=[Resource.H2O], can_fulfill_contract=False,
            )
        state.pool.replace([_mk(1, "S1"), _mk(2, "S2"), _mk(3, "S3"), _mk(4, "S4")])

        # Stack the deck so the next draw is a slot-3 card
        forced = _mk(3, "S3_new")
        state.deck.cards.append(forced)

        execute_event(state, _ec(EventType.DRAW_BUILDING_CARD), state.players[0])

        # The slot-3 card was replaced IN PLACE at index 2
        assert len(state.pool) == 4
        assert state.pool[0].building == "S1"
        assert state.pool[1].building == "S2"
        assert state.pool[2].building == "S3_new"
        assert state.pool[3].building == "S4"
        # Structured data exposes the slot info for the debug log
        data = state._last_event_data
        assert data["card_drawn_slot"] == 3
        assert data["card_replaced_slot"] == 3
        assert data["pool_idx"] == 2

    def test_draw_building_places_at_slot_position(self):
        """A drawn card goes to its slot position. Slot 1 → index 0,
        Slot 2 → index 1, etc. Regardless of what card was previously there."""
        from my_project.models import Card as _Card
        cards, contracts = _load()
        state = GameState.create(cards, contracts)

        def _mk(slot: int, name: str) -> _Card:
            return _Card(
                alternate="H2O/FE", slot=slot, building=name,
                costs=[], rates=[], effect="",
                can_sell=[Resource.H2O], can_fulfill_contract=False,
            )
        # Pool has only slot-1 cards; drawn card is slot 3 → goes to index 2
        state.pool.replace([_mk(1, "A"), _mk(1, "B"), _mk(1, "C"), _mk(1, "D")])
        state.deck.cards.append(_mk(3, "S3"))

        execute_event(state, _ec(EventType.DRAW_BUILDING_CARD), state.players[0])

        # Slot 3 card goes to index 2, replacing the slot-1 card there
        assert state.pool[0].building == "A"
        assert state.pool[1].building == "B"
        assert state.pool[2].building == "S3"  # Slot 3 → index 2
        assert state.pool[3].building == "D"
        data = state._last_event_data
        assert data["pool_idx"] == 2
        assert data["card_replaced_slot"] == 1  # Previous card was slot 1


# --- Redraw cascade ---


class TestRedrawCascade:
    def test_redraw_chains_to_next_card(self):
        """A redraw event fires its own effect and immediately fires the next card."""
        cards, contracts = _load()
        # Custom deck: redraw card followed by a power bill
        custom = [
            _ec(EventType.NO_EVENT, redraws=True),
            _ec(EventType.POWER_BILL),
            _ec(EventType.END_GAME),
        ]
        state = GameState.create(cards, contracts, num_players=3, event_deck=custom)
        state.players[0].rates[Resource.PWR] = 2  # so the bill pays cash
        money_before = state.players[0].money
        # Pull and execute the redraw card
        event = state.event_deck[state.event_idx]
        state.event_idx += 1
        execute_event_with_redraws(state, event, state.players[0])
        # Both events should have fired: power bill paid out
        assert state.players[0].money > money_before
        # And event_idx advanced past the chained card
        assert state.event_idx == 2

    def test_non_redraw_does_not_chain(self):
        """A non-redraw event does NOT advance event_idx beyond its own slot."""
        cards, contracts = _load()
        custom = [
            _ec(EventType.NO_EVENT, redraws=False),
            _ec(EventType.POWER_BILL),
            _ec(EventType.END_GAME),
        ]
        state = GameState.create(cards, contracts, num_players=3, event_deck=custom)
        event = state.event_deck[state.event_idx]
        state.event_idx += 1
        execute_event_with_redraws(state, event, state.players[0])
        assert state.event_idx == 1  # only the no-op consumed

    def test_redraw_chain_absorbs_end_game(self):
        """A redraw chain CAN consume END_GAME. The previous guard that
        stopped the chain before a terminal was wrong: it caused an extra
        player turn when a redraw landed just before END_GAME, unbalancing
        the turn count (seen in a 2-player game where P0 ended up with 11
        turns to P1's 10)."""
        cards, contracts = _load()
        custom = [
            _ec(EventType.NO_EVENT, redraws=True),
            _ec(EventType.END_GAME),
        ]
        state = GameState.create(cards, contracts, num_players=3, event_deck=custom)
        event = state.event_deck[state.event_idx]
        state.event_idx += 1
        detail = execute_event_with_redraws(state, event, state.players[0])
        assert "END GAME" in detail
        # Chain consumed BOTH the redraw and the terminal
        assert state.event_idx == 2

    def test_redraw_chain_absorbs_end_round(self):
        """Same for END_ROUND. The play adapter's _handle_post_event detects
        a consumed terminal via the deck-position invariant (event_idx ==
        len(event_deck) && not is_over) and still triggers reshuffle."""
        cards, contracts = _load()
        custom = [
            _ec(EventType.NO_EVENT, redraws=True),
            _ec(EventType.END_ROUND),
            _ec(EventType.END_GAME),
        ]
        state = GameState.create(cards, contracts, num_players=3, event_deck=custom)
        event = state.event_deck[state.event_idx]
        state.event_idx += 1
        detail = execute_event_with_redraws(state, event, state.players[0])
        assert "END OF ROUND" in detail
        # Chain consumed the redraw and END_ROUND; END_GAME still in deck.
        assert state.event_idx == 2


# --- PATENT_AUCTION (silent auction with bid heuristic) ---


class TestPatentAuction:
    def test_no_patents_is_noop(self):
        """With an empty patent_pile, the auction reports no patents left."""
        cards, contracts = _load()
        state = GameState.create(cards, contracts)
        money_before = [p.money for p in state.players]
        auction = _ec(EventType.PATENT_AUCTION)
        detail = execute_event(state, auction, state.players[0])
        assert "no patents" in detail.lower()
        assert [p.money for p in state.players] == money_before
