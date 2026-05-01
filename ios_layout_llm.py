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
        "--connection",
        choices=("auto", "usb", "network", "prefer-network"),
        default="auto",
        help="Choose how libimobiledevice looks up the device for direct plan/apply.",
    )
    parser.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT)
    parser.add_argument("--udid", default=None)
    parser.add_argument("--page-size", type=int, default=None)
    parser.add_argument("--folder-page-size", type=int, default=9)
    parser.add_argument("--max-folder-pages", type=int, default=15)
    parser.add_argument("--dock-size", type=int, default=None)
    parser.add_argument(
        "--full-backup",
        action="store_true",
        help="Backup command only: force a full idevicebackup2 backup.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("plan", help="Read the current layout and ask the LLM for a full layout plan.")
    plan_parser.add_argument("--instructions", required=True)
    plan_parser.add_argument("--out", type=Path, default=DEFAULT_PLAN)

    apply_parser = subparsers.add_parser("apply", help="Apply a saved full layout plan to the connected device.")
    apply_parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    apply_parser.add_argument("--dry-run", action="store_true", help="Validate and summarize the plan without writing to the device.")

    subparsers.add_parser("backup", help="Create an idevicebackup2 backup for fallback workflows.")

    apply_backup_parser = subparsers.add_parser("apply-backup", help="Apply a saved full layout plan to the latest local backup.")
    apply_backup_parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    apply_backup_parser.add_argument("--restore-device", action="store_true", help="Restore the edited backup to the device.")

    args = parser.parse_args()

    if args.command == "backup":
        run_backup(args.backup_root, args.udid, full=args.full_backup)
        return

    if args.command == "plan":
        state = DirectSpringBoardClient(args.udid, args.connection).get_icon_state()
        context = collect_full_layout_context(state)
        plan = request_full_layout_plan(
            context=context,
            instructions=args.instructions,
            page_size=args.page_size,
            dock_size=args.dock_size or max(4, len(context["current_layout"]["dock"])),
        )
        page_size = args.page_size or infer_page_size(context)
        validated_plan = validate_full_layout_plan(
            plan,
            context,
            page_size,
            args.folder_page_size,
            args.max_folder_pages,
            args.dock_size,
            args.instructions,
        )
        write_json(args.out, validated_plan)
        print(f"Wrote validated plan to {args.out}")
        print_full_layout_plan_summary(validated_plan, context)
        return

    if args.command == "apply":
        state = DirectSpringBoardClient(args.udid, args.connection).get_icon_state()
        context = collect_full_layout_context(state)
        plan = read_json(args.plan)
        page_size = args.page_size or infer_page_size(context)
        validated_plan = validate_full_layout_plan(
            plan,
            context,
            page_size,
            args.folder_page_size,
            args.max_folder_pages,
            args.dock_size,
        )
        print_full_layout_plan_summary(validated_plan, context)
        if args.dry_run:
            print("Dry run only. No device changes were made.")
            return
        updated_state = build_full_layout_state(state, validated_plan, context)
        DirectSpringBoardClient(args.udid, args.connection).set_icon_state(updated_state)
        verify_full_layout_applied(validated_plan, DirectSpringBoardClient(args.udid, args.connection).get_icon_state())
        print("Applied and verified layout on device.")
        return

    if args.command == "apply-backup":
        backup_dir = latest_backup_dir(args.backup_root)
        layout_file = find_layout_file(backup_dir)
        state = read_layout_state(layout_file)
        context = collect_full_layout_context(state)
        plan = read_json(args.plan)
        page_size = args.page_size or infer_page_size(context)
        validated_plan = validate_full_layout_plan(
            plan,
            context,
            page_size,
            args.folder_page_size,
            args.max_folder_pages,
            args.dock_size,
        )
        print_full_layout_plan_summary(validated_plan, context)
        updated_state = build_full_layout_state(state, validated_plan, context)
        apply_plan_to_backup(layout_file, updated_state)
        print(f"Applied plan to backup file {layout_file.content_path}")
        if args.restore_device:
            restore_backup(args.backup_root, args.udid)
        else:
            print("Backup restore skipped. Re-run with --restore-device when ready.")
        return


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
                b"ios-home-layout",
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
                "\n  uv run ios-home-layout --connection usb plan --instructions \"Read the current layout and keep it unchanged.\""
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


def collect_full_layout_context(state: Any) -> dict[str, Any]:
    dock_container, page_containers = split_icon_state(state)
    existing: dict[str, IconItem] = {}
    catalog: dict[str, IconItem] = {}
    folder_templates: list[dict[str, Any]] = []

    def icon_ref(icon: IconItem) -> dict[str, str]:
        return {"type": "widget" if icon.kind == "custom" else "app", "item_id": icon.item_id}

    def scan_folder(folder: dict[str, Any], source: str) -> list[dict[str, str]]:
        folder_items: list[dict[str, str]] = []
        for page in folder.get("iconLists", []):
            for child in flatten_icon_container(page):
                child_icon = make_icon_item(child, source, existing)
                if child_icon.kind == "folder":
                    continue
                catalog[child_icon.item_id] = child_icon
                folder_items.append(icon_ref(child_icon))
        return folder_items

    def scan_top_container(container: Any, source: str) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        for item in flatten_icon_container(container):
            icon = make_icon_item(item, source, existing)
            if icon.kind == "folder":
                if isinstance(item, dict):
                    folder_templates.append(item)
                    refs.append(
                        {
                            "type": "folder",
                            "folder_id": icon.item_id,
                            "name": icon.label,
                            "items": scan_folder(item, f"{source}/{icon.label}"),
                        }
                    )
                continue
            catalog[icon.item_id] = icon
            refs.append(icon_ref(icon))
        return refs

    current_layout = {
        "dock": scan_top_container(dock_container, "dock"),
        "pages": [scan_top_container(page, f"page-{index}") for index, page in enumerate(page_containers, start=1)],
    }
    return {
        "catalog": catalog,
        "current_layout": current_layout,
        "folder_template": copy.deepcopy(folder_templates[0]) if folder_templates else None,
    }


def infer_page_size(context: dict[str, Any]) -> int:
    lengths = [len(page) for page in context["current_layout"].get("pages", []) if page]
    return max(lengths + [24])


def request_full_layout_plan(
    context: dict[str, Any],
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

    catalog: dict[str, IconItem] = context["catalog"]
    available_items = [
        {
            "item_id": item_id,
            "label": icon.label,
            "kind": icon.kind,
            "bundle_id": icon.bundle_id,
            "current_location": icon.source,
        }
        for item_id, icon in catalog.items()
    ]
    effective_instructions = normalize_user_instructions(instructions)
    payload = {
        "instructions": effective_instructions,
        "constraints": {
            "dock_max_items": dock_size,
            "page_max_items": page_size,
            "folder_page_size": 9,
            "emit_final_desired_layout": True,
            "use_each_item_id_at_most_once": True,
            "include_every_item_id_somewhere": True,
            "folders_may_be_created_removed_or_renamed": True,
            "apps_may_move_into_or_out_of_folders": True,
            "widgets_or_custom_items_must_remain_top_level": True,
            "do_not_use_null_to_hide_apps": True,
            "do_not_put_unrelated_apps_in_wallet_or_finance_folders_unless_they_are_payment_banking_finance_apps": True,
            "do_not_dump_unrelated_apps_into_one_folder": True,
            "when_user_asks_to_keep_first_page_preserve_it_exactly": True,
            "if_existing_folders_do_not_fit_apps_create_a_sensible_misc_folder_or_leave_overflow_on_later_pages": True,
        },
        "current_layout": context["current_layout"],
        "available_items": available_items,
        "required_output_schema": {
            "dock": [
                {"type": "app", "item_id": "exact item_id"},
                {"type": "folder", "name": "Folder name", "items": ["exact item_id"]},
            ],
            "pages": [
                [
                    {"type": "app", "item_id": "exact item_id"},
                    {"type": "widget", "item_id": "exact custom/widget item_id"},
                    {"type": "folder", "name": "Folder name", "items": ["exact item_id"]},
                ]
            ],
            "notes": ["optional caveats"],
        },
    }

    client = OpenAI(api_key=api_key, base_url=normalize_openai_base_url(base_url))
    messages = [
        {
            "role": "system",
            "content": (
                "You generate complete iPhone/iPad Home Screen layouts. Return only valid JSON. "
                "Use only exact item_id values from the prompt. Folders are described by name and item_id contents. "
                "Do not invent app ids. Do not omit apps to hide them."
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


def normalize_user_instructions(instructions: str) -> str:
    lowered = instructions.lower()
    extra: list[str] = []
    if "remove" in lowered and ("desktop" in lowered or "home screen" in lowered):
        extra.append(
            "Important implementation note: SpringBoardServices cannot reliably hide installed apps by omitting them. "
            "For any app the user says to remove from the desktop/Home Screen, put it into a catch-all folder named Other or Unsorted instead."
        )
    if "one page" in lowered or "only one page" in lowered or "只要一页" in instructions:
        extra.append(
            "Keep the result to one Home Screen page when possible by putting apps into folders. "
            "If folder capacity is exceeded, create an Other/Unsorted folder before spilling to another page."
        )
    if not extra:
        return instructions
    return instructions + "\n\n" + "\n".join(extra)


def validate_full_layout_plan(
    plan: dict[str, Any],
    context: dict[str, Any],
    page_size: int,
    folder_page_size: int,
    max_folder_pages: int,
    dock_size: int | None,
    instructions: str = "",
) -> dict[str, Any]:
    catalog: dict[str, IconItem] = context["catalog"]
    used: set[str] = set()
    warnings: list[str] = []
    max_dock = dock_size or max(4, len(context["current_layout"]["dock"]))

    def normalize_item_id(raw: Any, location: str, allow_widget: bool) -> str | None:
        item_id: Any
        if isinstance(raw, str):
            item_id = raw
        elif isinstance(raw, dict):
            item_id = raw.get("item_id")
        else:
            warnings.append(f"Ignored invalid item at {location}: {raw!r}")
            return None
        if item_id not in catalog:
            warnings.append(f"Ignored unknown item at {location}: {item_id}")
            return None
        icon = catalog[item_id]
        if icon.kind == "custom" and not allow_widget:
            warnings.append(f"Kept widget/custom item out of folder at {location}: {item_id}")
            return None
        if item_id in used:
            warnings.append(f"Ignored duplicate item at {location}: {item_id}")
            return None
        used.add(item_id)
        return item_id

    def normalize_top_ref(raw: Any, location: str) -> dict[str, Any] | None:
        if isinstance(raw, str):
            item_id = normalize_item_id(raw, location, allow_widget=True)
            if item_id is None:
                return None
            return item_ref_for_catalog_item(catalog[item_id])
        if not isinstance(raw, dict):
            warnings.append(f"Ignored invalid icon at {location}: {raw!r}")
            return None
        ref_type = raw.get("type", "app")
        if ref_type == "folder":
            name = str(raw.get("name") or raw.get("title") or raw.get("displayName") or "Folder").strip() or "Folder"
            raw_items = raw.get("items", [])
            if not isinstance(raw_items, list):
                warnings.append(f"Folder items must be a list at {location}; using an empty folder.")
                raw_items = []
            folder_items: list[str] = []
            for index, child in enumerate(raw_items):
                item_id = normalize_item_id(child, f"{location}.items[{index}]", allow_widget=False)
                if item_id is not None:
                    folder_items.append(item_id)
            kept_items, overflow_items = split_folder_items(folder_items, folder_page_size, max_folder_pages)
            for overflow_item in overflow_items:
                used.discard(overflow_item)
            if overflow_items:
                warnings.append(
                    f"Moved {len(overflow_items)} overflow item(s) out of folder '{name}' at {location}; "
                    f"limit is {folder_page_size * max_folder_pages} apps."
                )
            return {"type": "folder", "name": name, "items": kept_items}
        item_id = normalize_item_id(raw, location, allow_widget=True)
        if item_id is None:
            return None
        return item_ref_for_catalog_item(catalog[item_id])

    raw_dock = plan.get("dock", [])
    if not isinstance(raw_dock, list):
        raise SystemExit("Plan field 'dock' must be a list.")
    dock_refs = compact_refs(normalize_top_ref(raw, f"dock[{index}]") for index, raw in enumerate(raw_dock))
    dock = dock_refs[:max_dock]
    release_dropped_refs(dock_refs[max_dock:], used)
    if len(dock_refs) > max_dock:
        warnings.append(f"Moved {len(dock_refs) - max_dock} oversized dock item(s) to fallback placement.")

    raw_pages = plan.get("pages", [])
    if not isinstance(raw_pages, list):
        raise SystemExit("Plan field 'pages' must be a list.")
    pages: list[list[dict[str, Any]]] = []
    for page_index, raw_page in enumerate(raw_pages, start=1):
        if not isinstance(raw_page, list):
            warnings.append(f"Ignored non-list page at pages[{page_index - 1}]")
            continue
        page = compact_refs(normalize_top_ref(raw, f"pages[{page_index - 1}][{index}]") for index, raw in enumerate(raw_page))
        if page:
            pages.append(page[:page_size])
            release_dropped_refs(page[page_size:], used)
            if len(page) > page_size:
                warnings.append(f"Moved {len(page) - page_size} oversized page-{page_index} item(s) to fallback placement.")

    enforce_fixed_first_page(dock, pages, catalog, used, context, instructions, warnings, page_size)
    append_missing_items(dock, pages, catalog, used, warnings, page_size, folder_page_size, max_folder_pages)
    return {
        "schema_version": 2,
        "dock": dock,
        "pages": pages,
        "notes": require_string_list(plan.get("notes", []), "notes", allow_non_strings=True),
        "warnings": warnings,
    }


def split_folder_items(item_ids: list[str], folder_page_size: int, max_folder_pages: int) -> tuple[list[str], list[str]]:
    capacity = max(1, folder_page_size) * max(1, max_folder_pages)
    return item_ids[:capacity], item_ids[capacity:]


def item_ref_for_catalog_item(icon: IconItem) -> dict[str, str]:
    return {"type": "widget" if icon.kind == "custom" else "app", "item_id": icon.item_id}


def compact_refs(refs: Any) -> list[dict[str, Any]]:
    return [ref for ref in refs if ref is not None]


def release_dropped_refs(refs: list[dict[str, Any]], used: set[str]) -> None:
    for ref in refs:
        if ref.get("type") == "folder":
            for item_id in ref.get("items", []):
                used.discard(item_id)
        elif "item_id" in ref:
            used.discard(ref["item_id"])


def enforce_fixed_first_page(
    dock: list[dict[str, Any]],
    pages: list[list[dict[str, Any]]],
    catalog: dict[str, IconItem],
    used: set[str],
    context: dict[str, Any],
    instructions: str,
    warnings: list[str],
    page_size: int,
) -> None:
    lowered = instructions.lower()
    if not any(phrase in lowered for phrase in ("keep first page", "keep page 1", "first page as is", "第一页")):
        return
    current_pages = context["current_layout"].get("pages", [])
    if not current_pages:
        return

    first_page = copy.deepcopy(current_pages[0])
    if asks_to_keep_first_page_folders_and_widgets_only(instructions):
        first_page = [
            ref
            for ref in first_page
            if ref.get("type") in {"folder", "widget"}
        ]
    fixed_ids = set(layout_ref_item_ids(first_page))
    release_refs_by_item_ids(dock, fixed_ids, used)
    for page in pages:
        release_refs_by_item_ids(page, fixed_ids, used)
    for item_id in fixed_ids:
        if item_id in catalog:
            used.add(item_id)

    if pages:
        pages[0] = first_page[:page_size]
    else:
        pages.append(first_page[:page_size])
    if asks_to_keep_first_page_folders_and_widgets_only(instructions):
        warnings.append("Preserved first-page folders/widgets from the current device layout because the instructions requested it.")
    else:
        warnings.append("Preserved first page from the current device layout because the instructions requested it.")


def asks_to_keep_first_page_folders_and_widgets_only(instructions: str) -> bool:
    lowered = instructions.lower()
    return (
        ("first page folders" in lowered and ("widget" in lowered or "widgets" in lowered))
        or ("第一页" in instructions and "文件夹" in instructions and ("widget" in lowered or "小组件" in instructions))
    )


def release_refs_by_item_ids(refs: list[dict[str, Any]], item_ids: set[str], used: set[str]) -> None:
    kept: list[dict[str, Any]] = []
    for ref in refs:
        if ref.get("type") == "folder":
            original_items = list(ref.get("items", []))
            ref["items"] = [item_id for item_id in original_items if item_id not in item_ids]
            for item_id in original_items:
                if item_id in item_ids:
                    used.discard(item_id)
            kept.append(ref)
        elif ref.get("item_id") in item_ids:
            used.discard(ref["item_id"])
        else:
            kept.append(ref)
    refs[:] = kept


def layout_ref_item_ids(refs: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for ref in refs:
        if ref.get("type") == "folder":
            for item in ref.get("items", []):
                if isinstance(item, str):
                    ids.append(item)
                elif isinstance(item, dict) and isinstance(item.get("item_id"), str):
                    ids.append(item["item_id"])
        elif "item_id" in ref:
            ids.append(ref["item_id"])
    return ids


def normalize_ref_item_ids(items: list[Any]) -> list[str]:
    ids: list[str] = []
    for item in items:
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, dict) and isinstance(item.get("item_id"), str):
            ids.append(item["item_id"])
    return ids


def append_missing_items(
    dock: list[dict[str, Any]],
    pages: list[list[dict[str, Any]]],
    catalog: dict[str, IconItem],
    used: set[str],
    warnings: list[str],
    page_size: int,
    folder_page_size: int,
    max_folder_pages: int,
) -> None:
    missing = [item_id for item_id in catalog if item_id not in used]
    if not missing:
        return
    fallback = find_fallback_folder(dock, pages)
    if fallback is None and any(catalog[item_id].kind != "custom" for item_id in missing):
        fallback = ensure_fallback_folder(dock, pages, page_size)
        warnings.append("Created fallback folder 'Other' for omitted apps; iOS may auto-fill omitted apps otherwise.")
    for item_id in missing:
        icon = catalog[item_id]
        if fallback is not None and icon.kind != "custom":
            if folder_has_capacity(fallback, folder_page_size, max_folder_pages):
                fallback["items"].append(item_id)
            else:
                if not pages or len(pages[-1]) >= page_size:
                    pages.append([])
                pages[-1].append(item_ref_for_catalog_item(icon))
        else:
            if not pages or len(pages[-1]) >= page_size:
                pages.append([])
            pages[-1].append(item_ref_for_catalog_item(icon))
        used.add(item_id)
    target = f"fallback folder '{fallback['name']}'" if fallback is not None else "last page"
    warnings.append(f"Appended {len(missing)} omitted item(s) to {target}; iOS may auto-fill omitted apps otherwise.")


def find_fallback_folder(dock: list[dict[str, Any]], pages: list[list[dict[str, Any]]]) -> dict[str, Any] | None:
    folders = [ref for ref in dock if ref.get("type") == "folder"]
    for page in pages:
        folders.extend(ref for ref in page if ref.get("type") == "folder")
    for preferred in ("其他", "Other", "Unsorted", "Misc", "杂项"):
        for folder in folders:
            if folder.get("name") == preferred:
                return folder
    return None


def folder_has_capacity(folder: dict[str, Any], folder_page_size: int, max_folder_pages: int) -> bool:
    return len(folder.get("items", [])) < max(1, folder_page_size) * max(1, max_folder_pages)


def ensure_fallback_folder(dock: list[dict[str, Any]], pages: list[list[dict[str, Any]]], page_size: int) -> dict[str, Any]:
    folder = {"type": "folder", "name": "Other", "items": []}
    if not pages:
        pages.append([])
    target_page = pages[0]
    if len(target_page) >= page_size:
        pages.append([])
        target_page = pages[-1]
    target_page.append(folder)
    return folder


def build_full_layout_state(state: Any, plan: dict[str, Any], context: dict[str, Any]) -> Any:
    updated = copy.deepcopy(state)
    dock_container, page_containers = split_icon_state(updated)
    catalog: dict[str, IconItem] = context["catalog"]
    item_by_id = {item_id: icon.original for item_id, icon in catalog.items()}
    folder_template = context.get("folder_template")

    def build_ref(ref: dict[str, Any]) -> Any:
        if ref["type"] == "folder":
            folder = make_folder_item(ref["name"], folder_template)
            folder_item_ids = normalize_ref_item_ids(ref.get("items", []))
            folder_items = [copy.deepcopy(item_by_id[item_id]) for item_id in folder_item_ids if item_id in item_by_id]
            folder["iconLists"] = chunk_items(folder_items, folder_page_capacity(folder_template))
            return folder
        return copy.deepcopy(item_by_id[ref["item_id"]])

    new_dock = [build_ref(ref) for ref in plan["dock"]]
    new_pages = [[build_ref(ref) for ref in page] for page in plan["pages"]]

    if isinstance(updated, dict):
        updated["buttonBar"] = adapt_container_shape(dock_container, new_dock)
        updated["iconLists"] = adapt_pages_shape(page_containers, new_pages)
        return updated
    if isinstance(updated, list):
        return [adapt_container_shape(dock_container, new_dock), *adapt_pages_shape(page_containers, new_pages)]
    raise SystemExit(f"Unsupported icon state shape: {type(updated).__name__}")


def make_folder_item(name: str, template: Any) -> dict[str, Any]:
    folder = copy.deepcopy(template) if isinstance(template, dict) else {"displayName": name, "iconLists": []}
    folder["displayName"] = name
    for key in ("name", "title"):
        if key in folder:
            folder[key] = name
    folder["iconLists"] = []
    return folder


def folder_page_capacity(template: Any) -> int:
    if not isinstance(template, dict):
        return 9
    lengths = [len(flatten_icon_container(page)) for page in template.get("iconLists", [])]
    return max([length for length in lengths if length > 0] + [9])


def chunk_items(items: list[Any], capacity: int) -> list[list[Any]]:
    capacity = max(1, capacity)
    return [items[index : index + capacity] for index in range(0, len(items), capacity)]


def print_full_layout_plan_summary(plan: dict[str, Any], context: dict[str, Any]) -> None:
    catalog: dict[str, IconItem] = context["catalog"]
    folders = 0
    top_level = 0
    foldered = 0
    for ref in plan["dock"]:
        top_level += 1
        if ref["type"] == "folder":
            folders += 1
            foldered += len(ref["items"])
    for page in plan["pages"]:
        top_level += len(page)
        for ref in page:
            if ref["type"] == "folder":
                folders += 1
                foldered += len(ref["items"])
    print(f"Plan: {len(plan['dock'])} dock item(s), {len(plan['pages'])} page(s), {top_level} top-level icon(s), {folders} folder(s), {foldered} foldered app(s).")
    for index, page in enumerate(plan["pages"], start=1):
        labels = [layout_ref_label(ref, catalog) for ref in page]
        print(f"Page {index}: " + ", ".join(labels))
    for warning in plan.get("warnings", []):
        print(f"Warning: {warning}")
    for note in plan.get("notes", []):
        print(f"Note: {note}")


def layout_ref_label(ref: dict[str, Any], catalog: dict[str, IconItem]) -> str:
    if ref["type"] == "folder":
        return f"{ref['name']}({len(ref.get('items', []))})"
    item = catalog.get(ref["item_id"])
    return item.label if item else ref["item_id"]


def verify_full_layout_applied(plan: dict[str, Any], state: Any) -> None:
    context = collect_full_layout_context(state)
    present = set(context["catalog"])
    expected = set(full_plan_item_ids(plan))
    missing = expected - present
    if missing:
        raise SystemExit(f"Verification failed: {len(missing)} planned item(s) were not found after apply: {sorted(missing)[:10]}")


def full_plan_item_ids(plan: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for ref in plan.get("dock", []):
        if ref.get("type") == "folder":
            ids.extend(ref.get("items", []))
        elif "item_id" in ref:
            ids.append(ref["item_id"])
    for page in plan.get("pages", []):
        for ref in page:
            if ref.get("type") == "folder":
                ids.extend(ref.get("items", []))
            elif "item_id" in ref:
                ids.append(ref["item_id"])
    return ids


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
