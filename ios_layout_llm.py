from __future__ import annotations

import argparse
import copy
import ctypes
import ctypes.util
import hashlib
import json
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from dotenv import load_dotenv
from openai import OpenAI


ROOT = Path(__file__).resolve().parent
DEFAULT_BACKUP_ROOT = ROOT / "backups"
DEFAULT_PLAN = ROOT / "layout_plan.json"
DEFAULT_FOLDER_PLAN = ROOT / "folder_plan.json"

SPRINGBOARD_LAYOUT_CANDIDATES = (
    "Library/SpringBoard/IconState.plist",
    "Library/SpringBoard/DesiredIconState.plist",
)


@dataclass(frozen=True)
class LayoutFile:
    backup_dir: Path
    file_id: str
    domain: str
    relative_path: str
    content_path: Path


@dataclass
class IconItem:
    item_id: str
    label: str
    kind: str
    source: str
    original: Any
    bundle_id: str | None = None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plan and apply iPhone/iPad Home Screen icon layouts using an OpenAI-compatible LLM."
    )
    parser.add_argument(
        "--backend",
        choices=("direct", "backup"),
        default="direct",
        help="Use SpringBoardServices directly, or use the slower backup/restore fallback.",
    )
    parser.add_argument(
        "--connection",
        choices=("auto", "usb", "network", "prefer-network"),
        default="auto",
        help="Direct backend only: choose how libimobiledevice looks up the device.",
    )
    parser.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT)
    parser.add_argument("--udid", default=None)
    parser.add_argument("--page-size", type=int, default=24)
    parser.add_argument("--dock-size", type=int, default=None)
    parser.add_argument(
        "--full-backup",
        action="store_true",
        help="Backup backend only: force a full idevicebackup2 backup.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("plan", help="Read the current layout and ask the LLM for a layout plan.")
    plan_parser.add_argument("--instructions", required=True)
    plan_parser.add_argument("--out", type=Path, default=DEFAULT_PLAN)
    plan_parser.add_argument("--skip-backup", action="store_true", help="Backup backend only: reuse the latest local backup.")

    apply_parser = subparsers.add_parser("apply", help="Apply a saved plan.")
    apply_parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    apply_parser.add_argument("--restore-device", action="store_true", help="Backup backend only: restore after editing the backup.")

    rearrange_parser = subparsers.add_parser("rearrange", help="Plan and apply in one command.")
    rearrange_parser.add_argument("--instructions", required=True)
    rearrange_parser.add_argument("--out", type=Path, default=DEFAULT_PLAN)
    rearrange_parser.add_argument(
        "--restore-device",
        action="store_true",
        help="Backup backend only: restore after editing the backup.",
    )
    rearrange_parser.add_argument("--skip-backup", action="store_true", help="Backup backend only: reuse the latest local backup.")

    inspect_parser = subparsers.add_parser("inspect", help="Print the current top-level icon layout.")
    inspect_parser.add_argument("--skip-backup", action="store_true", help="Backup backend only: reuse the latest local backup.")

    folderize_parser = subparsers.add_parser(
        "folderize",
        help="Move loose apps into existing page-2 folders while keeping page 1 and page-2 folder positions fixed.",
    )
    folderize_parser.add_argument("--instructions", required=True)
    folderize_parser.add_argument("--out", type=Path, default=DEFAULT_FOLDER_PLAN)
    folderize_parser.add_argument("--apply", action="store_true", help="Apply the generated folder assignment plan.")
    folderize_parser.add_argument("--skip-backup", action="store_true", help="Backup backend only: reuse the latest local backup.")

    args = parser.parse_args()

    layout_file: LayoutFile | None = None
    if args.backend == "backup":
        if args.command in {"plan", "rearrange", "inspect"} and not args.skip_backup:
            run_backup(args.backup_root, args.udid, full=args.full_backup)
        backup_dir = latest_backup_dir(args.backup_root)
        layout_file = find_layout_file(backup_dir)
        state = read_layout_state(layout_file)
    else:
        state = DirectSpringBoardClient(args.udid, args.connection).get_icon_state()

    catalog, current_layout = collect_top_level_items(state)
    dock_size = args.dock_size or max(4, len(current_layout["dock"]))

    if args.command == "inspect":
        print_layout_summary(catalog, current_layout)
        return

    if args.command == "folderize":
        context = collect_folderize_context(state)
        plan = request_folder_plan(context, args.instructions)
        validated_plan = validate_folder_plan(plan, context)
        write_json(args.out, validated_plan)
        print(f"Wrote validated folder plan to {args.out}")
        print_folder_plan_summary(validated_plan, context)
        if args.apply:
            updated_state = build_folderized_state(state, validated_plan)
            write_layout(args, layout_file, updated_state)
            verify_folderized_state(state, read_current_state(args, layout_file), validated_plan)
            print("Verified layout after apply.")
        else:
            print("Dry run only. Re-run with --apply to write this plan.")
        return

    if args.command in {"plan", "rearrange"}:
        plan = request_plan(
            catalog=catalog,
            current_layout=current_layout,
            instructions=args.instructions,
            page_size=args.page_size,
            dock_size=dock_size,
        )
        validated_plan = validate_plan(plan, catalog, current_layout, args.page_size, dock_size)
        write_json(args.out, validated_plan)
        print(f"Wrote validated plan to {args.out}")
        print_plan_summary(validated_plan)

        if args.command == "plan":
            return

        updated_state = build_state_from_plan(state, catalog, validated_plan, args.page_size, dock_size)
        write_layout(args, layout_file, updated_state)
        return

    if args.command == "apply":
        plan = read_json(args.plan)
        validated_plan = validate_plan(plan, catalog, current_layout, args.page_size, dock_size)
        updated_state = build_state_from_plan(state, catalog, validated_plan, args.page_size, dock_size)
        write_layout(args, layout_file, updated_state)
        print_plan_summary(validated_plan)


class DirectSpringBoardClient:
    def __init__(self, udid: str | None, connection: str = "auto") -> None:
        self.udid = udid.encode() if udid else None
        self.connection = connection
        self.imobiledevice = load_library("imobiledevice-1.0", "libimobiledevice-1.0.dylib")
        self.plist = load_library("plist-2.0", "libplist-2.0.dylib")
        self._configure_signatures()

    def get_icon_state(self) -> Any:
        device, client = self._connect()
        state = ctypes.c_void_p()
        try:
            err = self.imobiledevice.sbservices_get_icon_state(client, ctypes.byref(state), b"2")
            check_err(err, "sbservices_get_icon_state")
            data = self._plist_to_bin(state)
            parsed = plistlib.loads(data)
            if not isinstance(parsed, (dict, list)):
                raise SystemExit("SpringBoard returned an unsupported icon state shape.")
            return parsed
        finally:
            if state:
                self.plist.plist_free(state)
            self._disconnect(device, client)

    def set_icon_state(self, state: Any) -> None:
        device, client = self._connect()
        plist_node = ctypes.c_void_p()
        data = plistlib.dumps(state, fmt=plistlib.FMT_BINARY, sort_keys=False)
        buf = ctypes.create_string_buffer(data)
        try:
            err = self.plist.plist_from_bin(buf, len(data), ctypes.byref(plist_node))
            check_err(err, "plist_from_bin")
            err = self.imobiledevice.sbservices_set_icon_state(client, plist_node)
            check_err(err, "sbservices_set_icon_state")
        finally:
            if plist_node:
                self.plist.plist_free(plist_node)
            self._disconnect(device, client)

    def _connect(self) -> tuple[ctypes.c_void_p, ctypes.c_void_p]:
        device = ctypes.c_void_p()
        client = ctypes.c_void_p()
        if hasattr(self.imobiledevice, "idevice_new_with_options"):
            err = self.imobiledevice.idevice_new_with_options(
                ctypes.byref(device),
                self.udid,
                connection_options(self.connection),
            )
            check_err(err, "idevice_new_with_options")
        elif self.connection not in {"auto", "usb"}:
            raise SystemExit("This libimobiledevice build does not expose idevice_new_with_options; only USB lookup is available.")
        else:
            err = self.imobiledevice.idevice_new(ctypes.byref(device), self.udid)
            check_err(err, "idevice_new")
        try:
            err = self.imobiledevice.sbservices_client_start_service(
                device,
                ctypes.byref(client),
                b"ios-layout-llm",
            )
            check_err(err, "sbservices_client_start_service")
        except Exception:
            if device:
                self.imobiledevice.idevice_free(device)
            raise
        return device, client

    def _disconnect(self, device: ctypes.c_void_p, client: ctypes.c_void_p) -> None:
        if client:
            self.imobiledevice.sbservices_client_free(client)
        if device:
            self.imobiledevice.idevice_free(device)

    def _plist_to_bin(self, node: ctypes.c_void_p) -> bytes:
        out = ctypes.c_void_p()
        length = ctypes.c_uint32()
        err = self.plist.plist_to_bin(node, ctypes.byref(out), ctypes.byref(length))
        check_err(err, "plist_to_bin")
        try:
            return ctypes.string_at(out, length.value)
        finally:
            if out:
                self.plist.plist_mem_free(out)

    def _configure_signatures(self) -> None:
        self.imobiledevice.idevice_new.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_char_p]
        self.imobiledevice.idevice_new.restype = ctypes.c_int
        if hasattr(self.imobiledevice, "idevice_new_with_options"):
            self.imobiledevice.idevice_new_with_options.argtypes = [
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.c_char_p,
                ctypes.c_int,
            ]
            self.imobiledevice.idevice_new_with_options.restype = ctypes.c_int
        self.imobiledevice.idevice_free.argtypes = [ctypes.c_void_p]
        self.imobiledevice.idevice_free.restype = ctypes.c_int
        self.imobiledevice.sbservices_client_start_service.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_char_p,
        ]
        self.imobiledevice.sbservices_client_start_service.restype = ctypes.c_int
        self.imobiledevice.sbservices_client_free.argtypes = [ctypes.c_void_p]
        self.imobiledevice.sbservices_client_free.restype = ctypes.c_int
        self.imobiledevice.sbservices_get_icon_state.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_char_p,
        ]
        self.imobiledevice.sbservices_get_icon_state.restype = ctypes.c_int
        self.imobiledevice.sbservices_set_icon_state.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.imobiledevice.sbservices_set_icon_state.restype = ctypes.c_int

        self.plist.plist_to_bin.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_uint32),
        ]
        self.plist.plist_to_bin.restype = ctypes.c_int
        self.plist.plist_from_bin.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.plist.plist_from_bin.restype = ctypes.c_int
        self.plist.plist_free.argtypes = [ctypes.c_void_p]
        self.plist.plist_free.restype = None
        self.plist.plist_mem_free.argtypes = [ctypes.c_void_p]
        self.plist.plist_mem_free.restype = None


