"use strict";
/* Backgammon client. Talks to the Node/TS backend which is authoritative for
   rules; the client keeps a per-checker location model so movement animates. */

const BAR = 0, OFF = 25;
const $ = (id) => document.getElementById(id);

// ---- board layout: which point sits in each grid cell ----
// Top row (row1) cols 1..6 then bar then cols 8..13; bottom row similar.
const TOP_LEFT = [13, 14, 15, 16, 17, 18];
const TOP_RIGHT = [19, 20, 21, 22, 23, 24];
const BOT_LEFT = [12, 11, 10, 9, 8, 7];
const BOT_RIGHT = [6, 5, 4, 3, 2, 1];

let state = null;               // latest server state
let model = { white: [], black: [] }; // each entry: 1..24 | "bar" | "off"
let checkerEls = { white: [], black: [] };
let pointDivs = {};             // point number -> div
let barDiv = null, trayDiv = null;
let selected = null;            // { from } currently selected source
let busy = false;               // AI animating / transitions in flight
let endTimer = null;
let csize = 30;

// ---------- build the static board ----------
function buildBoard() {
  const pf = $("playfield");
  pf.innerHTML = "";
  const hintLayer = $("pointHints");
  hintLayer.innerHTML = "";
  $("checkerLayer").innerHTML = "";
  pointDivs = {};

  const makePoint = (num, row, col, isTop) => {
    const d = document.createElement("div");
    const colorClass = (col % 2 === (isTop ? 0 : 1)) ? "a" : "b";
    d.className = `point ${isTop ? "top" : "bottom"} ${colorClass}`;
    d.style.gridRow = String(row);
    d.style.gridColumn = String(col);
    d.dataset.testid = "point";
    d.dataset.point = String(num);
    const lbl = document.createElement("div");
    lbl.className = "plabel";
    lbl.textContent = num;
    d.appendChild(lbl);
    pf.appendChild(d);
    pointDivs[num] = d;
  };

  // top row
  TOP_LEFT.forEach((n, i) => makePoint(n, 1, i + 1, true));
  TOP_RIGHT.forEach((n, i) => makePoint(n, 1, i + 8, true));
  // bottom row
  BOT_LEFT.forEach((n, i) => makePoint(n, 2, i + 1, false));
  BOT_RIGHT.forEach((n, i) => makePoint(n, 2, i + 8, false));

  // bar
  barDiv = document.createElement("div");
  barDiv.className = "bar barcol";
  barDiv.dataset.testid = "bar";
  barDiv.style.gridColumn = "7";
  barDiv.style.gridRow = "1 / span 2";
  pf.appendChild(barDiv);

  // off tray
  trayDiv = document.createElement("div");
  trayDiv.className = "offtray";
  trayDiv.dataset.testid = "off-tray";
  trayDiv.innerHTML =
    '<div class="offhalf top" data-testid="off-ai"><div class="offlabel">AI off</div></div>' +
    '<div class="offhalf bottom" data-testid="off-you"><div class="offlabel">Your off</div></div>';
  pf.appendChild(trayDiv);

  // pre-create 15 checker elements per color
  checkerEls = { white: [], black: [] };
  for (const color of ["white", "black"]) {
    for (let i = 0; i < 15; i++) {
      const c = document.createElement("div");
      c.className = `checker ${color}`;
      c.dataset.testid = "checker";
      c.dataset.color = color;
      c.style.transform = "translate(-100px,-100px)";
      $("checkerLayer").appendChild(c);
      checkerEls[color].push(c);
    }
  }
}

// ---------- geometry ----------
function computeSizes() {
  const sample = pointDivs[6];
  if (!sample) return;
  const pw = sample.offsetWidth;
  const ph = sample.offsetHeight;
  csize = Math.min(pw * 0.86, (ph * 0.94) / 5.2);
  csize = Math.max(14, csize);
  $("checkerLayer").style.setProperty("--csize", csize + "px");
  $("pointHints").style.setProperty("--csize", csize + "px");
}

