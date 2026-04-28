"""Event execution for the board game engine.

This module owns the per-event handlers (``do_*``), the dispatch
function (``execute_event``), the redraw-chain orchestration
(``execute_event_with_redraws``), and the pause/resume machinery for
human prompts (``_event_needs_prompt``, ``resume_pending_event``).

Event *data types* (``EventCard``, ``EventType``, ``NewsCard``,
``NewsEffect``, ``EventDeckConfig``) and deck construction
(``build_event_deck``, ``default_event_counts``) live in
``simulation.py`` since they're also used by ``GameState`` construction
and the Monte Carlo loop. This module imports those types and adds the
behavior on top.

The dependency direction is strictly ``events -> simulation``. ``simulation``
re-imports the names from this module at the *bottom* of its module body
(after all its classes are defined) so test files and ``play_adapter``
that historically imported from ``my_project.simulation`` keep working.
"""

from __future__ import annotations

import random
from pathlib import Path

from my_project.models import Card, Resource
from my_project.simulation import (
    GameState,
    Player,
    EventCard,
    EventType,
    NewsCard,
    NewsEffect,
    DEBT_INTEREST_DIVISOR,
    _D20_DELTAS,
    _pleasure_dome_bonus,
    _player_owns_patent,
    _apply_patent_acquisition,
)


def _apply_debt(player: Player, amount: int) -> tuple[int, int]:
    """Charge `amount` debt against a player, consuming credit first.

    Credit absorbs debt before it becomes real debt. So a player with
    $30 credit and a $50 debt charge ends up with $0 credit and $20 debt.

    Returns a tuple `(credit_consumed, real_debt_added)` for callers that
    want to log the breakdown. Both are >= 0 and sum to `amount`.

    Negative or zero `amount` is a no-op.
    """
    if amount <= 0:
        return (0, 0)
    credit_consumed = min(player.credit, amount)
    player.credit -= credit_consumed
    real_debt_added = amount - credit_consumed
    player.debt += real_debt_added
    return (credit_consumed, real_debt_added)


def _record_event_line(
    state: "GameState",
    *,
    kind: str,
    text: str,
    player: "Player | None" = None,
) -> None:
    """Append a single line to the current event's detail log.

    `kind` is one of "header", "note", "player". For "player" lines, pass the
    affected `player` so the line snapshots their post-action NW components.
    The play adapter copies `state.last_event_lines` after every event resolves
    and surfaces it to the UI.
    """
    line: dict = {"kind": kind, "text": text}
    if player is not None:
        line["player_idx"] = state.players.index(player)
        line["name"] = player.name
        line["money_after"] = player.money
        line["debt_after"] = player.debt
        line["credit_after"] = player.credit
        line["net_worth_after"] = player.net_worth()
    state.last_event_lines.append(line)


def do_pwr_adjust(state: GameState, player: Player) -> None:
    """Adjust PWR market price based on active player's rate.

    Positive PWR shifts price down (like selling), negative shifts up.
    """
    pwr_rate = player.rate(Resource.PWR)
    price_before = state.market.price(Resource.PWR)
    if pwr_rate > 0:
        state.market.adjust(Resource.PWR, -pwr_rate)
    elif pwr_rate < 0:
        state.market.adjust(Resource.PWR, abs(pwr_rate))
    price_after = state.market.price(Resource.PWR)
    if pwr_rate != 0:
        direction = "↓" if pwr_rate > 0 else "↑"
        _record_event_line(
            state,
            kind="header",
            text=f"PWR Adjust — {player.name} has {pwr_rate:+d} PWR",
        )
        _record_event_line(
            state,
            kind="note",
            text=f"PWR price: ${price_before} → ${price_after} ({direction}{abs(pwr_rate)})",
        )


