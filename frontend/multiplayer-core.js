// ===== Host Game Loop + Pyodide bootstrap + Prompt resolution + Feed mutators =====
//
// Wrapped in an IIFE; public API on MP.core.
// Depends on: window.MP; Pyodide loaded dynamically inside startGame; and a
// handful of functions defined in multiplayer.js (today) / multiplayer-ui.js
// (after Commit 6):
//   - MP.ui.renderGame, MP.ui.isMyTurn, clearSelection
//   - MP.ui.showPrompt, MP.ui.showEndgame, showPatentOfficePicker
//   - MP.ui.wireGameButtons
//   - MP.ui.buildEventCardLabel (called from addEventFeedEntries for the banner)
// These resolve as bare globals at call time (top-level `function` decls in
// classic scripts are visible across scripts). When ui extraction lands, the
// bare calls here will be replaced with MP.ui.*.

(function () {
  const MP = window.MP = window.MP || {};

  // ===== Game Start (Host) =====

  const PY_FILES = [
    "__init__.py","play_adapter.py","simulation.py","events.py","models.py",
    "strategies.py","parsing.py","accounting.py"
  ];
  const DATA_FILES = [
    "data/Cards.csv","data/Contracts.csv","data/Patents.csv",
    "data/Events.csv","data/News.csv","data/CardValues.csv",
    "data/Corporations.csv","data/GameConfig.csv","data/market.csv"
  ];

  async function startGame() {
    document.getElementById("lobby-screen").style.display = "none";
    document.getElementById("loading-screen").style.display = "flex";

    // Load Pyodide script dynamically (only host needs it)
    const prog = document.getElementById("load-progress");
    prog.textContent = "Loading Pyodide runtime...";
    await new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "https://cdn.jsdelivr.net/pyodide/v0.27.0/full/pyodide.js";
      script.onload = resolve;
      script.onerror = reject;
      document.head.appendChild(script);
    });
    MP.pyodide = await loadPyodide();

    // Fetch and mount Python sources
    prog.textContent = "Loading game files...";
    MP.pyodide.FS.mkdirTree("/home/pyodide/my_project/data");

    for (const f of PY_FILES) {
      const resp = await fetch(`data/game/my_project/${f}`, {cache: "no-cache"});
      MP.pyodide.FS.writeFile(`/home/pyodide/my_project/${f}`, await resp.text());
    }
    for (const f of DATA_FILES) {
      const resp = await fetch(`data/game/my_project/${f}`, {cache: "no-cache"});
      MP.pyodide.FS.writeFile(`/home/pyodide/my_project/${f}`, await resp.text());
    }

    MP.pyodide.runPython(`import sys; sys.path.insert(0, "/home/pyodide")`);

    // Build seats array for PlayableGame
    const seats = MP.seatConfig.filter(s => s.type !== "empty").map(s => {
      if (s.type === "human-local" || s.type === "human-remote") return "human";
      return s.type; // "optimal", "smart", "random"
    });
    const names = MP.seatConfig.filter(s => s.type !== "empty").map(s => s.name);

    // Remap seat indices after filtering empties
    const seatMap = {};
    let j = 0;
    for (let i = 0; i < MP.seatConfig.length; i++) {
      if (MP.seatConfig[i].type !== "empty") {
        seatMap[i] = j++;
      }
    }
    MP.mySeat = seatMap[0]; // host is always original seat 0

    prog.textContent = "Creating game...";
    const seed = Math.floor(Math.random() * 100000);
    const corpMode = document.getElementById("opt-corp-draft")?.checked ? "draft" : "random";
    const createCode = `
from my_project.play_adapter import PlayableGame
_seats = ${JSON.stringify(seats)}
_names = ${JSON.stringify(names)}
game = PlayableGame(seed=${seed}, seats=_seats, names=_names, corporation_assignment="${corpMode}")
game
`;
    MP.game = MP.pyodide.runPython(createCode);

    // Notify clients + flip our own gameStarted flag
    MP.gameStarted = true;
    for (const [peerId, seatIdx] of Object.entries(MP.clientSeats)) {
      const mappedSeat = seatMap[seatIdx];
      if (mappedSeat !== undefined && MP.connections[peerId]) {
        MP.connections[peerId].send(JSON.stringify({type: "game_start", your_seat: mappedSeat}));
      }
    }
    // Update clientSeats to use mapped indices
    const newClientSeats = {};
    for (const [peerId, seatIdx] of Object.entries(MP.clientSeats)) {
      const mapped = seatMap[seatIdx];
      if (mapped !== undefined) newClientSeats[peerId] = mapped;
    }
    Object.keys(MP.clientSeats).forEach(k => delete MP.clientSeats[k]);
    Object.assign(MP.clientSeats, newClientSeats);

    showGameScreen();

    // Kickoff feed entry with game setup info. In draft mode we wait
    // until the draft completes so the summary shows the picked corps
    // and their starting rates — otherwise it would render empty rates.
    if (MP.game.is_draft_active()) {
      hostAdvanceDraft();
    } else {
      broadcastGameKickoff();
      hostAdvanceGame();
    }
  }

  function broadcastGameKickoff() {
    const state = MP.game.state_dict().toJs({dict_converter: Object.fromEntries});
    const setupLines = state.players.map(p => {
      const rates = RESOURCE_ORDER.map(r => {
        const v = p.rates?.[r] || 0;
        return v !== 0 ? `${v > 0 ? "+" : ""}${v}${r}` : null;
      }).filter(Boolean).join(" ");
      return `${p.name}${p.corporation ? " (" + p.corporation + ")" : ""}: ${rates || "no rates"}`;
    }).join("\n");
    const marketLine = RESOURCE_ORDER.map(r => `${r}=$${state.market[r]}`).join(" ");
    const kickoff = {
      kind: "turn-start",
      text: "Game started!",
      details: `Players:\n${setupLines}\n\nMarket: ${marketLine}`,
    };
    addFeedEntry(kickoff);
    MP.network.broadcastFeed(kickoff);
  }

  function showGameScreen() {
    document.getElementById("lobby-screen").style.display = "none";
    document.getElementById("loading-screen").style.display = "none";
    document.getElementById("game-wrapper").style.display = "flex";
    MP.ui.wireGameButtons();
  }

  // ===== Host: Game Loop =====

  function hostRefreshState() {
    const s = MP.game.state_dict().toJs({dict_converter: Object.fromEntries});
    MP.currentState = s;

    if (!MP.game.is_over() && MP.ui.isMyTurn(s)) {
      MP.currentLegal = MP.game.legal_human_actions().toJs({dict_converter: Object.fromEntries});
    } else {
      MP.currentLegal = null;
    }
    MP.ui.renderGame();
    MP.network.broadcastState();
    if (MP.debug?.isOpen()) MP.debug.updateDebugPanel();
  }

  const AI_TURN_DELAY = 800;   // ms between AI actions and event
  const AI_EVENT_DELAY = 1200; // ms to show event banner before next turn

  function hostAdvanceGame() {
    hostAdvanceStep();
  }

  // ===== Host: Corporation Draft =====

  const AI_DRAFT_DELAY = 500; // ms between AI draft picks

  function hostAdvanceDraft() {
    if (!MP.game.is_draft_active()) {
      // Draft just finished — emit the kickoff summary (now that corps
      // are assigned) and kick off the real game loop.
      hostRefreshState();
      broadcastGameKickoff();
      hostAdvanceGame();
      return;
    }
    const picker = MP.game.current_draft_picker();
    const humans = MP.currentState?.human_indices || [];
    // Always refresh first so the UI sees the latest draft state.
    hostRefreshState();
    if (humans.includes(picker)) {
      // Human seat (could be host or remote). Wait for a click / network msg.
      return;
    }
    // AI seat picks after a short delay so the UI can show who's picking.
    setTimeout(() => {
      const result = MP.game.step_draft_ai().toJs({dict_converter: Object.fromEntries});
      if (result?.ok) {
        const name = MP.currentState?.players[result.seat]?.name || `Seat ${result.seat}`;
        const entry = {
          kind: "free-action",
          text: `${name} drafts ${result.corp}`,
        };
        addFeedEntry(entry);
        MP.network.broadcastFeed(entry);
      }
      hostAdvanceDraft();
    }, AI_DRAFT_DELAY);
  }

  function handleHostDraftPick(corpName) {
    if (!MP.game.is_draft_active()) return;
    const picker = MP.game.current_draft_picker();
    if (picker !== MP.mySeat) return;
    const result = MP.game.submit_draft_pick(MP.mySeat, corpName).toJs({dict_converter: Object.fromEntries});
    if (!result?.ok) {
      alert(result?.reason || "Draft pick failed");
      hostRefreshState();
      return;
    }
    const name = MP.currentState?.players[MP.mySeat]?.name || "You";
    const entry = {kind: "free-action", text: `${name} drafts ${result.corp}`};
    addFeedEntry(entry);
    MP.network.broadcastFeed(entry);
    hostAdvanceDraft();
  }

  function handleRemoteDraftPick(peerId, msg) {
    const seatIdx = MP.clientSeats[peerId];
    if (seatIdx === undefined) return;
    if (!MP.game.is_draft_active()) return;
    if (MP.game.current_draft_picker() !== seatIdx) return;
    const result = MP.game.submit_draft_pick(seatIdx, msg.corp).toJs({dict_converter: Object.fromEntries});
    if (!result?.ok) {
      // Tell the client so they see an error instead of a hang.
      const conn = MP.connections[peerId];
      if (conn) {
        conn.send(JSON.stringify({type: "action_result", ok: false, reason: result?.reason || "Draft pick rejected"}));
      }
      return;
    }
    const name = MP.currentState?.players[seatIdx]?.name || `Seat ${seatIdx}`;
    const entry = {kind: "free-action", text: `${name} drafts ${result.corp}`};
    addFeedEntry(entry);
    MP.network.broadcastFeed(entry);
    hostAdvanceDraft();
  }

  function hostAdvanceStep() {
    if (MP.game.is_over()) {
      hostRefreshState();
      MP.ui.showEndgame();
      return;
    }

    const s = MP.game.state_dict().toJs({dict_converter: Object.fromEntries});
    const curIdx = s.current_player_index;

    // Human's turn — stop and wait for input
    if (s.human_indices.includes(curIdx)) {
      MP.game.begin_human_turn();
      hostRefreshState();
      if (MP.currentState.pending_prompt) {
        handleHostPrompt(MP.currentState.pending_prompt);
      }
      return;
    }

    // Snapshot player state BEFORE the AI turn
    const preTurnState = MP.game.state_dict().toJs({dict_converter: Object.fromEntries});
    const playerBefore = preTurnState.players[preTurnState.current_player_index];

    // AI turn — execute, show actions, pause, show event, pause, next
    const result = MP.game.step_ai_turn().toJs({dict_converter: Object.fromEntries});
    const stateSnap = MP.game.state_dict().toJs({dict_converter: Object.fromEntries});
    const eventLines = (stateSnap.last_event_lines || []).map(l => Object.assign({}, l));
    const playerSnaps = stateSnap.players.map(p => ({name: p.name, money: p.money, debt: p.debt, net_worth: p.net_worth}));

    const aiActions = result.actions || [];
    const playerIdx = result.player_index;
    const playerName = stateSnap.players[playerIdx]?.name || "AI";
    const playerAfter = stateSnap.players[playerIdx];
    const eventDetail = result.event?.detail || "";

    // Concise title: just action types ("Built Iron Mine, Sold FE")
    const titleParts = aiActions.map(a => {
      if (a.type === "build") return `Built ${(a.buildings || []).join(", ")}`;
      if (a.type === "sell") return `Sold ${a.sell_resource || ""}`;
      if (a.type === "contract") return `Contract`;
      if (a.type === "free_action") return a.detail || "Free action";
      return a.type;
    });
    const titleSummary = titleParts.join(", ") || "Pass";

    // Step 1: Show AI actions + update board
    const actionEntry = {
      kind: "turn",
      text: `${playerName}: ${titleSummary}`,
      actions: aiActions,
      playerBefore: playerBefore ? {money: playerBefore.money, debt: playerBefore.debt, net_worth: playerBefore.net_worth, rates: playerBefore.rates} : null,
      playerAfter: playerAfter ? {money: playerAfter.money, debt: playerAfter.debt, net_worth: playerAfter.net_worth, rates: playerAfter.rates} : null,
    };
    addFeedEntry(actionEntry);
    MP.network.broadcastFeed(actionEntry);
    hostRefreshState();

    // AI action animations
    MP.anim.animateAiActions(aiActions, playerIdx);

    // Step 2: After a pause, show the event (or handle prompt)
    setTimeout(() => {
      // If awaiting prompt (e.g. patent auction), don't add event entry yet —
      // the prompt resolution will add the complete event.
      if (result.awaiting_prompt) {
        hostRefreshState();
        handleHostPrompt(MP.currentState.pending_prompt);
        return;
      }

      if (eventDetail) {
        const evData = result.event?.structured || stateSnap.last_event_data || {};
        addEventFeedEntries(eventDetail, eventLines, playerSnaps, evData);

        // Render chained (redraw) events as separate feed entries
        const chained = result.chained_events || [];
        for (const ce of chained) {
          const ceData = ce.structured || {};
          ceData._is_redraw = true;
          const ceLines = ce.lines || [];
          addEventFeedEntries(ce.detail, ceLines, playerSnaps, ceData);
        }
        hostRefreshState();
      }

      // Step 3: After event display, advance to next turn
      setTimeout(() => {
        hostAdvanceStep();
      }, eventDetail ? AI_EVENT_DELAY : 200);

    }, AI_TURN_DELAY);
  }

  function handleHostPrompt(prompt) {
    if (!prompt) return;
    // Show to host if host is involved
    if (MP.mySeat >= 0) {
      MP.ui.showPrompt(prompt);
    }
    // Send to remote clients
    for (const [peerId, seatIdx] of Object.entries(MP.clientSeats)) {
      const conn = MP.connections[peerId];
      if (conn) {
        conn.send(JSON.stringify({type: "prompt", prompt, your_seat: seatIdx}));
      }
    }
  }

  // ===== Host: Handle Remote Player Actions =====

  // Stash a pending Patent Office build while we wait for the client to
  // pick which of the two drawn patents they want to keep. Null unless
  // a pick prompt is in flight.
  let _pendingPatentOfficeBuild = null;

  function handleRemoteAction(peerId, msg) {
    const seatIdx = MP.clientSeats[peerId];
    if (seatIdx === undefined || MP.currentState.current_player_index !== seatIdx) return;

    const action = msg.action;

    // Patent Office build from a remote client: the client doesn't have
    // access to MP.game, so it can't peek the patent pile locally. Send
    // a pick prompt back to the client, stash the build, and wait.
    if (action.type === "build" && action.patent_office_pick == null) {
      const hand = MP.currentState?.players[seatIdx]?.hand || [];
      const hasPatentOffice = (action.build_cards || []).some(i => hand[i]?.building === "Patent Office");
      if (hasPatentOffice) {
        const patents = MP.game.peek_patent_office_patents().toJs({dict_converter: Object.fromEntries});
        if (patents.length >= 2) {
          _pendingPatentOfficeBuild = { peerId, action };
          const conn = MP.connections[peerId];
          if (conn) {
            conn.send(JSON.stringify({
              type: "prompt",
              prompt: { kind: "patent_office_pick", patents },
            }));
          }
          return;
        }
      }
    }

    const result = MP.game.apply_human_action(MP.pyodide.toPy(action)).toJs({dict_converter: Object.fromEntries});
    if (result.ok) {
      addFeedEntry({kind: "action", text: `${MP.currentState.players[seatIdx]?.name}: ${result.detail}`});
      MP.network.broadcastFeed({kind: "action", text: `${MP.currentState.players[seatIdx]?.name}: ${result.detail}`});
    } else {
      // Tell the client their action was rejected so they see an error
      // instead of a phantom animation with no feedback. Success is
      // confirmed implicitly by the state refresh that follows.
      const conn = MP.connections[peerId];
      if (conn) {
        conn.send(JSON.stringify({
          type: "action_result",
          ok: false,
          reason: result.reason || "Action failed",
        }));
      }
    }
    hostRefreshState();
  }

  function handlePatentOfficePick(peerId, pickIdx) {
    if (!_pendingPatentOfficeBuild || _pendingPatentOfficeBuild.peerId !== peerId) return;
    const { action } = _pendingPatentOfficeBuild;
    _pendingPatentOfficeBuild = null;
    action.patent_office_pick = pickIdx;
    handleRemoteAction(peerId, { action });
  }

  function handleRemoteEndTurn(peerId) {
    const seatIdx = MP.clientSeats[peerId];
    if (seatIdx === undefined || MP.currentState.current_player_index !== seatIdx) return;

    const result = MP.game.end_human_turn().toJs({dict_converter: Object.fromEntries});
    const snap = MP.game.state_dict().toJs({dict_converter: Object.fromEntries});
    const playerSnaps = snap.players.map(p => ({name: p.name, money: p.money, debt: p.debt, net_worth: p.net_worth}));
    const evData = result.structured || snap.last_event_data || {};
    addEventFeedEntries(
      result.detail || "Turn ended",
      (result.lines || snap.last_event_lines || []).map(l => Object.assign({}, l)),
      playerSnaps,
      evData,
    );
    // Render chained (redraw) events as separate feed entries. Missing this
    // on the remote-end-turn path meant redraws never showed up on either
    // the host feed or the client feed when a remote player ended their turn.
    const chained = result.chained_events || [];
    for (const ce of chained) {
      const ceData = ce.structured || {};
      ceData._is_redraw = true;
      const ceLines = ce.lines || [];
      addEventFeedEntries(ce.detail, ceLines, playerSnaps, ceData);
    }

    if (result.awaiting_prompt) {
      hostRefreshState();
      handleHostPrompt(MP.currentState.pending_prompt);
      return;
    }
    hostRefreshState();
    hostAdvanceGame();
  }

  function handleRemotePromptAnswer(peerId, answers) {
    const seatIdx = MP.clientSeats[peerId];
    if (seatIdx === undefined) return;
    // Store the answer and check if all answers collected
    pendingPromptAnswers = pendingPromptAnswers || {};
    pendingPromptAnswers[seatIdx] = answers;
    tryResolvePrompt();
  }

  function handleRemotePatentAction(peerId, msg) {
    const seatIdx = MP.clientSeats[peerId];
    if (seatIdx === undefined || MP.currentState.current_player_index !== seatIdx) return;

    let result;
    switch (msg.action) {
      case "water_engine":
        result = MP.game.use_water_engine(seatIdx).toJs({dict_converter: Object.fromEntries});
        break;
      case "nanotech":
        result = MP.game.use_nanotechnology(seatIdx).toJs({dict_converter: Object.fromEntries});
        break;
      case "oc":
        result = MP.game.use_optimization_center(seatIdx, msg.resource).toJs({dict_converter: Object.fromEntries});
        break;
      case "teleport":
        result = MP.game.use_teleportation(
          seatIdx,
          msg.resource,
          msg.hacker_target || null,
          msg.hacker_direction || 0,
        ).toJs({dict_converter: Object.fromEntries});
        break;
    }
    if (result?.ok) {
      addFeedEntry({kind: "free-action", text: `${MP.currentState.players[seatIdx]?.name}: ${result.detail}`});
      MP.network.broadcastFeed({kind: "free-action", text: `${MP.currentState.players[seatIdx]?.name}: ${result.detail}`});
    }
    hostRefreshState();
  }

  function handleRemotePoolSwap(peerId, msg) {
    const seatIdx = MP.clientSeats[peerId];
    if (seatIdx === undefined || MP.currentState.current_player_index !== seatIdx) return;

    // Capture card names before swap
    const player = MP.currentState?.players?.[seatIdx];
    const handCard = player?.hand?.[msg.hand_idx];
    const poolCard = MP.currentState?.pool?.[msg.pool_idx];
    const handCardName = handCard?.building || "?";
    const poolCardName = poolCard?.building || "?";

    MP.game.human_pool_swap(msg.hand_idx, msg.pool_idx);
    hostRefreshState();

    // Add swap to event feed
    const playerName = player?.name || "Player";
    addFeedEntry({
      kind: "free-action",
      text: `${playerName}: Swapped ${handCardName} ↔ ${poolCardName}`
    });
    MP.network.broadcastFeed({
      kind: "free-action",
      text: `${playerName}: Swapped ${handCardName} ↔ ${poolCardName}`
    });
  }

  // ===== Prompt Collection =====
  let pendingPromptAnswers = {};

  function submitPrompt() {
    const prompt = MP.currentState?.pending_prompt;
    if (!prompt) return;

    if (MP.role === "host") {
      // Collect host's answer
      const answers = collectPromptInputs(prompt);
      pendingPromptAnswers[MP.mySeat] = answers;
      document.getElementById("prompt-modal").style.display = "none";
      tryResolvePrompt();
    } else {
      // Client sends answer to host
      const answers = collectPromptInputs(prompt);
      MP.hostConn.send(JSON.stringify({type: "prompt_answer", answers}));
      document.getElementById("prompt-modal").style.display = "none";
    }
  }

  function collectPromptInputs(prompt) {
    if (prompt.kind === "patent_auction") {
      const inp = document.querySelector(`.prompt-bid-input[data-seat-idx="${MP.mySeat}"]`);
      return {bids: {[MP.mySeat]: parseInt(inp?.value || "0")}};
    }
    if (prompt.kind === "debt_paydown") {
      const inp = document.querySelector(`.prompt-paydown-input[data-seat-idx="${MP.mySeat}"]`);
      return {payments: {[MP.mySeat]: parseInt(inp?.value || "0")}};
    }
    return {};
  }

  function tryResolvePrompt() {
    if (MP.role !== "host" || !MP.currentState?.pending_prompt) return;
    const prompt = MP.currentState.pending_prompt;

    // Check if we have answers from all required human seats
    const humanSeats = MP.currentState.human_indices || [];
    const allAnswered = humanSeats.every(idx => pendingPromptAnswers[idx] !== undefined);
    if (!allAnswered) return;

    // Merge all answers
    let merged = {};
    if (prompt.kind === "patent_auction") {
      merged = {bids: {}};
      for (const answers of Object.values(pendingPromptAnswers)) {
        Object.assign(merged.bids, answers.bids || {});
      }
    } else if (prompt.kind === "debt_paydown") {
      merged = {payments: {}};
      for (const answers of Object.values(pendingPromptAnswers)) {
        Object.assign(merged.payments, answers.payments || {});
      }
    }

    pendingPromptAnswers = {};
    const result = MP.game.resolve_pending_prompt(MP.pyodide.toPy(merged)).toJs({dict_converter: Object.fromEntries});
    const snapAfter = MP.game.state_dict().toJs({dict_converter: Object.fromEntries});
    const promptEvData = snapAfter.last_event_data || {};
    const promptLines = (snapAfter.last_event_lines || []).map(l => Object.assign({}, l));
    addEventFeedEntries(
      result.detail || "Prompt resolved",
      promptLines,
      snapAfter.players.map(p => ({name: p.name, money: p.money, debt: p.debt, net_worth: p.net_worth})),
      promptEvData,
    );

    if (result.awaiting_prompt) {
      hostRefreshState();
      handleHostPrompt(MP.currentState.pending_prompt);
      return;
    }
    hostRefreshState();
    hostAdvanceGame();
  }

  // ===== Event Feed data mutators =====

  function addFeedEntry(entry) {
    entry.time = new Date().toLocaleTimeString();
    MP.feedEntries.push(entry);
    MP.ui.renderFeed();
  }

  function addEventFeedEntries(_eventDetail, eventLines, playerSnaps, eventData) {
    const entry = {
      kind: "event",
      eventData: eventData || {},
      event_lines: eventLines,
      player_snapshots: (eventData && eventData.player_snapshots) || playerSnaps,
    };
    addFeedEntry(entry);
    MP.network.broadcastFeed(entry);
    MP.anim.showEventBanner(MP.ui.buildEventCardLabel(eventData || {}));

    // Round marker when END_ROUND fires
    const evType = (eventData || {}).event_type;
    if (evType === "end_round") {
      const roundNum = MP.currentState?.deck_round || MP.currentState?.round || "?";
      const marker = {kind: "round-marker", text: `--- Round ${roundNum} Complete ---`};
      addFeedEntry(marker);
      MP.network.broadcastFeed(marker);
    }

    // Animate draw building card: new card flies from event deck to pool
    if (eventData?.event_type === "draw_building_card" && eventData.card_drawn) {
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          const poolCards = document.querySelectorAll("#mp-pool-grid .pool-card");
          const destEl = poolCards[poolCards.length - 1]; // new card is last in pool
          const deckEl = document.getElementById("event-deck-card");
          if (destEl && deckEl) {
            const destRect = destEl.getBoundingClientRect();
            const deckRect = deckEl.getBoundingClientRect();
            MP.anim.animateCard(
              deckRect,
              destRect,
              `<div class="card-name">${eventData.card_drawn}</div>`
            );
          }
        });
      });
    }
  }

  // Public API
  MP.core = {
    startGame,
    showGameScreen,
    hostRefreshState,
    hostAdvanceGame,
    hostAdvanceStep,
    hostAdvanceDraft,
    handleHostDraftPick,
    handleRemoteDraftPick,
    handleHostPrompt,
    handleRemoteAction,
    handleRemoteEndTurn,
    handleRemotePromptAnswer,
    handleRemotePatentAction,
    handleRemotePoolSwap,
    submitPrompt,
    tryResolvePrompt,
    addFeedEntry,
    addEventFeedEntries,
    handlePatentOfficePick,
    // setPromptAnswer lets UI auto-submit an empty answer for a seat that
    // isn't involved in the current prompt (e.g. debt paydown when I'm not
    // in debt). Must NOT be used to bypass the prompt modal for active seats.
    setPromptAnswer: (seat, answers) => { pendingPromptAnswers[seat] = answers; },
  };
})();