// x,y (top-left of checker) for a given location + stack index
function xyFor(color, loc, index, count) {
  if (loc === "bar") {
    const bx = barDiv.offsetLeft + barDiv.offsetWidth / 2 - csize / 2;
    const bh = barDiv.offsetHeight;
    const step = csize * 0.9;
    if (color === "white") return { x: bx, y: bh / 2 + 4 + index * step };
    return { x: bx, y: bh / 2 - csize - 4 - index * step };
  }
  if (loc === "off") {
    const tx = trayDiv.offsetLeft + trayDiv.offsetWidth / 2 - csize / 2;
    const th = trayDiv.offsetHeight;
    const step = csize * 0.30;
    if (color === "white") return { x: tx, y: th - csize - 6 - index * step };
    return { x: tx, y: 6 + index * step };
  }
  // point 1..24
  const d = pointDivs[loc];
  const isTop = loc >= 13;
  const x = d.offsetLeft + d.offsetWidth / 2 - csize / 2;
  const usable = d.offsetHeight * 0.96;
  let step = csize;
  if (count > 5) step = Math.min(csize, (usable - csize) / (count - 1));
  if (isTop) return { x, y: d.offsetTop + 2 + index * step };
  return { x, y: d.offsetTop + d.offsetHeight - csize - 2 - index * step };
}

// ---------- render checkers from model ----------
function render() {
  computeSizes();
  for (const color of ["white", "black"]) {
    // group indices by location
    const byLoc = {};
    model[color].forEach((loc, i) => {
      (byLoc[loc] = byLoc[loc] || []).push(i);
    });
    // reset all badges
    checkerEls[color].forEach((el) => {
      const b = el.querySelector(".countbadge");
      if (b) b.remove();
    });
    for (const loc in byLoc) {
      const idxs = byLoc[loc];
      const count = idxs.length;
      idxs.forEach((ci, stackPos) => {
        const el = checkerEls[color][ci];
        el.dataset.loc = String(loc);
        const { x, y } = xyFor(color, loc === "bar" || loc === "off" ? loc : Number(loc), stackPos, count);
        el.style.width = csize + "px";
        el.style.height = csize + "px";
        el.style.transform = `translate(${x}px, ${y}px)`;
        el.style.zIndex = String(10 + stackPos);
        // count badge on the topmost checker when a stack is tall
        if (count > 5 && stackPos === Math.min(count - 1, 4)) {
          const badge = document.createElement("div");
          badge.className = "countbadge";
          badge.textContent = count;
          el.appendChild(badge);
        }
      });
    }
  }
  applySelectable();
}

// ---------- model sync with authoritative counts ----------
function desiredLocs(color, s) {
  const arr = [];
  const sign = color === "white" ? 1 : -1;
  for (let p = 1; p <= 24; p++) {
    const v = s.points[p] * sign;
    for (let k = 0; k < v; k++) arr.push(p);
  }
  const bar = color === "white" ? s.bar.white : s.bar.black;
  for (let k = 0; k < bar; k++) arr.push("bar");
  const off = color === "white" ? s.off.white : s.off.black;
  for (let k = 0; k < off; k++) arr.push("off");
  return arr;
}
function reconcile(s) {
  for (const color of ["white", "black"]) {
    const need = desiredLocs(color, s);
    const cur = model[color];
    const result = new Array(15).fill(null);
    const used = new Array(need.length).fill(false);
    for (let i = 0; i < 15; i++) {
      const li = cur.length > i ? cur[i] : null;
      let found = -1;
      for (let j = 0; j < need.length; j++) {
        if (!used[j] && need[j] === li) { found = j; break; }
      }
      if (found >= 0) { used[found] = true; result[i] = li; }
    }
    const leftover = [];
    for (let j = 0; j < need.length; j++) if (!used[j]) leftover.push(need[j]);
    for (let i = 0; i < 15; i++) if (result[i] === null) result[i] = leftover.pop();
    model[color] = result;
  }
}

function applyLocalMove(color, from, to, hit) {
  const fromLoc = from === BAR ? "bar" : from;
  const toLoc = to === OFF ? "off" : to;
  if (hit) {
    const opp = color === "white" ? "black" : "white";
    const oi = model[opp].findIndex((l) => l === to);
    if (oi >= 0) model[opp][oi] = "bar";
  }
  const i = model[color].findIndex((l) => l === fromLoc);
  if (i >= 0) model[color][i] = toLoc;
}