def do_power_bill(state: GameState) -> None:
    """Power bill event: positive PWR earns money, negative PWR adds debt.

    Pleasure Dome owners also get a flat per-dome bonus on every power bill.

    Energy Vault: if the player has a NEGATIVE PWR rate and vault > 0,
    the vault shields them from paying this power bill entirely (they
    neither earn nor pay). The vault decrements by 1 per shielded bill.
    If the player has a positive or zero rate, the vault has no effect.
    """
    pwr_price = state.market.price(Resource.PWR)
    _record_event_line(
        state, kind="header", text=f"Power Bill — PWR @ ${pwr_price}"
    )
    per_player_bill: list[dict] = []
    for player in state.players:
        base_rate = player.rate(Resource.PWR)
        vault = player.patent_state.get("energy_vault", 0)

        # Build the per-player text for the event log row.
        bill_parts: list[str] = []
        player_bill: dict = {"name": player.name, "rate": base_rate}

        # Energy Vault: shields from negative power bills
        if base_rate < 0 and vault > 0:
            player.patent_state["energy_vault"] = vault - 1
            bill_parts.append(
                f"Energy Vault shields bill ({vault - 1} uses left)"
            )
            player_bill["shielded"] = True
        elif base_rate > 0:
            earning = base_rate * pwr_price
            player.money += earning
            state.pwr_total_earned += earning
            state.bills_units_earned[Resource.PWR] += base_rate
            player.flow_sold_units[Resource.PWR] += base_rate
            player.flow_sell_revenue[Resource.PWR] += earning
            bill_parts.append(
                f"+${earning} (sold {base_rate} PWR)"
            )
            player_bill["earning"] = earning
        elif base_rate < 0:
            shortage = abs(base_rate)
            cost = shortage * pwr_price
            _apply_debt(player, cost)
            state.pwr_total_debt += cost
            state.bills_units_owed[Resource.PWR] += shortage
            player.ledger.record_event_cost(Resource.PWR, cost, base_rate)
            player.flow_bought_units[Resource.PWR] += shortage
            player.flow_buy_cost[Resource.PWR] += cost
            bill_parts.append(
                f"−${cost} (bought {shortage} PWR)"
            )
            player_bill["debt"] = cost
        # Pleasure Dome bonus is added on top of normal bill processing.
        bonus = _pleasure_dome_bonus(state, player)
        if bonus > 0:
            player.money += bonus
            bill_parts.append(f"+${bonus} dome bonus")
            player_bill["dome_bonus"] = bonus
        per_player_bill.append(player_bill)
        if bill_parts:
            _record_event_line(
                state,
                kind="player",
                player=player,
                text=" · ".join(bill_parts),
            )
    state.log.annotate("pwr_price", pwr_price)
    state.log.annotate("per_player", per_player_bill)