def load_library(name: str, fallback: str) -> ctypes.CDLL:
    found = ctypes.util.find_library(name)
    candidates = [found, f"/opt/homebrew/lib/{fallback}", f"/usr/local/lib/{fallback}"]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return ctypes.CDLL(candidate)
        except OSError:
            pass
    raise SystemExit(f"Could not load {name}. Is libimobiledevice installed?")


def check_err(err: int, operation: str) -> None:
    if err != 0:
        message = f"{operation} failed with error code {err}"
        if operation.startswith("idevice_new") and err == -3:
            message += (
                "\nNo device was found by libimobiledevice. Check:"
                "\n  - USB: idevice_id -l"
                "\n  - Wi-Fi/network: idevice_id -n"
                "\n  - Pair/trust state: idevicepair validate"
                "\nUnlock the device and tap Trust if prompted. If multiple devices show up, pass --udid <udid>."
            )
        elif operation == "sbservices_client_start_service":
            message += (
                "\nThe device was found, but SpringBoardServices could not start. "
                "Unlock the device, confirm it is paired/trusted, then retry."
                "\nIf `idevice_id -n` sees the device but `idevice_id -l` does not, connect it by USB and retry with:"
                "\n  uv run ios-layout-llm --connection usb inspect"
            )
        raise SystemExit(message)