// ---------- selectable checkers + hints ----------
function clearHints() {
  $("pointHints").innerHTML = "";
  checkerEls.white.forEach((e) => e.classList.remove("selected"));
  checkerEls.black.forEach((e) => e.classList.remove("selected"));
}
function applySelectable() {
  checkerEls.white.forEach((e) => { e.classList.remove("selectable"); e.onclick = null; });
  checkerEls.black.forEach((e) => { e.classList.remove("selectable"); e.onclick = null; });
  if (busy || !state || state.turn !== "white" || state.phase !== "move") return;
  const froms = new Set(state.legalMoves.map((m) => m.from));
  // mark topmost white checker of each legal source
  froms.forEach((from) => {
    const loc = from === BAR ? "bar" : from;
    // topmost checker at loc
    let topIdx = -1, topPos = -1;
    model.white.forEach((l, i) => {
      if (l === loc) { topPos++; if (topPos >= 0) topIdx = i; }
    });
    if (topIdx >= 0) {
      const el = checkerEls.white[topIdx];
      el.classList.add("selectable");
      el.onclick = () => selectSource(from);
    }
  });
}

function selectSource(from) {
  if (busy) return;
  selected = from;
  clearHints();
  // highlight selected source's topmost checker
  const loc = from === BAR ? "bar" : from;
  const idxs = [];
  model.white.forEach((l, i) => { if (l === loc) idxs.push(i); });
  if (idxs.length) checkerEls.white[idxs[idxs.length - 1]].classList.add("selected");

  const dests = state.legalMoves.filter((m) => m.from === from);
  const hintLayer = $("pointHints");
  // group by destination to know stack index and combine dice labels
  const byTo = {};
  dests.forEach((m) => { (byTo[m.to] = byTo[m.to] || []).push(m.die); });
  for (const toStr in byTo) {
    const to = Number(toStr);
    const dice = byTo[toStr];
    const loc = to === OFF ? "off" : to;
    // count existing white checkers at destination to stack the hint on top
    const cnt = model.white.filter((l) => l === (loc === "off" ? "off" : to)).length;
    const { x, y } = xyFor("white", loc === "off" ? "off" : to, cnt, cnt + 1);
    const h = document.createElement("div");
    h.className = "hint";
    h.dataset.testid = "hint";
    // NOTE: positioned via left/top (NOT transform) because the .hint pulse
    // animation drives `transform` and would override an inline translate.
    h.style.left = `${x}px`;
    h.style.top = `${y}px`;
    h.textContent = to === OFF ? "off" : dice.join("/");
    h.title = to === OFF ? "Bear off (die " + dice.join(" or ") + ")" : "Move here using die " + dice.join(" or ");
    h.onclick = () => doMove(from, to, dice[0]);
    hintLayer.appendChild(h);
  }
}

// ---------- server calls ----------
async function api(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return res.json();
}

// ---------- actions ----------
async function doRoll() {
  if (busy || !state || state.turn !== "white" || state.phase !== "roll") return;
  busy = true;
  animateDiceRolling();
  const s = await api("/api/roll");
  await sleep(480);
  state = s;
  reconcile(s);
  render();
  busy = false;
  updateUI();
  clearHints();
  if (state.turnOver && state.history === undefined) {}
  maybeAutoEnd();
}

async function doMove(from, to, die) {
  if (busy) return;
  cancelAutoEnd();
  // optimistic local move (compute hit locally)
  const hit = to !== OFF && model.black.filter((l) => l === to).length === 1;
  applyLocalMove("white", from, to, hit);
  selected = null;
  clearHints();
  render();
  const s = await api("/api/move", { from, to, die });
  state = s;
  reconcile(s);
  render();
  updateUI();
  maybeAutoEnd();
}

async function doUndo() {
  if (busy) return;
  cancelAutoEnd();
  const s = await api("/api/undo");
  state = s;
  reconcile(s);
  render();
  selected = null;
  clearHints();
  updateUI();
}

function maybeAutoEnd() {
  if (state && state.turn === "white" && state.phase === "move" && state.turnOver) {
    endTimer = setTimeout(() => doEndTurn(), 1100);
  }
}
function cancelAutoEnd() { if (endTimer) { clearTimeout(endTimer); endTimer = null; } }

async function doEndTurn() {
  cancelAutoEnd();
  if (busy) return;
  if (!state || state.turn !== "white" || !state.turnOver) return;
  const s = await api("/api/endturn");
  state = s;
  updateUI();
  clearHints();
  await runAi();
}

