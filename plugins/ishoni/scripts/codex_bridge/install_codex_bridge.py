#!/usr/bin/env python3
"""Install the local Codex dispatcher adapter without changing Tailmsg's wire protocol."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid

sys.dont_write_bytecode = True   # the installer may run from a plugin cache; keep it pristine
from codex_bridge import validate_config  # noqa: E402  (sibling module, after the bytecode switch)

SOURCE = Path(__file__).resolve().parent
LABEL = "dev.tailmsg.codex-bridge"
FILES = ("codex_bridge.py", "codex_queue_client.py", "codex_resident.py", "CODEX_DISPATCHER.md")
DEFAULT_CODEX = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
TUNING = {"poll_seconds": 2, "batch_delay_seconds": 30, "retry_seconds": 15}


INSTRUCTIONS = "CODEX_DISPATCHER.md"


def install_sources(source, target, write=True):
    """Copy the adapter files into target -> (sha256 per file, notes).

    An existing local CODEX_DISPATCHER.md is kept byte for byte: it may hold the user's own workflow.
    Only a first install copies the bundled template; template updates are reviewed and merged by hand.
    The caller has already backed the old files up, so rollback restores exactly what was there."""
    hashes, notes = {}, []
    for filename in FILES:
        path = target / filename
        contents = (source / filename).read_bytes()
        if filename == INSTRUCTIONS and path.is_file() and not path.is_symlink():
            kept = path.read_bytes()
            if kept != contents:
                notes.append("kept the installed dispatcher instructions %s; the bundled template %s differs, "
                             "review and merge it yourself if wanted" % (path, source / filename))
            hashes[filename] = hashlib.sha256(kept).hexdigest()
            continue
        if write:
            atomic(path, contents)
        hashes[filename] = hashlib.sha256(contents).hexdigest()
    return hashes, notes


def merged_bridge_config(existing, home, state, target, thread_id, allow_replies, updates=None):
    """The bridge config an install or upgrade writes, validated like the bridge itself loads it.

    Identity fields are (re)pinned. Everything else already there survives an upgrade: tuned timing,
    custom dispatcher instructions, kind routing (excluded_kinds, excluded_kind_adapter, ...), pinned
    Codex settings and unknown keys. Replies keep the authorized value unless allow_replies is True or
    False; new installs start with replies off."""
    config = dict(existing)
    config.update({"tailmsg_home": str(home), "bridge_home": str(state), "dispatcher_thread_id": thread_id,
                   "dispatcher_host_id": "local", "caller_thread_id": thread_id})
    config.setdefault("dispatcher_instructions", str(target / "CODEX_DISPATCHER.md"))
    for key, value in TUNING.items():
        config.setdefault(key, value)
    config["allow_replies"] = bool(existing.get("allow_replies")) if allow_replies is None else bool(allow_replies)
    config.update(updates or {})
    try:
        validate_config(dict(config))
    except ValueError as exc:
        raise RuntimeError("the bridge configuration would be invalid (%s); fix %s first"
                           % (exc, home / "codex-bridge.json")) from exc
    return config


def atomic(path, data, mode=0o600):
    """Replace path with data via a fresh, exclusively created temporary file in the same directory."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, str(path))
    except BaseException:
        if os.path.lexists(temporary):
            os.unlink(temporary)
        raise


def check_queue_dispatcher(path, thread_id):
    """Read an existing queue without creating it or changing its contents."""
    if not os.path.lexists(str(path)):
        return
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("the existing dispatcher queue cannot be read safely; review it before reinstalling")
    db = None
    try:
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
        thread_ids = [row[0] for row in db.execute("SELECT DISTINCT thread_id FROM messages")]
    except (OSError, sqlite3.Error) as exc:
        raise RuntimeError("the existing dispatcher queue cannot be read; review it before reinstalling") from exc
    finally:
        if db is not None:
            db.close()
    if any(existing != thread_id for existing in thread_ids):
        raise RuntimeError("the persistent dispatcher ID conflicts with the existing queue; review it before reinstalling")