def connection_options(connection: str) -> int:
    usb = 1 << 1
    network = 1 << 2
    prefer_network = 1 << 3
    if connection == "usb":
        return usb
    if connection == "network":
        return network
    if connection == "prefer-network":
        return usb | network | prefer_network
    return usb | network


def write_layout(args: argparse.Namespace, layout_file: LayoutFile | None, state: Any) -> None:
    if args.backend == "direct":
        DirectSpringBoardClient(args.udid, args.connection).set_icon_state(state)
        print("Applied layout directly to SpringBoardServices; no backup was created.")
        return

    if layout_file is None:
        raise SystemExit("Backup backend selected, but no layout file was loaded.")
    apply_plan_to_backup(layout_file, state)
    print(f"Applied plan to backup file {layout_file.content_path}")
    if args.restore_device:
        restore_backup(args.backup_root, args.udid)
    else:
        print("Device restore skipped. Re-run with --restore-device when ready.")


def read_current_state(args: argparse.Namespace, layout_file: LayoutFile | None) -> Any:
    if args.backend == "direct":
        return DirectSpringBoardClient(args.udid, args.connection).get_icon_state()
    if layout_file is None:
        raise SystemExit("Backup backend selected, but no layout file was loaded.")
    return read_layout_state(layout_file)


def run_backup(backup_root: Path, udid: str | None, full: bool) -> None:
    backup_root.mkdir(parents=True, exist_ok=True)
    cmd = ["idevicebackup2"]
    if udid:
        cmd += ["--udid", udid]
    cmd += ["backup"]
    if full:
        cmd += ["--full"]
    cmd.append(str(backup_root))
    run(cmd)


def restore_backup(backup_root: Path, udid: str | None) -> None:
    cmd = ["idevicebackup2"]
    if udid:
        cmd += ["--udid", udid]
    cmd += ["restore", "--system", "--settings", "--skip-apps", str(backup_root)]
    run(cmd)


def run(cmd: list[str]) -> None:
    print("+ " + " ".join(cmd))
    subprocess.run(cmd, check=True)


def latest_backup_dir(backup_root: Path) -> Path:
    if (backup_root / "Manifest.db").exists():
        return backup_root

    candidates = [path for path in backup_root.iterdir() if (path / "Manifest.db").exists()]
    if not candidates:
        raise SystemExit(f"No iOS backup with Manifest.db found under {backup_root}")
    return max(candidates, key=lambda path: (path / "Manifest.db").stat().st_mtime)


def find_layout_file(backup_dir: Path) -> LayoutFile:
    manifest = backup_dir / "Manifest.db"
    con = sqlite3.connect(manifest)
    try:
        rows = con.execute(
            """
            SELECT fileID, domain, relativePath
            FROM Files
            WHERE domain = 'HomeDomain'
              AND (
                relativePath IN (?, ?)
                OR relativePath LIKE 'Library/SpringBoard/%IconState%.plist'
              )
            ORDER BY
              CASE relativePath
                WHEN ? THEN 0
                WHEN ? THEN 1
                ELSE 2
              END
            """,
            (
                SPRINGBOARD_LAYOUT_CANDIDATES[0],
                SPRINGBOARD_LAYOUT_CANDIDATES[1],
                SPRINGBOARD_LAYOUT_CANDIDATES[0],
                SPRINGBOARD_LAYOUT_CANDIDATES[1],
            ),
        ).fetchall()
    finally:
        con.close()

    for file_id, domain, relative_path in rows:
        content_path = backup_dir / file_id[:2] / file_id
        if content_path.exists():
            return LayoutFile(
                backup_dir=backup_dir,
                file_id=file_id,
                domain=domain,
                relative_path=relative_path,
                content_path=content_path,
            )

    raise SystemExit("Could not find a SpringBoard IconState plist in the backup.")