async function doDouble() {
  if (busy || !state || !state.canDouble) return;
  const s = await api("/api/double");
  // s.accepted / s.cubeReasoning
  if (s.winner) {
    state = s; reconcile(s); render(); updateUI();
    showModal("Double Declined", `<p>The AI declined your double.</p><p><b>${escapeHtml(s.cubeReasoning || "")}</b></p><p>${escapeHtml(s.message)}</p>`, gameOverButtons());
    return;
  }
  state = s; updateUI();
  showModal("Double Accepted", `<p>The AI accepted your double. The cube is now <b>${s.cube.value}</b>.</p><p>${escapeHtml(s.cubeReasoning || "")}</p>`, [
    { label: "Roll the dice", primary: true, onClick: () => { hideModal(); } },
  ]);
}

// ---------- AI turn ----------
async function runAi() {
  busy = true;
  updateUI();
  await sleep(500);
  const s = await api("/api/ai");

  if (s.aiDoubled) {
    // AI offers a double; player decides
    busy = false;
    state = s; updateUI();
    showModal(
      `AI offers a double`,
      `<p>The AI wants to raise the stake to <b>${s.cube.value * 2}</b>.</p>` +
      `<p>${escapeHtml(s.cubeReasoning || "")}</p>` +
      `<p>If you decline, the AI wins <b>${s.cube.value}</b> point${s.cube.value === 1 ? "" : "s"}.</p>`,
      [
        { label: `Accept (play for ${s.cube.value * 2})`, primary: true, onClick: async () => { hideModal(); await respondDouble(true); } },
        { label: "Decline", onClick: async () => { hideModal(); await respondDouble(false); } },
      ],
    );
    return;
  }

  // animate AI dice + moves
  if (s.aiDice) animateDiceRolling();
  await sleep(520);
  if (s.aiDice) renderDice(s.aiDice, []);
  const moves = s.aiMoves || [];
  for (const m of moves) {
    applyLocalMove("black", m.from, m.to, m.hit);
    render();
    await sleep(430);
  }
  state = s;
  reconcile(s);
  render();
  busy = false;
  updateUI();

  if (s.winner) {
    showGameOver(s);
  }
}

async function respondDouble(accept) {
  const s = await api("/api/double/respond", { accept });
  state = s; reconcile(s); render(); updateUI();
  if (s.winner) {
    showGameOver(s);
    return;
  }
  if (accept) {
    // AI still needs to roll & move
    await runAi();
  }
}

async function doNewGame() {
  cancelAutoEnd();
  const diff = $("difficulty").value;
  const s = await api("/api/new", { difficulty: diff });
  state = s;
  model = { white: [], black: [] };
  reconcile(s);
  render();
  busy = false;
  hideModal();
  updateUI();
}

// ---------- dice rendering ----------
const DICE_DOTS = {
  1: [4], 2: [0, 8], 3: [0, 4, 8], 4: [0, 2, 6, 8], 5: [0, 2, 4, 6, 8], 6: [0, 2, 3, 5, 6, 8],
};
function dieEl(value, used, rolling) {
  const d = document.createElement("div");
  d.className = "die" + (used ? " used" : "") + (rolling ? " rolling" : "");
  d.dataset.testid = "die";
  const on = new Set(DICE_DOTS[value] || []);
  for (let i = 0; i < 9; i++) {
    const dot = document.createElement("div");
    dot.className = "dot" + (on.has(i) ? "" : " hidden");
    d.appendChild(dot);
  }
  return d;
}
function renderDice(dice, remaining) {
  const box = $("dice");
  box.innerHTML = "";
  if (!dice || dice.length === 0) return;
  const isDouble = dice.length === 4;
  if (isDouble) {
    const total = 4, left = remaining.length;
    for (let i = 0; i < 4; i++) box.appendChild(dieEl(dice[0], i >= left, false));
  } else {
    // two distinct dice; a die is "used" if its value not remaining
    const rem = remaining.slice();
    dice.forEach((v) => {
      const idx = rem.indexOf(v);
      const used = idx < 0;
      if (!used) rem.splice(idx, 1);
      box.appendChild(dieEl(v, used, false));
    });
  }
}
function animateDiceRolling() {
  const box = $("dice");
  box.innerHTML = "";
  const n = 2;
  const els = [];
  for (let i = 0; i < n; i++) { const e = dieEl(1 + Math.floor(Math.random() * 6), false, true); box.appendChild(e); els.push(e); }
  let ticks = 0;
  const iv = setInterval(() => {
    ticks++;
    els.forEach((e) => {
      const v = 1 + Math.floor(Math.random() * 6);
      const on = new Set(DICE_DOTS[v]);
      [...e.children].forEach((dot, i) => dot.className = "dot" + (on.has(i) ? "" : " hidden"));
    });
    if (ticks > 6) clearInterval(iv);
  }, 70);
}

