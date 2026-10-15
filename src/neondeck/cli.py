"""Command line interface: run the agent, install it as a background service, manage the token."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import secrets
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__, platform
from .config import home_dir, load_config, preview_url, save_config
from .netguard import LOCALNET, is_trusted, localnet_guidance, new_entry, normalize_mac
from .platform import UnsupportedPlatform
from .vsix import build_vsix, extension_id

# Pre-rename (Claude Deck) identifier, cleaned up by install/uninstall
LEGACY_EXTENSION = "hooxlab.claude-deck-bridge"
LEGACY_LINKS = (
    Path.home() / ".vscode" / "extensions" / f"{LEGACY_EXTENSION}-0.1.0",
    Path.home() / ".cursor" / "extensions" / f"{LEGACY_EXTENSION}-0.1.0",
)


# ---------------------------------------------------------------- helpers

def editor_clis() -> list[str]:
    return platform.current().editor_clis()


def diagnose_network() -> tuple[dict | None, str | None]:
    return platform.current().diagnose_network()


def remove_legacy_links() -> None:
    for link in LEGACY_LINKS:
        if link.is_symlink():
            link.unlink()
            print(f"Rimosso il vecchio collegamento {link}")


def remove_legacy_extension(cli: str) -> None:
    res = subprocess.run([cli, "--list-extensions"], capture_output=True, text=True)
    if LEGACY_EXTENSION in res.stdout.split():
        res = subprocess.run([cli, "--uninstall-extension", LEGACY_EXTENSION], capture_output=True, text=True)
        print(f"Vecchia estensione {LEGACY_EXTENSION} {'rimossa' if res.returncode == 0 else 'NON rimossa'} con {cli}")


def load() -> tuple[Path, dict]:
    home = home_dir()
    return home, load_config(home)


def current_network() -> dict | None:
    return diagnose_network()[0]


def admin_info(cfg: dict, timeout: float = 3) -> dict | None:
    """/api/admin/info of the running server; None when it does not answer."""
    req = urllib.request.Request(f"http://127.0.0.1:{cfg['port']}/api/admin/info",
                                 headers={"X-Admin-Key": cfg["admin_key"]})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            info = json.load(r)
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) else None


def admin_call(cfg: dict, path: str, method: str = "GET", timeout: float = 3) -> tuple[int, dict | None]:
    """Call an admin endpoint of the running server: (status, json). Status 0 when it does not answer."""
    req = urllib.request.Request(f"http://127.0.0.1:{cfg['port']}{path}", method=method,
                                 headers={"X-Admin-Key": cfg["admin_key"]})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except OSError:
        return 0, None
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    return status, data if isinstance(data, dict) else None


def wait_network_check(cfg: dict, timeout: float = 8.0, fetch=admin_info, sleep=time.sleep) -> dict | None:
    """Poll the server until its first network check is done (or the timeout); returns the last info."""
    deadline = time.monotonic() + timeout
    info = None
    while True:
        info = fetch(cfg, 1) or info
        if (info and info.get("network_checked")) or time.monotonic() >= deadline:
            return info
        sleep(0.5)


def print_localnet_help() -> None:
    print("\nAttenzione: " + localnet_guidance(), file=sys.stderr)
    print("Se macOS mostra la richiesta di accesso alla rete locale per python, clicca «Consenti».",
          file=sys.stderr)


NO_NETWORK = ("Impossibile determinare la rete corrente (nessun gateway predefinito o MAC del gateway "
              "non leggibile). Verifica di essere collegato a una rete e riprova.")


def admin_url(cfg: dict) -> str:
    return f"http://localhost:{cfg['port']}/admin#key={urllib.parse.quote(cfg['admin_key'], safe='')}"


def print_qr(cfg: dict) -> None:
    import segno

    url = preview_url(cfg)
    print(f"\nTelefono (stessa rete Wi-Fi): {url}")
    print(f"Se il nome non si risolve: {preview_url(cfg, by_name=False)}\n")
    segno.make(url).terminal(compact=True)


# ---------------------------------------------------------------- commands

def cmd_run(args) -> int:
    from .server import main as run_server

    run_server()
    return 0


def cmd_install(args) -> int:
    home, cfg = load()  # creates config, token and admin key on first use
    svc = platform.current().service()
    if not args.no_service:
        rc = svc.install()
        if rc:
            return rc
        if svc.started:
            info = wait_network_check(cfg)
            if info and info.get("network_reason") == LOCALNET:
                print_localnet_help()
    if not args.no_vscode:
        remove_legacy_links()
        install_extension()
    auto_trust(home, cfg)
    if not args.no_service:
        print(f"\n{svc.log_hint()}")
        print("Pannello admin: esegui `neondeck admin`")
    print_qr(cfg)
    return 0


def auto_trust(home: Path, cfg: dict) -> None:
    """On a fresh install the current network becomes trusted, or devices could not connect at all."""
    if cfg["trusted_networks"]:
        return
    net, reason = diagnose_network()
    if net is None:
        print("\nNessuna rete fidata e rete corrente non riconosciuta: "
              "collegati alla rete di casa/ufficio ed esegui `neondeck trust`.", file=sys.stderr)
        if reason == LOCALNET:
            print_localnet_help()
        return
    entry = new_entry(net)
    cfg["trusted_networks"].append(entry)
    save_config(home, cfg)
    print(f"\nLa rete corrente è stata marcata come fidata: {entry['name']} ({entry['mac']}).")
    print("Altre reti: `neondeck trust`; elenco: `neondeck networks`.")


def install_extension() -> None:
    clis = editor_clis()
    with tempfile.TemporaryDirectory() as tmp:
        vsix = build_vsix(Path(tmp))
        if not clis:
            print("\nNessun VS Code/Cursor trovato. Installa l'estensione a mano:")
            print("  neondeck vsix, poi in VS Code: Extensions: Install from VSIX...")
            return
        for cli in clis:
            res = subprocess.run([cli, "--install-extension", str(vsix), "--force"],
                                 capture_output=True, text=True)
            ok = res.returncode == 0
            print(f"Estensione {'installata' if ok else 'NON installata'} con {cli}")
            if not ok:
                print(res.stderr.strip(), file=sys.stderr)
            remove_legacy_extension(cli)
    print('Ricarica le finestre di VS Code/Cursor: "Developer: Reload Window".')


def cmd_uninstall(args) -> int:
    platform.current().service().uninstall()
    for cli in editor_clis():
        res = subprocess.run([cli, "--uninstall-extension", extension_id()],
                             capture_output=True, text=True)
        print(f"Estensione {'rimossa' if res.returncode == 0 else 'non rimossa'} con {cli}")
        remove_legacy_extension(cli)
    remove_legacy_links()
    if args.purge:
        home = home_dir()
        shutil.rmtree(home, ignore_errors=True)
        print(f"Cartella dati eliminata: {home}")
    else:
        print("Configurazione conservata (usa --purge per eliminarla).")
    return 0


def local_backend_reports() -> list[dict]:
    """Terminal backends probed from the CLI itself (used when the server does not answer)."""
    import asyncio

    from .terminals import make_backends, reports

    backends = [b for b in make_backends(platform.current(), {}, lambda: None) if b.name != "iterm"]
    rows = asyncio.run(reports(backends))
    if platform.current().name == "darwin":
        rows.insert(0, {"name": "iterm", "text": "iTerm2: server non attivo"})
    return rows


def cmd_status(args) -> int:
    home, cfg = load()
    print(f"Versione: {__version__}")
    for line in platform.current().service().status_lines():
        print(line)
    url = f"http://127.0.0.1:{cfg['port']}/api/sessions"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {cfg['token']}"})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            data = json.load(r)
        print(f"Server: risponde su {url} ({len(data['sessions'])} sessioni)")
    except (OSError, ValueError, KeyError) as exc:
        print(f"Server: non risponde su {url} ({exc})")
    localnet = False
    info = admin_info(cfg)
    if info:
        if info.get("lan_listening"):
            print(f"Ascolto LAN: attivo su {info['lan_ip']}:{cfg['port']} (rete '{info.get('network_name')}')")
            if info.get("bonjour") is not None:
                print("Bonjour: " + ("attivo" if info["bonjour"] else "non attivo (permesso Rete locale?); il Pi può collegarsi tramite IP"))
        else:
            print("Ascolto LAN: disattivato (rete non fidata, sconosciuta o modalità solo locale)")
        if info.get("network_reason") == LOCALNET:  # what the server sees is what matters
            localnet = True
            print("Rete (vista dal server): non leggibile, permesso Rete locale mancante")
    net, reason = diagnose_network()
    localnet = localnet or reason == LOCALNET
    if net is None:
        print("Rete: non riconosciuta (i dispositivi in LAN verrebbero respinti)")
    else:
        known = next((n for n in cfg["trusted_networks"] if normalize_mac(str(n.get("mac", ""))) == net["mac"]), None)
        label = f"fidata ({known['name']})" if known else "NON fidata (esegui `neondeck trust`)"
        print(f"Rete: {net['mac']} {net.get('subnet') or ''} - {label}".replace("  ", " "))
    if localnet:
        print_localnet_help()
    print("Terminali:")
    for row in (info or {}).get("backends") or local_backend_reports():
        print(f"  {row['text']}")
    print(f"Socket bridge: {'presente' if (home / 'bridge.sock').exists() else 'assente'}")
    ext = extension_id()
    for cli in editor_clis():
        res = subprocess.run([cli, "--list-extensions"], capture_output=True, text=True)
        has = ext in res.stdout.split()
        print(f"Estensione in {cli}: {'installata' if has else 'non installata'}")
    return 0


def cmd_qr(args) -> int:
    _, cfg = load()
    net, reason = diagnose_network()
    if net is None or not is_trusted(net["mac"], cfg["trusted_networks"]):
        print("Attenzione: la rete corrente non è fidata, l'agent non ascolta sulla LAN. "
              "Esegui `neondeck trust` per autorizzarla.", file=sys.stderr)
    info = admin_info(cfg)
    if reason == LOCALNET or (info and info.get("network_reason") == LOCALNET):
        print_localnet_help()
    print_qr(cfg)
    return 0


def cmd_pair(args, call=admin_call, ask=input) -> int:
    _, cfg = load()
    if args.code:
        status, _ = call(cfg, f"/api/admin/pair/{urllib.parse.quote(args.code, safe='')}/approve", "POST")
        if status == 0:
            print("L'agent non risponde: avvialo con `neondeck run` o `neondeck install`.", file=sys.stderr)
            return 1
        if status != 200:
            print(f"Nessuna richiesta in attesa con il codice {args.code} (scaduta?).", file=sys.stderr)
            return 1
        print(f"Codice {args.code} approvato.")
        return 0
    status, data = call(cfg, "/api/admin/pair")
    if status == 0:
        print("L'agent non risponde: avvialo con `neondeck run` o `neondeck install`.", file=sys.stderr)
        return 1
    pending = (data or {}).get("pending") or []
    if not pending:
        print("Nessuna richiesta di abbinamento in attesa. Sul telefono tocca ABBINA.")
        return 0
    for q in pending:
        print(f"Codice {q['code']} da {q['ip']} ({q.get('ua') or '-'}), scade tra {q['expires_in']} s")
        try:
            answer = ask("Approvare? [s/N] ").strip().lower()
        except EOFError:
            break
        if answer in ("s", "si", "sì", "y", "yes"):
            action, done = "approve", "approvato"
        elif answer in ("n", "no"):
            action, done = "deny", "rifiutato"
        else:
            print("Saltato.")
            continue
        status, _ = call(cfg, f"/api/admin/pair/{q['code']}/{action}", "POST")
        print(f"Codice {q['code']} {done}." if status == 200 else "Richiesta scaduta nel frattempo.")
    return 0


def cmd_token(args) -> int:
    home, cfg = load()
    field, what = ("admin_key", "chiave admin") if args.admin else ("token", "token")
    if args.rotate:
        cfg[field] = secrets.token_urlsafe(24)
        save_config(home, cfg)
        print(cfg[field])
        extra = "" if args.admin else "; i dispositivi collegati dovranno reinserirlo"
        print(f"Nuova {what} salvata{extra}. L'agent in esecuzione la rilegge da solo.", file=sys.stderr)
    else:
        print(cfg[field])
    return 0


def cmd_trust(args) -> int:
    home, cfg = load()
    net, reason = diagnose_network()
    if net is None:
        print(NO_NETWORK, file=sys.stderr)
        if reason == LOCALNET:
            print_localnet_help()
        return 1
    nets = cfg["trusted_networks"]
    existing = next((n for n in nets if normalize_mac(str(n.get("mac", ""))) == net["mac"]), None)
    if existing:
        if args.name:
            existing["name"] = args.name
        entry = existing
        print("Rete già fidata.")
    else:
        entry = new_entry(net, args.name)
        nets.append(entry)
        print("Rete aggiunta.")
    save_config(home, cfg)
    print(f"  nome:   {entry['name']}\n  MAC:    {entry['mac']}\n  subnet: {entry.get('subnet') or '-'}")
    return 0


def cmd_networks(args) -> int:
    _, cfg = load()
    nets = cfg["trusted_networks"]
    net = current_network()
    if not nets:
        print("Nessuna rete fidata. Esegui `neondeck trust` dalla rete da autorizzare.")
    for i, n in enumerate(nets, 1):
        here = net is not None and normalize_mac(str(n.get("mac", ""))) == net["mac"]
        print(f"{'*' if here else ' '} {i}. {n.get('name', '?')}  {n.get('mac', '?')}  "
              f"{n.get('subnet') or '-'}  (aggiunta il {n.get('added', '?')})")
    if net is None:
        print("\nRete corrente non riconosciuta.")
    elif not is_trusted(net["mac"], nets):
        print(f"\nRete corrente NON fidata: {net['mac']} {net.get('subnet') or ''}".rstrip())
    else:
        print("\n* = rete corrente")
    return 0


def cmd_untrust(args) -> int:
    home, cfg = load()
    nets = cfg["trusted_networks"]
    ref = args.network.strip()
    target = None
    if ref.isdigit() and 1 <= int(ref) <= len(nets):
        target = nets[int(ref) - 1]
    else:
        mac = normalize_mac(ref)
        target = next((n for n in nets if (mac and normalize_mac(str(n.get("mac", ""))) == mac)
                       or str(n.get("name", "")).lower() == ref.lower()), None)
    if target is None:
        print(f"Rete non trovata: {ref} (vedi `neondeck networks`)", file=sys.stderr)
        return 1
    nets.remove(target)
    save_config(home, cfg)
    print(f"Rete rimossa: {target.get('name')} ({target.get('mac')})")
    return 0


def cmd_restart(args) -> int:
    return platform.current().service().restart()


def cmd_admin(args) -> int:
    _, cfg = load()
    url = admin_url(cfg)
    argv = platform.current().open_argv(url)
    if argv is None:
        print(f"xdg-open non trovato. Apri a mano:\n{url}")
        return 0
    return subprocess.run(argv).returncode


def cmd_vsix(args) -> int:
    print(build_vsix(Path(args.output) if args.output else Path.cwd()))
    return 0


# ---------------------------------------------------------------- entry point

def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="neondeck",
        description="Neondeck: telecomando in stile Stream Deck per le sessioni Claude Code "
                    "(iTerm2, tmux, WezTerm, kitty e VS Code).")
    p.add_argument("--version", action="version", version=f"neondeck {__version__}")
    sub = p.add_subparsers(dest="command", metavar="comando")

    def add(name, fn, help_, **kw):
        sp = sub.add_parser(name, help=help_, description=help_, **kw)
        sp.set_defaults(func=fn)
        return sp

    add("run", cmd_run, "avvia il server in primo piano (predefinito)")
    sp = add("install", cmd_install, "installa il servizio (LaunchAgent su macOS, systemd utente su Linux) e l'estensione VS Code/Cursor")
    sp.add_argument("--no-vscode", action="store_true", help="non installare l'estensione")
    sp.add_argument("--no-service", "--no-launchd", dest="no_service", action="store_true",
                    help="non installare il servizio (--no-launchd resta come alias)")
    sp = add("uninstall", cmd_uninstall, "rimuove il servizio e l'estensione")
    sp.add_argument("--purge", action="store_true", help="elimina anche la cartella di configurazione")
    add("status", cmd_status, "mostra lo stato di servizio, server, rete, terminali ed estensione")
    add("restart", cmd_restart, "riavvia l'agent in esecuzione (launchctl o systemctl)")
    add("qr", cmd_qr, "mostra l'indirizzo per il telefono e il relativo QR code")
    sp = add("pair", cmd_pair, "abbina un telefono approvando il codice a 6 cifre che mostra")
    sp.add_argument("code", nargs="?", metavar="CODICE", help="approva direttamente questo codice, senza chiedere")
    sp = add("token", cmd_token, "stampa il token di accesso (o la chiave admin)")
    sp.add_argument("--rotate", action="store_true", help="genera un nuovo valore")
    sp.add_argument("--admin", action="store_true", help="opera sulla chiave admin invece che sul token")
    sp = add("trust", cmd_trust, "marca la rete corrente come fidata")
    sp.add_argument("--name", metavar="NOME", help="nome della rete")
    add("networks", cmd_networks, "elenca le reti fidate e segna quella corrente")
    sp = add("untrust", cmd_untrust, "rimuove una rete fidata")
    sp.add_argument("network", metavar="MAC|NOME|N", help="MAC, nome o numero d'ordine (vedi `networks`)")
    add("admin", cmd_admin, "apre il pannello di amministrazione nel browser")
    sp = add("vsix", cmd_vsix, "crea il pacchetto .vsix dell'estensione e ne stampa il percorso")
    sp.add_argument("-o", "--output", metavar="PERCORSO", help="file o cartella di destinazione")
    return p


def main(argv: list[str] | None = None) -> None:
    args = make_parser().parse_args(argv)
    func = getattr(args, "func", cmd_run)
    try:
        platform.current()
        sys.exit(func(args) or 0)
    except UnsupportedPlatform as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
