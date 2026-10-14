"use strict";
// Neondeck bridge: reports integrated terminals to the local agent over a Unix socket
// and executes focus / send requests. Node built-ins only, no build step.
const vscode = require("vscode");
const net = require("net");
const os = require("os");
const path = require("path");
const crypto = require("crypto");

const DEBOUNCE_MS = 200;
const MIN_DELAY = 1000;
const MAX_DELAY = 15000;

let out;
let sock = null;
let buf = "";
let connected = false;
let retryTimer = null;
let delay = MIN_DELAY;
let sendTimer = null;
let stopped = false;
const windowId = crypto.randomUUID();
const ids = new Map(); // terminal -> stable numeric id
let nextId = 1;

function log(msg) {
  if (out) out.appendLine(`${new Date().toISOString()} ${msg}`);
}

function socketPath() {
  const home = process.env.NEONDECK_HOME || process.env.CLAUDE_DECK_HOME || path.join(os.homedir(), ".config", "neondeck");
  return path.join(home, "bridge.sock");
}

function idOf(t) {
  let id = ids.get(t);
  if (id === undefined) { id = nextId++; ids.set(t, id); }
  return id;
}

function terminalById(id) {
  return vscode.window.terminals.find((t) => ids.get(t) === id);
}

function write(obj) {
  if (!connected || !sock) return;
  try { sock.write(JSON.stringify(obj) + "\n"); } catch (e) { log(`write: ${e.message}`); }
}

function folders() {
  return (vscode.workspace.workspaceFolders || []).map((f) => f.uri.fsPath);
}

function sendHello() {
  write({ type: "hello", windowId, app: vscode.env.appName, folders: folders(), focused: vscode.window.state.focused });
}

async function sendTerminals() {
  const active = vscode.window.activeTerminal;
  const list = [];
  for (const t of vscode.window.terminals) {
    let pid;
    try { pid = await t.processId; } catch { pid = undefined; }
    if (typeof pid !== "number") continue;
    list.push({ id: idOf(t), processId: pid, name: t.name, active: t === active });
  }
  write({ type: "terminals", terminals: list, folders: folders(), focused: vscode.window.state.focused });
}

function schedule() {
  if (!connected) return;
  clearTimeout(sendTimer);
  sendTimer = setTimeout(() => { sendTerminals().catch((e) => log(`terminals: ${e.message}`)); }, DEBOUNCE_MS);
}

function reply(req, ok, detail) {
  write({ type: "result", req, ok, detail });
}

function handle(msg) {
  if (!msg || typeof msg !== "object") return;
  if (msg.type !== "focus" && msg.type !== "send") return;
  const t = terminalById(msg.terminalId);
  if (!t) return reply(msg.req, false, "Terminale non trovato");
  try {
    if (msg.type === "focus") t.show(false);
    else if (typeof msg.text === "string") t.sendText(msg.text, false);
    else return reply(msg.req, false, "Testo mancante");
    reply(msg.req, true, "ok");
  } catch (e) {
    reply(msg.req, false, String(e && e.message || e));
  }
}

function scheduleRetry() {
  if (stopped) return;
  clearTimeout(retryTimer);
  retryTimer = setTimeout(connect, delay);
  delay = Math.min(delay * 2, MAX_DELAY);
}

function connect() {
  if (stopped || sock) return;
  const s = net.createConnection(socketPath());
  sock = s;
  buf = "";
  s.setEncoding("utf8");
  s.on("connect", () => {
    connected = true;
    delay = MIN_DELAY;
    log("connesso all'agent");
    sendHello();
    sendTerminals().catch((e) => log(`terminals: ${e.message}`));
  });
  s.on("data", (chunk) => {
    buf += chunk;
    let i;
    while ((i = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, i);
      buf = buf.slice(i + 1);
      if (!line.trim()) continue;
      try { handle(JSON.parse(line)); } catch (e) { log(`messaggio non valido: ${e.message}`); }
    }
  });
  s.on("error", (e) => { if (e.code !== "ENOENT" && e.code !== "ECONNREFUSED") log(`socket: ${e.message}`); });
  s.on("close", () => {
    if (sock === s) { sock = null; connected = false; }
    scheduleRetry();
  });
}

function reconnectNow() {
  clearTimeout(retryTimer);
  delay = MIN_DELAY;
  if (sock) { sock.destroy(); sock = null; connected = false; }
  connect();
}

function activate(context) {
  out = vscode.window.createOutputChannel("Neondeck");
  context.subscriptions.push(out);
  log(`attivo (${vscode.env.appName}), socket ${socketPath()}`);
  context.subscriptions.push(
    vscode.window.onDidOpenTerminal(schedule),
    vscode.window.onDidCloseTerminal((t) => { ids.delete(t); schedule(); }),
    vscode.window.onDidChangeActiveTerminal(schedule),
    vscode.window.onDidChangeWindowState(schedule),
    vscode.workspace.onDidChangeWorkspaceFolders(() => { sendHello(); schedule(); }),
    vscode.commands.registerCommand("neondeck.reconnect", reconnectNow),
    { dispose: deactivate },
  );
  connect();
}

function deactivate() {
  stopped = true;
  clearTimeout(retryTimer);
  clearTimeout(sendTimer);
  if (sock) sock.destroy();
  sock = null;
  connected = false;
}

module.exports = { activate, deactivate };