// ---------- UI state sync ----------
function updateUI() {
  if (!state) return;
  $("scoreWhite").textContent = state.score.white;
  $("scoreBlack").textContent = state.score.black;
  $("pipWhite").textContent = state.pip.white;
  $("pipBlack").textContent = state.pip.black;
  $("cubeVal").textContent = state.cube.value;
  $("cubeOwner").textContent = state.cube.owner === null ? "center" : (state.cube.owner === "white" ? "yours" : "AI");

  const ti = $("turnIndicator");
  const yourTurn = state.turn === "white";
  ti.textContent = state.phase === "gameover" ? "—" : (yourTurn ? "You" : "AI");
  ti.className = "turnchip " + (yourTurn ? "you" : "ai");

  const msg = $("message");
  msg.innerHTML = escapeHtml(state.message || "");
  msg.className = "";
  if (state.winner === "white") msg.className = "good";
  else if (state.winner === "black") msg.className = "bad";

  // dice
  if (state.phase === "move" && state.turn === "white") renderDice(state.dice, state.remainingDice);
  else if (state.phase !== "move" && state.turn === "white" && (!state.dice || state.dice.length === 0)) $("dice").innerHTML = "";

  // buttons
  const over = state.phase === "gameover";
  $("rollBtn").disabled = busy || over || !(state.turn === "white" && state.phase === "roll");
  $("doubleBtn").disabled = busy || over || !state.canDouble;
  $("undoBtn").disabled = busy || over || !(state.turn === "white" && state.phase === "move" && hasHistory());
  $("endTurnBtn").disabled = busy || over || !(state.turn === "white" && state.turnOver);
  $("endTurnBtn").classList.toggle("primary", !$("endTurnBtn").disabled);
  $("rollBtn").classList.toggle("primary", !$("rollBtn").disabled);

  applySelectable();
}
// we don't get history array in serialized state; infer undo availability from remaining vs dice
function hasHistory() {
  if (!state.dice || state.dice.length === 0) return false;
  return state.remainingDice.length < state.dice.length;
}

// ---------- modal ----------
function showModal(title, bodyHtml, buttons) {
  $("modalTitle").textContent = title;
  $("modalBody").innerHTML = bodyHtml;
  const bc = $("modalBtns");
  bc.innerHTML = "";
  (buttons || []).forEach((b) => {
    const btn = document.createElement("button");
    btn.className = "btn" + (b.primary ? " primary" : "");
    btn.textContent = b.label;
    btn.onclick = b.onClick;
    bc.appendChild(btn);
  });
  $("modalOverlay").classList.remove("hidden");
}
function hideModal() { $("modalOverlay").classList.add("hidden"); }
function gameOverButtons() {
  return [{ label: "New Game", primary: true, onClick: () => doNewGame() }];
}
function showGameOver(s) {
  const won = s.winner === "white";
  const title = won ? "🎉 You Win!" : "AI Wins";
  const body = `<p><b>${escapeHtml(s.message)}</b></p>` +
    `<p>Match score — You <b>${s.score.white}</b> · AI <b>${s.score.black}</b></p>`;
  showModal(title, body, gameOverButtons());
}

// ---------- utils ----------
function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

// ---------- init ----------
async function init() {
  buildBoard();
  $("rollBtn").onclick = doRoll;
  $("undoBtn").onclick = doUndo;
  $("endTurnBtn").onclick = doEndTurn;
  $("doubleBtn").onclick = doDouble;
  $("newGameBtn").onclick = doNewGame;
  $("difficulty").onchange = () => doNewGame();

  const s = await api("/api/state");
  state = s;
  reconcile(s);
  render();
  updateUI();

  let rt;
  window.addEventListener("resize", () => {
    clearTimeout(rt);
    rt = setTimeout(() => { render(); if (selected !== null && state && state.turn === "white" && state.phase === "move") selectSource(selected); }, 120);
  });
}
window.addEventListener("DOMContentLoaded", init);