def read_layout_state(layout_file: LayoutFile) -> dict[str, Any]:
    with layout_file.content_path.open("rb") as fh:
        state = plistlib.load(fh)
    if not isinstance(state, dict):
        raise SystemExit(f"Unsupported layout plist shape in {layout_file.content_path}")
    if "iconLists" not in state or "buttonBar" not in state:
        keys = ", ".join(sorted(str(key) for key in state.keys()))
        raise SystemExit(f"Layout plist does not contain expected iconLists/buttonBar keys. Found: {keys}")
    return state


def collect_top_level_items(state: Any) -> tuple[dict[str, IconItem], dict[str, list[str] | list[list[str]]]]:
    catalog: dict[str, IconItem] = {}
    dock: list[str] = []
    pages: list[list[str]] = []

    dock_container, page_containers = split_icon_state(state)

    for item in flatten_icon_container(dock_container):
        icon = make_icon_item(item, "dock", catalog)
        catalog[icon.item_id] = icon
        dock.append(icon.item_id)

    for page_index, page in enumerate(page_containers, start=1):
        page_ids: list[str] = []
        for item in flatten_icon_container(page):
            icon = make_icon_item(item, f"page-{page_index}", catalog)
            catalog[icon.item_id] = icon
            page_ids.append(icon.item_id)
        pages.append(page_ids)

    return catalog, {"dock": dock, "pages": pages}


def split_icon_state(state: Any) -> tuple[Any, list[Any]]:
    if isinstance(state, dict):
        return state.get("buttonBar", []), state.get("iconLists", [])
    if isinstance(state, list):
        return (state[0] if state else []), list(state[1:])
    raise SystemExit(f"Unsupported icon state shape: {type(state).__name__}")


def flatten_icon_container(container: Any) -> list[Any]:
    if not isinstance(container, list):
        return []
    if all(is_icon_leaf(item) for item in container):
        return container

    flattened: list[Any] = []
    for item in container:
        if is_icon_leaf(item):
            flattened.append(item)
        elif isinstance(item, list):
            flattened.extend(flatten_icon_container(item))
    return flattened


def is_icon_leaf(item: Any) -> bool:
    if isinstance(item, str):
        return True
    if isinstance(item, dict):
        return "iconLists" in item or any(
            key in item
            for key in (
                "bundleIdentifier",
                "displayIdentifier",
                "applicationIdentifier",
                "webClipIdentifier",
                "displayName",
            )
        )
    return False


def make_icon_item(item: Any, source: str, existing: dict[str, IconItem]) -> IconItem:
    if isinstance(item, str):
        base_id = item
        label = prettify_identifier(item)
        kind = "app"
        bundle_id = item
    elif isinstance(item, dict):
        bundle_id = first_text(
            item,
            "bundleIdentifier",
            "displayIdentifier",
            "applicationIdentifier",
            "webClipIdentifier",
            "uniqueIdentifier",
        )
        label = first_text(item, "displayName", "name", "title") or prettify_identifier(bundle_id or "folder")
        kind = "folder" if "iconLists" in item else "app"
        if item.get("iconType") == "custom":
            kind = "custom"
        if kind == "folder":
            base_id = f"folder:{label}"
        else:
            base_id = bundle_id or stable_id_for_item(item, label)
    else:
        raise ValueError(f"Unsupported icon item: {item!r}")

    item_id = base_id
    suffix = 2
    while item_id in existing:
        item_id = f"{base_id}#{suffix}"
        suffix += 1

    return IconItem(item_id=item_id, label=label, kind=kind, source=source, original=item, bundle_id=bundle_id)


def first_text(mapping: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def prettify_identifier(identifier: str | None) -> str:
    if not identifier:
        return "Unknown"
    tail = identifier.split(".")[-1].replace("-", " ").replace("_", " ")
    return tail[:1].upper() + tail[1:]


def stable_id_for_item(item: dict[str, Any], label: str) -> str:
    digest = hashlib.sha1(plistlib.dumps(item, sort_keys=True)).hexdigest()[:8]
    return f"{label.lower().replace(' ', '-')}-{digest}"


def request_plan(
    catalog: dict[str, IconItem],
    current_layout: dict[str, Any],
    instructions: str,
    page_size: int,
    dock_size: int,
) -> dict[str, Any]:
    load_dotenv(ROOT / ".env")
    base_url = os.getenv("OPENAI_BASE_URL")
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL")
    if not api_key or not model:
        raise SystemExit("OPENAI_API_KEY and OPENAI_MODEL must be set in .env")

    client = OpenAI(api_key=api_key, base_url=normalize_openai_base_url(base_url))
    prompt = build_prompt(catalog, current_layout, instructions, page_size, dock_size)
    messages = [
        {
            "role": "system",
            "content": (
                "You organize iPhone and iPad Home Screen layouts. Return only valid JSON. "
                "Use only the exact item_id values provided. Do not invent app IDs. "
                "Existing folders may be moved as units; do not alter folder contents."
            ),
        },
        {"role": "user", "content": prompt},
    ]

    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.2,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        print(f"Structured JSON request failed, retrying without response_format: {exc}", file=sys.stderr)
        response = client.chat.completions.create(model=model, messages=messages, temperature=0.2)

    content = extract_llm_content(response)
    if not content:
        raise SystemExit("LLM returned an empty response.")
    return parse_json_response(content)


def build_prompt(
    catalog: dict[str, IconItem],
    current_layout: dict[str, Any],
    instructions: str,
    page_size: int,
    dock_size: int,
) -> str:
    items = [
        {
            "item_id": icon.item_id,
            "label": icon.label,
            "kind": icon.kind,
            "bundle_id": icon.bundle_id,
            "current_location": icon.source,
        }
        for icon in catalog.values()
    ]
    payload = {
        "user_instructions": instructions,
        "constraints": {
            "dock_max_items": dock_size,
            "page_max_items": page_size,
            "use_each_item_at_most_once": True,
            "unknown_or_omitted_items_are_allowed_but_should_be_minimized": True,
            "preserve_existing_folder_contents": True,
        },
        "current_layout": current_layout,
        "available_items": items,
        "required_output_schema": {
            "dock": ["item_id"],
            "pages": [["item_id"]],
            "notes": ["short rationale or caveat"],
        },
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def parse_json_response(content: str) -> dict[str, Any]:
    stripped = content.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        result = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"LLM did not return valid JSON: {exc}\n\n{content}") from exc
    if not isinstance(result, dict):
        raise SystemExit("LLM JSON response must be an object.")
    return result


def extract_llm_content(response: Any) -> str | None:
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        try:
            return response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            content = response.get("content")
            return content if isinstance(content, str) else None
    choices = getattr(response, "choices", None)
    if choices:
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None)
        if isinstance(content, str):
            return content
    return None