def install(args):
    os.umask(0o077)
    args.thread_id = str(uuid.UUID(args.thread_id))
    home = Path(args.tailmsg_home).expanduser().resolve()
    original_path = home / "config.json"
    original = json.loads(original_path.read_text())
    for filename in FILES:
        if not (SOURCE / filename).is_file():
            raise RuntimeError("missing adapter source: " + filename)
    if not (home / "tailmsg.py").is_file():
        raise RuntimeError("install Tailmsg first")
    current = (original.get("agents") or {}).get("codex") or {}
    old_adapter = current.get("wake_adapter")
    target = home / "codex-local"
    script = target / "codex_bridge.py"
    python = str(Path(sys.executable).resolve())
    config_path = home / "codex-bridge.json"
    existing_config = json.loads(config_path.read_text()) if config_path.exists() else {}
    if existing_config.get("dispatcher_thread_id", args.thread_id) != args.thread_id:
        raise RuntimeError("the persistent dispatcher ID cannot be replaced by reinstalling; drain and review the existing queue first")
    adapter = {"type": "exec", "argv": [python, str(script), "--config", str(config_path), "wake"]}
    if old_adapter and old_adapter != adapter:
        raise RuntimeError("a different codex wake adapter is already installed; review it first")
    state = home / "codex-bridge"
    check_queue_dispatcher(state / "bridge.sqlite3", args.thread_id)
    codex_path = getattr(args, "codex_path", None) or existing_config.get("codex_path") or DEFAULT_CODEX
    config = merged_bridge_config(existing_config, home, state, target, args.thread_id,
                                  getattr(args, "allow_replies", None),
                                  {"codex_path": str(Path(codex_path).expanduser().resolve())})
    if getattr(args, "dry_run", False):
        # Every check above has run; report the plan before the first write or service call.
        plist_path = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
        print(json.dumps({
            "dry_run": True, "dispatcher_thread_id": args.thread_id, "tailmsg_home": str(home),
            "would_copy": [str(target / filename) for filename in FILES],
            "would_write": [str(config_path), str(original_path), str(plist_path), str(state / "installation.json")],
            "codex_wake_adapter": adapter, "existing_codex_wake_adapter": old_adapter,
            "backup_directory": str(home / "backups" / "codex-bridge-<UTC timestamp>"),
            "would_reload_services": [] if args.no_start else [LABEL, "dev.tailmsg"],
            "allow_replies": config["allow_replies"], "codex_path": config["codex_path"],
            "dispatcher_instructions": config["dispatcher_instructions"],
            "excluded_kinds": config.get("excluded_kinds", []),
            "notes": install_sources(SOURCE, target, write=False)[1]}, ensure_ascii=False, indent=2))
        return
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = home / "backups" / ("codex-bridge-" + stamp)
    backup.mkdir(mode=0o700, parents=True)
    plist_path = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
    destinations = [(original_path, "tailmsg-config.json"), (config_path, "bridge-config.json"),
                    (plist_path, "service.plist"), (state / "installation.json", "bridge-installation.json")]
    destinations += [(target / filename, "codex-local/" + filename) for filename in FILES]
    manifest = {"tailmsg_home": str(home), "targets": [], "service_was_loaded": False}
    manifest["service_was_loaded"] = subprocess.run(
        ["launchctl", "print", "gui/%d/%s" % (os.getuid(), LABEL)], capture_output=True).returncode == 0
    for existing, relative in destinations:
        saved = None
        if existing.is_file():
            saved = relative
            destination = backup / relative
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copy2(str(existing), str(destination))
        manifest["targets"].append({"path": str(existing), "backup": saved})
    atomic(backup / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode())
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    hashes, notes = install_sources(SOURCE, target)
    atomic(config_path, (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode())
    original.setdefault("agents", {}).setdefault("codex", {})["wake_adapter"] = adapter
    atomic(original_path, (json.dumps(original, ensure_ascii=False, indent=2) + "\n").encode())
    plist = {
        "Label": LABEL, "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
        "ProgramArguments": [python, str(script), "--config", str(config_path), "daemon"],
        "WorkingDirectory": str(target),
        "StandardOutPath": str(state / "service.log"),
        "StandardErrorPath": str(state / "service.log"),
        "EnvironmentVariables": {"TAILSCALE_BE_CLI": "1", "PATH": "/usr/bin:/bin:/opt/homebrew/bin"},
    }
    atomic(plist_path, plistlib.dumps(plist))
    receipt = {"dispatcher_thread_id": args.thread_id, "host_id": "local", "installed_at": stamp,
               "source": str(SOURCE), "files_sha256": hashes, "backup": str(backup),
               "service_label": LABEL, "allow_replies": config["allow_replies"]}
    atomic(state / "installation.json", (json.dumps(receipt, indent=2) + "\n").encode())
    if not args.no_start:
        service = "gui/%d/%s" % (os.getuid(), LABEL)
        subprocess.run(["launchctl", "bootout", service], capture_output=True)
        subprocess.run(["launchctl", "bootstrap", "gui/%d" % os.getuid(), str(plist_path)], check=True)
        subprocess.run(["launchctl", "kickstart", "-k", "gui/%d/dev.tailmsg" % os.getuid()], check=True)
    print(json.dumps({"installed": True, "started": not args.no_start,
                      "dispatcher_thread_id": args.thread_id, "backup": str(backup),
                      "config": str(config_path), "notes": notes}, ensure_ascii=False))


def rollback(args):
    backup = Path(args.rollback).expanduser().resolve()
    manifest = json.loads((backup / "manifest.json").read_text())
    if getattr(args, "dry_run", False):
        print(json.dumps({
            "dry_run": True, "backup": str(backup),
            "would_restore": [t["path"] for t in manifest["targets"] if t["backup"] is not None],
            "would_remove": [t["path"] for t in manifest["targets"] if t["backup"] is None],
            "would_reload_services": [] if args.no_start else [LABEL, "dev.tailmsg"],
            "queue_and_inbox_preserved": True}, ensure_ascii=False, indent=2))
        return
    if not args.no_start:
        subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), LABEL)], capture_output=True)
    for target in manifest["targets"]:
        destination = Path(target["path"])
        saved = target["backup"]
        if saved is None:
            if destination.is_file():
                destination.unlink()
        else:
            atomic(destination, (backup / saved).read_bytes())
    if not args.no_start:
        plist_path = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
        if manifest.get("service_was_loaded") and plist_path.is_file():
            subprocess.run(["launchctl", "bootstrap", "gui/%d" % os.getuid(), str(plist_path)], check=True)
        subprocess.run(["launchctl", "kickstart", "-k", "gui/%d/dev.tailmsg" % os.getuid()], check=True)
    print(json.dumps({"rolled_back": True, "backup": str(backup), "queue_and_inbox_preserved": True}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread-id", help="existing local desktop dispatcher thread UUID")
    parser.add_argument("--rollback", help="restore a specific installation backup without deleting inbox or queue")
    parser.add_argument("--tailmsg-home", default="~/.tailmsg")
    parser.add_argument("--codex-path", help="Codex CLI (default: the installed value, else the Codex desktop CLI)")
    replies = parser.add_mutually_exclusive_group()
    replies.add_argument("--allow-replies", dest="allow_replies", action="store_true", default=None,
                         help="enable only with user authorization")
    replies.add_argument("--no-allow-replies", dest="allow_replies", action="store_false",
                         help="turn automatic replies off (upgrades otherwise keep the installed setting)")
    parser.add_argument("--no-start", action="store_true", help="install without reloading either service")
    parser.add_argument("--dry-run", action="store_true",
                        help="run every check and print the planned changes without writing files or touching services")
    args = parser.parse_args()
    if args.rollback:
        if args.thread_id:
            parser.error("--rollback and --thread-id are mutually exclusive")
        rollback(args)
    elif args.thread_id:
        install(args)
    else:
        parser.error("provide --thread-id or --rollback")


if __name__ == "__main__":
    main()