def _ai_debt_paydown(state: GameState, player: Player) -> int:
    """AI heuristic: pay down debt with cash, keeping a reserve for builds.

    Paydowns are in $10 increments (rounded down).
    """
    reserve = 10  # minimum cash to keep for buying resources
    available = max(0, player.money - reserve)
    paydown = min(player.debt, available)
    return (paydown // 10) * 10  # round down to $10 increments


def do_debt_collection(state: GameState) -> None:
    """Increase debt by $1 per DEBT_INTEREST_DIVISOR owed.

    The contract "shield" from the original rules is implicit: contract
    rewards now pay off debt directly (and any leftover becomes credit
    that absorbs future debt before it hits the player's actual debt).

    Financial Instruments hook: each owner of FI gains cash equal to the
    sum of REAL debt added to OTHER players (not themselves) this round.
    Credit-absorbed amounts don't count for the FI payout.
    """
    _record_event_line(state, kind="header", text="Debt Collection")

    # Phase 1: pay down debt with cash before interest accrues.
    # Human paydowns come from pending_debt_paydowns (set by prompt);
    # AI paydowns use the heuristic.
    for idx, player in enumerate(state.players):
        if player.debt <= 0 or player.money <= 0:
            continue
        if idx in state.pending_debt_paydowns:
            raw = min(state.pending_debt_paydowns[idx], player.debt, player.money)
            paydown = (raw // 10) * 10  # round down to $10 increments
        else:
            paydown = _ai_debt_paydown(state, player)
        if paydown > 0:
            player.money -= paydown
            player.debt -= paydown
            _record_event_line(
                state, kind="player", player=player,
                text=f"Paid down ${paydown} debt → ${player.debt} remaining, ${player.money} cash",
            )
    state.pending_debt_paydowns.clear()

    # Phase 2: interest charges.
    debt_added: dict[int, int] = {}
    for idx, player in enumerate(state.players):
        interest = player.debt // DEBT_INTEREST_DIVISOR
        if interest <= 0:
            continue
        # Charge the interest through _apply_debt so any credit absorbs it
        credit_used, real_debt = _apply_debt(player, interest)
        debt_added[idx] = real_debt
        if real_debt > 0 and credit_used > 0:
            text = (
                f"+${interest} interest "
                f"(${credit_used} absorbed by credit, ${real_debt} → debt ${player.debt})"
            )
        elif real_debt > 0:
            text = f"+${interest} interest → debt ${player.debt}"
        else:
            text = f"+${interest} interest absorbed by credit (${player.credit} left)"
        _record_event_line(state, kind="player", player=player, text=text)

    # Financial Instruments payout: for each owner, gain cash equal to the
    # sum of debt added to OTHER players this round.
    for idx, player in enumerate(state.players):
        if not _player_owns_patent(player, "Financial Instruments"):
            continue
        bonus = sum(amt for other_idx, amt in debt_added.items() if other_idx != idx)
        if bonus > 0:
            player.money += bonus
            _record_event_line(
                state,
                kind="player",
                player=player,
                text=f"+${bonus} Financial Instruments payout",
            )

    # Annotate debt collection metadata
    per_player_dc: list[dict] = []
    for idx, player in enumerate(state.players):
        entry: dict = {"name": player.name}
        interest = (player.debt + debt_added.get(idx, 0)) // DEBT_INTEREST_DIVISOR
        if interest > 0:
            entry["interest"] = interest
        if debt_added.get(idx, 0) > 0:
            entry["debt_added"] = debt_added[idx]
        per_player_dc.append(entry)
    state.log.annotate("per_player", per_player_dc)


def do_futures_trading(state: GameState) -> None:
    """Negative rates push market prices up, but no debt is charged.

    This fires on FUTURES_TRADING event cards. The market rises by the
    total negative rates across all players for each non-PWR resource.
    """
    _record_event_line(state, kind="header", text="Futures Trading")

    # Track per-player contributions to negative rates
    player_contributions: list[dict] = []
    total_negatives: dict[Resource, int] = {
        r: 0 for r in Resource if r != Resource.PWR
    }
    for player in state.players:
        contrib: dict[str, int] = {}
        for r in total_negatives:
            rate = player.rate(r)
            if rate < 0:
                total_negatives[r] += abs(rate)
                contrib[r.value] = rate
        if contrib:
            player_contributions.append({"name": player.name, "rates": contrib})

    # Apply market adjustments and track price changes
    market_changes: list[dict] = []
    for r, total in total_negatives.items():
        if total > 0:
            price_before = state.market.price(r)
            state.market.adjust(r, total)
            price_after = state.market.price(r)
            market_changes.append({
                "resource": r.value,
                "units": total,
                "price_before": price_before,
                "price_after": price_after,
            })
            _record_event_line(
                state, kind="note",
                text=f"{r.value} +{total} units → ${price_before}→${price_after}",
            )

    # Store structured data for the frontend
    state._futures_trading_data = {
        "market_changes": market_changes,
        "player_contributions": player_contributions,
    }
    state.log.annotate("market_changes", market_changes)
    state.log.annotate("player_contributions", player_contributions)


def do_futures_settlement(state: GameState) -> None:
    """Players with negative non-PWR rates pay debt at current market price.

    Fires at END_ROUND and END_GAME only. Does NOT move market prices —
    price movement happens only during do_futures_trading() mid-round.
    """
    _record_event_line(state, kind="header", text="Futures Settlement")
    starting_prices = {r: state.market.price(r) for r in Resource if r != Resource.PWR}

    for player in state.players:
        per_resource_parts: list[str] = []
        total_cost = 0
        for r in starting_prices:
            rate = player.rate(r)
            if rate < 0:
                shortage = abs(rate)
                cost = starting_prices[r] * shortage
                _apply_debt(player, cost)
                state.futures_total_debt += cost
                state.futures_units_bought[r] += shortage
                state.futures_debt_per_resource[r] += cost
                player.ledger.record_event_cost(r, cost, rate)
                # Per-player futures tracking
                player.flow_futures_units[r] += shortage
                player.flow_futures_cost[r] += cost
                per_resource_parts.append(
                    f"{shortage} {r.value} @ ${starting_prices[r]} = ${cost}"
                )
                total_cost += cost
        if per_resource_parts:
            _record_event_line(
                state,
                kind="player",
                player=player,
                text=f"−${total_cost} (settled " + ", ".join(per_resource_parts) + ")",
            )


def do_news(state: GameState, event: EventCard) -> str:
    """Execute a news event: adjust market prices per the payload.

    Payload shape:
        {"market_deltas": {"FOOD": 4, "H2O": -2, ...}}

    Each entry moves the named resource's market position by the given delta
    (positive = price rises, negative = price drops). Uses the same
    Market.adjust that PWR_ADJUST and futures settlements use.
    """
    payload = event.payload or {}
    deltas = payload.get("market_deltas", {})
    parts = []
    for r_str, delta in deltas.items():
        resource = Resource(r_str)
        state.market.adjust(resource, delta)
        sign = "+" if delta >= 0 else ""
        parts.append(f"{r_str} {sign}{delta}")
    label = event.display_label()
    detail = ", ".join(parts) if parts else "no effect"
    _record_event_line(state, kind="header", text=f"NEWS: {label}")
    if parts:
        _record_event_line(state, kind="note", text=detail)
    return f"NEWS: {label} ({detail})"


def _draw_news_card(state: GameState) -> NewsCard | None:
    """Draw the next card from the news deck. Reshuffle if exhausted."""
    if not state.news_deck:
        return None
    if state.news_idx >= len(state.news_deck):
        random.shuffle(state.news_deck)
        state.news_idx = 0
    card = state.news_deck[state.news_idx]
    state.news_idx += 1
    return card


def _apply_news_effect(state: GameState, effect: NewsEffect, active_player: Player) -> str:
    """Apply a single news effect, returning a short detail string."""
    if effect.kind == "rate_all":
        # Apply rate deltas to every player in the game.
        parts = []
        for r_str, delta in effect.payload.items():
            resource = Resource(r_str)
            for p in state.players:
                p.rates[resource] = p.rate(resource) + delta
            sign = "+" if delta >= 0 else ""
            parts.append(f"All {sign}{delta} {r_str}")
        text = ", ".join(parts) or "no rate change"
        _record_event_line(state, kind="note", text=text)
        return text

    if effect.kind == "market_random":
        resources = effect.payload.get("resources", [])
        rolls = int(effect.payload.get("rolls", 1))
        parts = []
        for r_str in resources:
            resource = Resource(r_str)
            for _ in range(rolls):
                delta = random.choice(_D20_DELTAS)
                state.market.adjust(resource, delta)
                sign = "+" if delta >= 0 else ""
                parts.append(f"{r_str} {sign}{delta}")
        text = ", ".join(parts) or "no market change"
        _record_event_line(state, kind="note", text=text)
        return text

    if effect.kind == "trigger":
        which = effect.payload.get("event")
        if which == "power_bill":
            do_power_bill(state)
            return "→ power bill"
        if which == "debt_collection":
            do_debt_collection(state)
            return "→ debt collection"
        if which == "futures_trading":
            do_futures_trading(state)
            return "→ futures trading"
        if which == "futures_settlement":
            do_futures_settlement(state)
            return "→ futures settlement"
        return f"unknown trigger: {which}"

    return f"unknown effect: {effect.kind}"


def do_news_bulletin(state: GameState, active_player: Player) -> str:
    """Draw the top card from the news deck and apply all of its effects."""
    card = _draw_news_card(state)
    if card is None:
        _record_event_line(state, kind="header", text="News Bulletin (deck empty)")
        return "news bulletin (deck empty)"
    if not card.effects:
        _record_event_line(state, kind="header", text=f"NEWS: {card.name} (All Quiet)")
        return f"NEWS: {card.name} (All Quiet)"
    _record_event_line(state, kind="header", text=f"NEWS: {card.name}")
    detail_parts = [_apply_news_effect(state, eff, active_player) for eff in card.effects]
    return f"NEWS: {card.name} ({'; '.join(detail_parts)})"


def do_draw_building_card(state: GameState) -> str:
    """Refresh the pool by drawing a fresh card from the building deck.

    Pool size stays at POOL_SIZE. Each card slot (1-4) maps to a fixed pool
    position (indices 0-3): slot 1 → index 0, slot 2 → index 1, etc.
    A drawn card replaces whatever card is currently at its slot position.
    The drawn slot and the replacement index are recorded in the structured
    event data for the debug log.
    """
    if not state.deck.cards and not state.deck.discard:
        _record_event_line(state, kind="header", text="Draw Building Card (deck empty)")
        return "draw building card (deck empty)"
    drawn = state.deck.draw(1)
    if not drawn:
        _record_event_line(state, kind="header", text="Draw Building Card (deck empty)")
        return "draw building card (deck empty)"
    new_card = drawn[0]
    evicted_name = None
    evicted_slot: int | None = None
    replaced_pool_idx: int | None = None
    if state.pool:
        # Card slot (1-4) maps directly to pool position (0-3).
        # A slot-2 card always goes to pool index 1, replacing whatever is there.
        evict_idx = new_card.slot - 1
        if evict_idx < len(state.pool):
            evicted = state.pool[evict_idx]
            evicted_name = evicted.building
            evicted_slot = evicted.slot
            replaced_pool_idx = evict_idx
            # Swap in place so the pool slot order stays stable. CardZone
            # logs this as a single-index mutation, matching the event's
            # actual semantics better than pop+append.
            state.pool[evict_idx] = new_card
            state.deck.discard.append(evicted)
        else:
            # Pool smaller than slot number (shouldn't happen in normal play)
            state.pool.append(new_card)
    else:
        state.pool.append(new_card)
    evict_text = (
        f" (replaced {evicted_name} @slot {evicted_slot})"
        if evicted_name is not None
        else ""
    )
    _record_event_line(
        state, kind="header",
        text=f"Drew {new_card.building} (slot {new_card.slot}) into pool{evict_text}",
    )
    # Debug line so the mutation log makes the slot mapping obvious.
    if replaced_pool_idx is not None:
        _record_event_line(
            state, kind="detail",
            text=(
                f"  slot {new_card.slot} → pool[{replaced_pool_idx}] "
                f"(evicted slot {evicted_slot})"
            ),
        )
    # Store structured data
    state._draw_card_data = {
        "card_drawn": new_card.building,
        "card_drawn_slot": new_card.slot,
        "card_replaced": evicted_name,
        "card_replaced_slot": evicted_slot,
        "pool_idx": replaced_pool_idx,
        "rates": [{
            "resource": ra.resource.value,
            "amount": ra.amount,
        } for ra in new_card.rates],
        "costs": [{
            "resource": ra.resource.value,
            "amount": ra.amount,
        } for ra in new_card.costs],
        "effect": new_card.effect or None,
    }
    return f"draw building card → {new_card.building}{evict_text}"


def _draw_patent(state: GameState) -> Card | None:
    """Draw the next patent off the pile, or return None if exhausted."""
    if state.patent_idx >= len(state.patent_pile):
        return None
    patent = state.patent_pile[state.patent_idx]
    state.patent_idx += 1
    return patent


def settle_silent_auction(
    state: GameState,
    patent: Card,
    bids: dict[int, int],
    active_player: Player,
) -> tuple[int, int] | None:
    """Resolve a silent auction given a {player_idx: bid} map.

    Returns (winner_idx, amount_paid) or None if no one bid above 0.

    Rules:
      - Highest bidder wins
      - Ties broken by turn order RELATIVE TO the player who drew the
        event (`active_player`): they win first, then the next player
        clockwise in seat order, and so on. This matches the table-top
        intuition — you trigger the auction, so you get the edge.
      - Winner pays runner_up + $5, UNLESS they tied (same bid as
        runner-up), in which case they pay their own bid exactly
      - If only one player bids, they pay $5 (since runner_up = 0)
    """
    positive_bids = {idx: amt for idx, amt in bids.items() if amt > 0}
    if not positive_bids:
        return None

    # Tie-break: rank seats by distance from the event drawer's seat.
    num_players = len(state.players)
    initiator_idx = state.players.index(active_player)
    def _turn_rank(seat_idx: int) -> int:
        return (seat_idx - initiator_idx) % num_players

    sorted_bidders = sorted(
        positive_bids.items(),
        key=lambda kv: (-kv[1], _turn_rank(kv[0])),
    )
    winner_idx, winner_bid = sorted_bidders[0]
    runner_up_bid = sorted_bidders[1][1] if len(sorted_bidders) > 1 else 0

    # Pay runner_up + 5, capped at winner's own bid (so ties pay exact bid)
    amount_paid = min(runner_up_bid + 5, winner_bid)
    winner = state.players[winner_idx]
    _apply_debt(winner, amount_paid)
    winner.buildings_played.append(patent)
    # Apply the patent's rates to the winner's accumulated rates (most CSV
    # patents have empty rates; the mechanical effects are wired via hooks)
    winner.apply_rates(patent)
    # Run any one-shot acquisition effects (Energy Vault init, Thinking
    # Machines draw + hand_size, etc.)
    _apply_patent_acquisition(state, winner, patent)
    return (winner_idx, amount_paid)


def do_patent_auction(state: GameState, active_player: Player) -> str:
    """Run a silent patent auction.

    Draws the top patent from the pile, collects bids, settles, applies the
    result. The bid for each player is taken from `state.pending_bids` if
    set (used by the play adapter to inject human-supplied bids); otherwise
    falls back to a heuristic AI bid. `active_player` is the player who
    drew the event — used for tie-breaking (see settle_silent_auction).
    """
    patent = _draw_patent(state)
    if patent is None:
        _record_event_line(
            state, kind="header", text="Patent Auction (no patents left)"
        )
        return "patent auction (no patents left)"

    _record_event_line(
        state, kind="header", text=f"Patent Auction — {patent.building}"
    )
    bids: dict[int, int] = {}
    for idx, player in enumerate(state.players):
        if idx in state.pending_bids:
            bids[idx] = state.pending_bids[idx]
        else:
            bids[idx] = _default_ai_bid(state, player, patent)
    # Clear the bid overrides; they're consumed by this single auction.
    state.pending_bids = {}

    # Snapshot vault/hand_size BEFORE settling so we can detect acquisition
    # effects (Energy Vault init, Thinking Machines draw) per-player.
    pre_vault = [
        p.patent_state.get("energy_vault", 0) for p in state.players
    ]
    pre_hand_size = [p.hand_size for p in state.players]

    # Annotate auction metadata (bids are decisions, not state mutations)
    state.log.annotate("patent", patent.building)
    state.log.annotate("bids", {
        state.players[idx].name: amt for idx, amt in bids.items()
    })

    result = settle_silent_auction(state, patent, bids, active_player)

    if result is not None:
        state.log.annotate("winner", state.players[result[0]].name)
        state.log.annotate("price", result[1])

    # Per-player bid lines, showing the winner with "WON".
    winner_idx = result[0] if result is not None else None
    sorted_bidders = sorted(
        ((idx, amt) for idx, amt in bids.items() if amt > 0),
        key=lambda kv: (-kv[1], kv[0]),
    )
    for idx, amt in sorted_bidders:
        suffix = " — WON" if idx == winner_idx else ""
        _record_event_line(
            state,
            kind="player",
            player=state.players[idx],
            text=f"Bid ${amt}{suffix}",
        )

    if result is None:
        _record_event_line(state, kind="note", text="No bids")
        return f"patent auction ({patent.building}): no bids"

    winner_idx, amount = result
    winner = state.players[winner_idx]
    _record_event_line(
        state,
        kind="note",
        text=f"{winner.name} pays ${amount} debt",
    )
    # Acquisition-effect notes
    if winner.patent_state.get("energy_vault", 0) > pre_vault[winner_idx]:
        _record_event_line(
            state,
            kind="note",
            text=f"Energy Vault initialized: {winner.patent_state['energy_vault']} PWR",
        )
    if winner.hand_size > pre_hand_size[winner_idx]:
        _record_event_line(
            state,
            kind="note",
            text="Thinking Machines: drew 1 card, hand size +1",
        )
    return (
        f"patent auction: {winner.name} "
        f"won {patent.building} for ${amount} debt"
    )


# Lazily loaded from Patents.csv AI_Value column. Populated on first
# call to _default_ai_bid. Module-level cache avoids re-reading on
# every auction.
_patent_base_values: dict[str, int] | None = None


def _get_patent_base_values() -> dict[str, int]:
    global _patent_base_values
    if _patent_base_values is None:
        from my_project.parsing import parse_patent_values
        data_dir = Path(__file__).parent / "data"
        _patent_base_values = parse_patent_values(data_dir / "Patents.csv")
    return _patent_base_values


def _default_ai_bid(state: "GameState", player: Player, patent: Card) -> int:
    """Bid on a patent auction using regression-learned values.

    Uses the time-adjusted value from CardValues.csv (early/mid phase
    based on game progress). Adds noise (±$10 in $5 increments) to
    keep games varied. Falls back to Patents.csv AI_Value, then
    rate-based heuristic.

    The AI is willing to go into debt for valuable patents — the bid
    is based on the patent's value, not available cash. Debt interest
    is cheap ($1 per $10) relative to patent value.
    """
    # Time-adjusted value from regression
    from my_project.strategies import card_value_now
    base_value = int(card_value_now(patent.building, state))
    if base_value <= 0:
        base_value = _get_patent_base_values().get(patent.building, 0)
    if base_value <= 0:
        base_value = sum(ra.amount for ra in patent.rates if ra.amount > 0) * 8
    if base_value <= 0:
        return 0
    # Bid 50-85% of value — the AI wants a return on the debt it takes on.
    # Each AI rolls independently so bids differ even for the same patent.
    fraction = random.uniform(0.50, 0.85)
    target = base_value * fraction
    # Add per-player jitter so low-value patents don't all round the same
    target += random.uniform(-10, 10)
    # Round to $5 increments
    bid = max(5, (round(target / 5) * 5))
    return bid


def _event_needs_prompt(state: GameState, event: EventCard) -> dict | None:
    """Inspect an event and return a prompt dict if it needs human input.

    Patent Auction: needs a bid from each human seat.
    Debt Collection: lets humans choose how much debt to pay down.

    Returns None if no human input is required (all-AI game, or the event
    doesn't need prompts).
    """
    if event.type == EventType.PATENT_AUCTION:
        # Peek at the next patent without drawing it
        if state.patent_idx >= len(state.patent_pile):
            return None  # no patents left, nothing to prompt
        patent = state.patent_pile[state.patent_idx]
        return {
            "kind": "patent_auction",
            "patent": _patent_to_dict(patent),
        }
    if event.type == EventType.DEBT_COLLECTION:
        debtors = [
            {"seat": i, "debt": p.debt, "money": p.money}
            for i, p in enumerate(state.players)
            if p.debt > 0 and p.money > 0
        ]
        if debtors:
            return {"kind": "debt_paydown", "players": debtors}
    return None


def _patent_to_dict(patent: Card) -> dict:
    """Serialize a patent Card for the UI prompt."""
    return {
        "name": patent.building,
        "rates": [{"resource": ra.resource.value, "amount": ra.amount} for ra in patent.rates],
        "effect": patent.effect,
    }


def execute_event_with_redraws(
    state: GameState,
    event: EventCard,
    active_player: Player,
) -> str:
    """Execute an event and any cascading redraws.

    If the event has `redraws=True`, immediately draws and executes the next
    event card from the deck (advancing event_idx). Continues chaining as
    long as each fired card has `redraws=True`. The deck is sized in
    build_event_deck to account for these extra consumptions.

    Chains may absorb END_ROUND / END_GAME; the play adapter's
    _handle_post_event detects a consumed terminal by comparing
    event_idx to the deck length.

    Mid-event interruptions: if any event in the chain needs human input
    (via _event_needs_prompt), the function pauses by setting
    state.pending_prompt and state._suspended_event, then returns the partial
    detail. The play adapter exposes the prompt to the UI and calls
    resume_pending_event() once the prompt is resolved.
    """
    # Reset per-event line capture for this fresh event chain. Subsequent
    # chained redraws append to the same list.
    state.last_event_lines = []

    # Check if THIS event needs a prompt before firing it
    prompt = _event_needs_prompt(state, event)
    if prompt is not None and _has_human_player(state):
        state.pending_prompt = prompt
        state._suspended_event = event
        state._suspended_chain_active = event.redraws
        return f"awaiting prompt: {prompt['kind']}"

    detail = execute_event(state, event, active_player)
    while event.redraws and state.event_idx < len(state.event_deck):
        next_event = state.event_deck[state.event_idx]
        # Pause-check before firing the next chained event
        next_prompt = _event_needs_prompt(state, next_event)
        if next_prompt is not None and _has_human_player(state):
            state.event_idx += 1
            state.pending_prompt = next_prompt
            state._suspended_event = next_event
            state._suspended_chain_active = next_event.redraws
            return detail + " | awaiting prompt: " + next_prompt["kind"]
        state.event_idx += 1
        detail = detail + " | " + execute_event(state, next_event, active_player)
        event = next_event
    return detail


def _has_human_player(state: GameState) -> bool:
    """Heuristic for 'is anyone in this game a human?'

    The simulation engine doesn't track human/AI seats — that lives on
    PlayableGame. We use a sentinel: humans set _is_human_game on the state
    via the play adapter at construction time.
    """
    return getattr(state, "_is_human_game", False)


def resume_pending_event(state: GameState, active_player: Player) -> str:
    """Resume a previously suspended event after its prompt was resolved.

    Called by the play adapter after the human supplies the prompt's answer.
    Fires the suspended event normally, then continues any redraw chain that
    was in progress.
    """
    if state._suspended_event is None:
        return ""
    event = state._suspended_event
    state._suspended_event = None
    state.pending_prompt = None
    chain_active = state._suspended_chain_active
    state._suspended_chain_active = False

    detail = execute_event(state, event, active_player)
    # If the suspended event was a redraw card, continue the chain
    while chain_active and state.event_idx < len(state.event_deck):
        next_event = state.event_deck[state.event_idx]
        next_prompt = _event_needs_prompt(state, next_event)
        if next_prompt is not None and _has_human_player(state):
            state.event_idx += 1
            state.pending_prompt = next_prompt
            state._suspended_event = next_event
            state._suspended_chain_active = next_event.redraws
            return detail + " | awaiting prompt: " + next_prompt["kind"]
        state.event_idx += 1
        detail = detail + " | " + execute_event(state, next_event, active_player)
        chain_active = next_event.redraws
    return detail


def _build_event_structured(
    event_type: EventType, detail: str, state: GameState,
) -> dict:
    """Build a structured dict describing what happened in an event.

    Pulls from data stored on the state by the do_* functions, so
    no string parsing is needed.
    """
    d: dict = {"event_type": event_type.value}

    if event_type == EventType.DRAW_BUILDING_CARD:
        draw_data = getattr(state, "_draw_card_data", None) or {}
        d["card_drawn"] = draw_data.get("card_drawn", "")
        d["card_drawn_slot"] = draw_data.get("card_drawn_slot")
        d["card_replaced"] = draw_data.get("card_replaced")
        d["card_replaced_slot"] = draw_data.get("card_replaced_slot")
        d["pool_idx"] = draw_data.get("pool_idx")
        d["card_rates"] = draw_data.get("rates", [])
        d["card_costs"] = draw_data.get("costs", [])
        d["card_effect"] = draw_data.get("effect")

    elif event_type == EventType.NEWS_BULLETIN:
        import re
        m = re.match(r"NEWS:\s*(.+?)(?:\s*\((.+)\))?$", detail)
        d["news_name"] = m.group(1).strip() if m else detail
        d["news_effects"] = m.group(2).strip() if m and m.group(2) else ""

    elif event_type == EventType.PATENT_AUCTION:
        import re
        m = re.match(r"patent auction:\s*(.+)", detail, re.I)
        d["auction_result"] = m.group(1).strip() if m else detail

    elif event_type == EventType.FUTURES_TRADING:
        ft_data = getattr(state, "_futures_trading_data", None) or {}
        d["market_changes"] = ft_data.get("market_changes", [])
        d["player_contributions"] = ft_data.get("player_contributions", [])

    elif event_type in (EventType.END_ROUND, EventType.END_GAME):
        d["sub_events"] = ["Power Bill", "Futures Settlement"]

    # Player snapshots after the event
    d["player_snapshots"] = [
        {"name": p.name, "money": p.money, "debt": p.debt,
         "credit": p.credit, "net_worth": p.net_worth(),
         "rates": {r.value: v for r, v in p.rates.items()}}
        for p in state.players
    ]

    return d


def execute_event(state: GameState, event: EventCard, active_player: Player) -> str:
    """Execute an event card and return a description.

    If the event card has `pwr_adjust=True`, also fires do_pwr_adjust
    AFTER the primary effect (adjusts PWR market by active player's rate).
    This is a CSV-configurable modifier — any event type can carry it.
    """
    # Most events are global (affect all players); only PWR_ADJUST is player-specific.
    if event.type == EventType.PWR_ADJUST:
        pidx = state.players.index(active_player)
        state.log.begin(f"event:{event.type.value}", active_player.name, pidx, f"Event: {event.type.value}")
    else:
        label = event.label or event.type.value
        state.log.begin(f"event:{event.type.value}", "", -1, f"Event: {label}")
    detail: str
    match event.type:
        case EventType.NO_EVENT:
            detail = "no event"
        case EventType.PWR_ADJUST:
            # Standalone pwr_adjust event (backward compat)
            do_pwr_adjust(state, active_player)
            detail = f"PWR adjust (rate={active_player.rate(Resource.PWR)})"
        case EventType.POWER_BILL:
            do_power_bill(state)
            detail = "power bill"
        case EventType.DEBT_COLLECTION:
            do_debt_collection(state)
            detail = "debt collection"
        case EventType.FUTURES_TRADING:
            do_futures_trading(state)
            detail = "futures trading"
        case EventType.NEWS:
            detail = do_news(state, event)
        case EventType.NEWS_BULLETIN:
            detail = do_news_bulletin(state, active_player)
        case EventType.DRAW_BUILDING_CARD:
            detail = do_draw_building_card(state)
        case EventType.PATENT_AUCTION:
            detail = do_patent_auction(state, active_player)
        case EventType.END_ROUND:
            _record_event_line(state, kind="header", text="END OF ROUND")
            do_power_bill(state)
            do_futures_settlement(state)
            detail = "END OF ROUND (power bill + futures settlement)"
        case EventType.END_GAME:
            _record_event_line(state, kind="header", text="END GAME")
            do_power_bill(state)
            do_futures_settlement(state)
            detail = "END GAME (final power bill + futures settlement)"
        case _:
            detail = f"unknown event: {event.type}"

    # Attach structured metadata based on event type
    state._last_event_structured = _build_event_structured(
        event.type, detail, state,
    )
    structured = state._last_event_structured
    structured["redraws"] = event.redraws
    structured["pwr_adjust_flag"] = event.pwr_adjust

    # PWR_Adjust modifier: fire after the primary effect if the card flag is set.
    if event.pwr_adjust and event.type != EventType.PWR_ADJUST:
        pwr_before = state.market.price(Resource.PWR)
        do_pwr_adjust(state, active_player)
        pwr_after = state.market.price(Resource.PWR)
        pwr_rate = active_player.rate(Resource.PWR)
        detail += f" + PWR adjust ({pwr_rate:+d})"
        structured["pwr_adjust"] = {
            "rate": pwr_rate,
            "price_before": pwr_before,
            "price_after": pwr_after,
        }
    # Bonus building-card draw: if the event carries the redraws flag,
    if event.redraws and event.type != EventType.DRAW_BUILDING_CARD:
        bonus_detail = do_draw_building_card(state)
        detail += f" + {bonus_detail}"

    structured["detail"] = detail
    state._last_event_data = structured
    state.log.end()
    return detail