def normalize_openai_base_url(base_url: str | None) -> str | None:
    if not base_url:
        return None
    parsed = urlparse(base_url)
    path = parsed.path.rstrip("/")
    if path in {"", "/"}:
        path = "/v1"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def validate_plan(
    plan: dict[str, Any],
    catalog: dict[str, IconItem],
    current_layout: dict[str, Any],
    page_size: int,
    dock_size: int,
) -> dict[str, Any]:
    dock = require_string_list(plan.get("dock", []), "dock")[:dock_size]
    pages_raw = plan.get("pages", [])
    if not isinstance(pages_raw, list):
        raise SystemExit("Plan field 'pages' must be a list of pages.")

    known = set(catalog)
    used: set[str] = set()
    clean_dock: list[str] = []
    clean_pages: list[list[str]] = []
    warnings: list[str] = []

    for item_id in dock:
        if item_id not in known:
            warnings.append(f"Ignored unknown dock item: {item_id}")
            continue
        if item_id in used:
            warnings.append(f"Ignored duplicate dock item: {item_id}")
            continue
        used.add(item_id)
        clean_dock.append(item_id)

    for page_index, page in enumerate(pages_raw, start=1):
        page_items = require_string_list(page, f"pages[{page_index - 1}]")
        clean_page: list[str] = []
        for item_id in page_items:
            if item_id not in known:
                warnings.append(f"Ignored unknown page item: {item_id}")
                continue
            if item_id in used:
                warnings.append(f"Ignored duplicate page item: {item_id}")
                continue
            used.add(item_id)
            clean_page.append(item_id)
            if len(clean_page) >= page_size:
                break
        if clean_page:
            clean_pages.append(clean_page)

    original_order = list(current_layout["dock"])
    for page in current_layout["pages"]:
        original_order.extend(page)

    leftovers = [item_id for item_id in original_order if item_id not in used]
    if leftovers:
        warnings.append(f"Appended {len(leftovers)} omitted item(s) in original order.")
    for item_id in leftovers:
        if len(clean_pages) == 0 or len(clean_pages[-1]) >= page_size:
            clean_pages.append([])
        clean_pages[-1].append(item_id)

    return {
        "dock": clean_dock,
        "pages": clean_pages,
        "notes": require_string_list(plan.get("notes", []), "notes", allow_non_strings=True),
        "warnings": warnings,
    }


def collect_folderize_context(state: Any) -> dict[str, Any]:
    _, pages = split_icon_state(state)
    if len(pages) < 2:
        raise SystemExit("Folderize requires at least two Home Screen pages.")

    existing: dict[str, IconItem] = {}
    page1_ids: list[str] = []
    page2_folder_ids: list[str] = []
    target_folders: dict[str, IconItem] = {}
    movable_apps: dict[str, IconItem] = {}
    preserved_after_page2: list[str] = []

    for item in flatten_icon_container(pages[0]):
        icon = make_icon_item(item, "page-1", existing)
        existing[icon.item_id] = icon
        page1_ids.append(icon.item_id)

    for item in flatten_icon_container(pages[1]):
        icon = make_icon_item(item, "page-2", existing)
        existing[icon.item_id] = icon
        if icon.kind == "folder":
            page2_folder_ids.append(icon.item_id)
            target_folders[icon.item_id] = icon
        else:
            movable_apps[icon.item_id] = icon

    for page_index, page in enumerate(pages[2:], start=3):
        for item in flatten_icon_container(page):
            icon = make_icon_item(item, f"page-{page_index}", existing)
            existing[icon.item_id] = icon
            if icon.kind == "folder":
                preserved_after_page2.append(icon.item_id)
            else:
                movable_apps[icon.item_id] = icon

    return {
        "page1_ids": page1_ids,
        "page2_folder_ids": page2_folder_ids,
        "target_folders": target_folders,
        "movable_apps": movable_apps,
        "preserved_after_page2": preserved_after_page2,
    }


def request_folder_plan(context: dict[str, Any], instructions: str) -> dict[str, Any]:
    load_dotenv(ROOT / ".env")
    base_url = os.getenv("OPENAI_BASE_URL")
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL")
    if not api_key or not model:
        raise SystemExit("OPENAI_API_KEY and OPENAI_MODEL must be set in .env")

    folders = [
        {
            "folder_id": folder_id,
            "label": folder.label,
            "existing_examples": folder_content_labels(folder.original)[:25],
            "existing_count": len(folder_content_labels(folder.original)),
        }
        for folder_id, folder in context["target_folders"].items()
    ]
    apps = [
        {
            "item_id": item_id,
            "label": app.label,
            "bundle_id": app.bundle_id,
            "current_location": app.source,
        }
        for item_id, app in context["movable_apps"].items()
    ]
    payload = {
        "instructions": instructions,
        "fixed_rules": [
            "Do not move or change page 1.",
            "Do not move page 2 folders; they are the only allowed destination folders.",
            "Assign a loose app to a folder only when the folder name or existing examples are a clear match.",
            "If no specific folder is suitable and a folder named 其他 exists, use 其他 as the fallback.",
            "Avoid null. SpringBoardServices does not reliably hide omitted apps; omitted apps can be auto-filled back onto the Home Screen.",
            "Do not invent folders or app ids.",
        ],
        "destination_folders": folders,
        "loose_apps_to_classify": apps,
        "required_output_schema": {
            "assignments": [
                {
                    "item_id": "exact app item_id",
                    "target_folder_id": "exact folder_id or null",
                    "reason": "brief reason",
                }
            ],
            "notes": ["optional caveats"],
        },
    }

    client = OpenAI(api_key=api_key, base_url=normalize_openai_base_url(base_url))
    messages = [
        {
            "role": "system",
            "content": (
                "You classify iPhone Home Screen apps into existing folders. "
                "Return only valid JSON. Use only exact ids from the prompt. "
                "When unsure, use null."
            ),
        },
        {"role": "user", "content": json.dumps(payload, indent=2, ensure_ascii=False)},
    ]
    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.1,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        print(f"Structured JSON request failed, retrying without response_format: {exc}", file=sys.stderr)
        response = client.chat.completions.create(model=model, messages=messages, temperature=0.1)

    content = extract_llm_content(response)
    if not content:
        raise SystemExit("LLM returned an empty response.")
    return parse_json_response(content)


def validate_folder_plan(plan: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    assignments_raw = plan.get("assignments", [])
    if not isinstance(assignments_raw, list):
        raise SystemExit("Folder plan field 'assignments' must be a list.")

    movable_ids = set(context["movable_apps"])
    folder_ids = set(context["target_folders"])
    fallback_folder_id = "folder:其他" if "folder:其他" in folder_ids else None
    assignments: dict[str, str | None] = {}
    reasons: dict[str, str] = {}
    warnings: list[str] = []

    for row in assignments_raw:
        if not isinstance(row, dict):
            warnings.append(f"Ignored non-object assignment: {row!r}")
            continue
        item_id = row.get("item_id")
        target = row.get("target_folder_id")
        if item_id not in movable_ids:
            warnings.append(f"Ignored unknown or fixed item: {item_id}")
            continue
        if target is None and fallback_folder_id is not None:
            target = fallback_folder_id
            warnings.append(f"Changed null target for {item_id} to {fallback_folder_id}; direct SpringBoard writes cannot hide omitted apps.")
        if target is not None and target not in folder_ids:
            warnings.append(f"Changed invalid target for {item_id} to null: {target}")
            target = fallback_folder_id
        assignments[item_id] = target
        reason = row.get("reason")
        if isinstance(reason, str):
            reasons[item_id] = reason

    for item_id in movable_ids:
        assignments.setdefault(item_id, fallback_folder_id)

    return {
        "assignments": [
            {
                "item_id": item_id,
                "target_folder_id": target,
                "reason": reasons.get(item_id, ""),
            }
            for item_id, target in assignments.items()
        ],
        "notes": require_string_list(plan.get("notes", []), "notes", allow_non_strings=True),
        "warnings": warnings,
    }


def build_folderized_state(state: Any, plan: dict[str, Any]) -> Any:
    updated = copy.deepcopy(state)
    dock, pages = split_icon_state(updated)
    if len(pages) < 2:
        raise SystemExit("Folderize requires at least two Home Screen pages.")

    context = collect_folderize_context(updated)
    target_folders = context["target_folders"]
    movable_apps = context["movable_apps"]
    page2_folder_ids = context["page2_folder_ids"]

    for row in plan["assignments"]:
        item_id = row["item_id"]
        target_id = row["target_folder_id"]
        if target_id is None:
            continue
        append_to_folder(target_folders[target_id].original, movable_apps[item_id].original)

    new_page2 = [target_folders[folder_id].original for folder_id in page2_folder_ids]
    new_pages = [pages[0], adapt_container_shape(pages[1], new_page2)]

    if isinstance(updated, dict):
        updated["buttonBar"] = dock
        updated["iconLists"] = new_pages
        return updated
    if isinstance(updated, list):
        return [dock, *new_pages]
    raise SystemExit(f"Unsupported icon state shape: {type(updated).__name__}")


def append_to_folder(folder: dict[str, Any], item: Any) -> None:
    icon_lists = folder.get("iconLists")
    if not isinstance(icon_lists, list):
        icon_lists = []

    flat_items: list[Any] = []
    page_lengths: list[int] = []
    for page in icon_lists:
        page_items = flatten_icon_container(page)
        page_lengths.append(len(page_items))
        flat_items.extend(page_items)

    flat_items.append(item)
    capacity = max([length for length in page_lengths if length > 0] + [9])
    folder["iconLists"] = [flat_items[index : index + capacity] for index in range(0, len(flat_items), capacity)]


def folder_content_labels(folder: Any) -> list[str]:
    if not isinstance(folder, dict):
        return []
    labels: list[str] = []
    for page in folder.get("iconLists", []):
        for item in flatten_icon_container(page):
            labels.append(label_for_item(item))
    return labels


def label_for_item(item: Any) -> str:
    if isinstance(item, str):
        return prettify_identifier(item)
    if isinstance(item, dict):
        return first_text(item, "displayName", "name", "title") or prettify_identifier(
            first_text(
                item,
                "bundleIdentifier",
                "displayIdentifier",
                "applicationIdentifier",
                "webClipIdentifier",
                "uniqueIdentifier",
            )
        )
    return "Unknown"


def print_folder_plan_summary(plan: dict[str, Any], context: dict[str, Any]) -> None:
    folder_labels = {folder_id: folder.label for folder_id, folder in context["target_folders"].items()}
    app_labels = {item_id: app.label for item_id, app in context["movable_apps"].items()}
    assigned = [row for row in plan["assignments"] if row["target_folder_id"] is not None]
    removed = [row for row in plan["assignments"] if row["target_folder_id"] is None]
    print(f"Assignments: {len(assigned)} app(s) into folders; {len(removed)} app(s) removed from Home Screen.")
    for row in assigned:
        print(f"  - {app_labels[row['item_id']]} -> {folder_labels[row['target_folder_id']]}")
    if removed:
        print("Removed from Home Screen:")
        print("  " + ", ".join(app_labels[row["item_id"]] for row in removed))
    for warning in plan.get("warnings", []):
        print(f"Warning: {warning}")
    for note in plan.get("notes", []):
        print(f"Note: {note}")


def verify_folderized_state(before: Any, after: Any, plan: dict[str, Any]) -> None:
    before_context = collect_folderize_context(before)
    after_context = collect_folderize_context(after)
    _, before_pages = split_icon_state(before)
    _, after_pages = split_icon_state(after)
    if not before_pages or not after_pages or visible_page_signature(before_pages[0]) != visible_page_signature(after_pages[0]):
        raise SystemExit("Verification failed: page 1 changed.")
    before_folder_order = [context_folder_label(before_context, folder_id) for folder_id in before_context["page2_folder_ids"]]
    after_folder_order = [context_folder_label(after_context, folder_id) for folder_id in after_context["page2_folder_ids"]]
    if before_folder_order != after_folder_order:
        raise SystemExit("Verification failed: page 2 folder order changed.")

    folder_items_after = {
        folder_id: set(folder_content_item_ids(folder.original))
        for folder_id, folder in after_context["target_folders"].items()
    }
    all_after_home_ids = set(after_context["page1_ids"]) | set(after_context["page2_folder_ids"])
    for folder_item_ids in folder_items_after.values():
        all_after_home_ids.update(folder_item_ids)

    for row in plan["assignments"]:
        item_id = row["item_id"]
        target_id = row["target_folder_id"]
        if target_id is None:
            if item_id in all_after_home_ids:
                raise SystemExit(f"Verification failed: {item_id} should have been removed from Home Screen.")
        elif item_id not in folder_items_after.get(target_id, set()):
            raise SystemExit(f"Verification failed: {item_id} was not found in target folder {target_id}.")


def visible_page_signature(page: Any) -> list[tuple[str, str]]:
    signature: list[tuple[str, str]] = []
    for item in flatten_icon_container(page):
        if isinstance(item, dict) and "iconLists" in item:
            kind = "custom" if item.get("iconType") == "custom" else "folder"
        else:
            kind = "app"
        signature.append((kind, label_for_item(item)))
    return signature


def context_folder_label(context: dict[str, Any], folder_id: str) -> str:
    folder = context["target_folders"].get(folder_id)
    return folder.label if folder else folder_id


def folder_content_item_ids(folder: Any) -> list[str]:
    if not isinstance(folder, dict):
        return []
    existing: dict[str, IconItem] = {}
    ids: list[str] = []
    for page in folder.get("iconLists", []):
        for item in flatten_icon_container(page):
            icon = make_icon_item(item, "folder", existing)
            existing[icon.item_id] = icon
            ids.append(icon.item_id)
    return ids


def require_string_list(value: Any, field: str, allow_non_strings: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise SystemExit(f"Plan field '{field}' must be a list.")
    result: list[str] = []
    for item in value:
        if isinstance(item, str):
            result.append(item)
        elif allow_non_strings:
            result.append(str(item))
        else:
            raise SystemExit(f"Plan field '{field}' must contain only strings.")
    return result


def build_state_from_plan(
    state: Any,
    catalog: dict[str, IconItem],
    plan: dict[str, Any],
    page_size: int,
    dock_size: int,
) -> Any:
    updated = copy.deepcopy(state)
    item_by_id = {item_id: icon.original for item_id, icon in catalog.items()}
    new_dock = [item_by_id[item_id] for item_id in plan["dock"][:dock_size]]
    new_pages = [[item_by_id[item_id] for item_id in page[:page_size]] for page in plan["pages"]]

    if isinstance(updated, dict):
        updated["buttonBar"] = adapt_container_shape(updated.get("buttonBar", []), new_dock)
        updated["iconLists"] = adapt_pages_shape(updated.get("iconLists", []), new_pages)
        return updated

    if isinstance(updated, list):
        dock_container = updated[0] if updated else []
        page_containers = updated[1:] if len(updated) > 1 else []
        return [adapt_container_shape(dock_container, new_dock), *adapt_pages_shape(page_containers, new_pages)]

    raise SystemExit(f"Unsupported icon state shape: {type(updated).__name__}")


def apply_plan_to_backup(layout_file: LayoutFile, state: Any) -> None:
    backup_path = layout_file.content_path.with_suffix(layout_file.content_path.suffix + f".{int(time.time())}.bak")
    shutil.copy2(layout_file.content_path, backup_path)
    with layout_file.content_path.open("wb") as fh:
        plistlib.dump(state, fh, fmt=plistlib.FMT_BINARY, sort_keys=False)

    update_manifest_metadata(layout_file)
    print(f"Created backup of original layout file at {backup_path}")


def adapt_pages_shape(existing_pages: Any, new_pages: list[list[Any]]) -> list[Any]:
    if not isinstance(existing_pages, list) or not existing_pages:
        return new_pages
    sample_page = next((page for page in existing_pages if isinstance(page, list) and page), existing_pages[0])
    return [adapt_container_shape(sample_page, page) for page in new_pages]


def adapt_container_shape(existing_container: Any, flat_items: list[Any]) -> list[Any]:
    if not isinstance(existing_container, list):
        return flat_items
    if not existing_container or all(is_icon_leaf(item) for item in existing_container):
        return flat_items

    row_lengths = [len(row) for row in existing_container if isinstance(row, list)]
    if not row_lengths:
        return flat_items

    rows: list[list[Any]] = []
    index = 0
    for row_length in row_lengths:
        if index >= len(flat_items):
            break
        rows.append(flat_items[index : index + row_length])
        index += row_length
    while index < len(flat_items):
        row_length = row_lengths[-1]
        rows.append(flat_items[index : index + row_length])
        index += row_length
    return rows


def update_manifest_metadata(layout_file: LayoutFile) -> None:
    manifest = layout_file.backup_dir / "Manifest.db"
    try:
        con = sqlite3.connect(manifest)
        row = con.execute("SELECT file FROM Files WHERE fileID = ?", (layout_file.file_id,)).fetchone()
        if not row or row[0] is None:
            return
        metadata = plistlib.loads(row[0])
        if not isinstance(metadata, dict):
            return

        stat = layout_file.content_path.stat()
        changed = False
        for key in ("Size", "size"):
            if key in metadata:
                metadata[key] = stat.st_size
                changed = True
        for key in ("LastModified", "LastStatusChange", "Birth"):
            if key in metadata:
                metadata[key] = int(stat.st_mtime)
                changed = True
        if "Digest" in metadata:
            metadata["Digest"] = hashlib.sha1(layout_file.content_path.read_bytes()).digest()
            changed = True
        if changed:
            con.execute(
                "UPDATE Files SET file = ? WHERE fileID = ?",
                (plistlib.dumps(metadata, fmt=plistlib.FMT_BINARY, sort_keys=False), layout_file.file_id),
            )
            con.commit()
    except Exception as exc:
        print(f"Warning: could not update Manifest.db metadata: {exc}", file=sys.stderr)
    finally:
        try:
            con.close()
        except Exception:
            pass


def print_layout_summary(catalog: dict[str, IconItem], current_layout: dict[str, Any]) -> None:
    print("Dock:")
    for item_id in current_layout["dock"]:
        print(f"  - {describe_item(catalog[item_id])}")
    for index, page in enumerate(current_layout["pages"], start=1):
        print(f"Page {index}:")
        for item_id in page:
            print(f"  - {describe_item(catalog[item_id])}")


def print_plan_summary(plan: dict[str, Any]) -> None:
    print("Dock:")
    for item_id in plan["dock"]:
        print(f"  - {item_id}")
    for index, page in enumerate(plan["pages"], start=1):
        print(f"Page {index}: {len(page)} item(s)")
        print("  " + ", ".join(page))
    for warning in plan.get("warnings", []):
        print(f"Warning: {warning}")
    for note in plan.get("notes", []):
        print(f"Note: {note}")


def describe_item(icon: IconItem) -> str:
    bundle = f" ({icon.bundle_id})" if icon.bundle_id and icon.bundle_id != icon.item_id else ""
    return f"{icon.label} [{icon.kind}] id={icon.item_id}{bundle}"


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        value = json.load(fh)
    if not isinstance(value, dict):
        raise SystemExit(f"{path} must contain a JSON object.")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(value, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


if __name__ == "__main__":
    main()
