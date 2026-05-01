# iOS Home Layout

Use an OpenAI-compatible LLM and `libimobiledevice` to plan and apply full iPhone/iPad Home Screen layouts.

The default path talks directly to `com.apple.springboardservices`, so normal use does not create an iOS backup. A backup/apply-backup workflow is available when you want a local `idevicebackup2` fallback.

## Status

This project has only been tested on macOS. Linux may work with compatible `libimobiledevice` packages, but it is not verified.

## Features

- Generate one full layout plan for dock, pages, folders, and folder contents.
- Put apps on the desktop or inside folders.
- Move apps out of folders.
- Create, remove, and rename folders by emitting the desired final layout.
- Preserve page 1 when the prompt asks to keep it unchanged.
- Validate model output before writing to the device.
- Keep API settings in `.env`.
- Use `uv` for Python setup.

SpringBoardServices does not reliably hide apps by omitting them from the icon state. iOS can auto-fill omitted installed apps back onto Home Screen pages. To avoid that, validation appends omitted apps to a fallback folder such as `其他`, `Other`, `Unsorted`, `Misc`, or `杂项`, or creates an `Other` folder when needed.

## Requirements

- macOS with `libimobiledevice` installed.
- A paired/trusted iPhone or iPad.
- Python managed by `uv`.
- An OpenAI-compatible API endpoint.

On macOS with Homebrew:

```bash
brew install libimobiledevice uv
```

Confirm the device is visible over USB:

```bash
idevice_id -l
```

USB is recommended. SpringBoardServices may fail when the device is only visible with `idevice_id -n`.

## Setup

```bash
git clone https://github.com/zanderzhng/ios-home-layout.git
cd ios-home-layout
cp .env.example .env
uv sync
```

Edit `.env`:

```dotenv
OPENAI_BASE_URL=https://your-endpoint/v1
OPENAI_API_KEY=your-key
OPENAI_MODEL=your-model
```

If `OPENAI_BASE_URL` points at an endpoint root, the script will use `/v1` automatically.

## Commands

There are four main commands:

- `plan`: read the connected device and write a validated `layout_plan.json`.
- `apply`: apply a saved plan to the connected device, with `--dry-run` available.
- `backup`: create an `idevicebackup2` backup for fallback workflows.
- `apply-backup`: apply a saved plan to the latest local backup, optionally restoring it.

## Plan

```bash
uv run ios-home-layout --connection usb plan \
  --instructions "Keep page 1 as-is. Reorganize the rest by creating folders for travel, tools, finance, media, games, and home apps."
```

The plan can place apps directly on pages, create folders, rename folders, remove folders, and move apps into or out of folders.

For a one-page iPad-style layout:

```bash
uv run ios-home-layout --connection usb plan \
  --instructions "Keep first page folders and widgets as-is. Move all other apps into existing folders if they fit. If they do not fit, put them in Other. I only want one page."
```

The output is `layout_plan.json` by default.

## Apply

Dry run first:

```bash
uv run ios-home-layout --connection usb apply --plan layout_plan.json --dry-run
```

Apply to the connected device:

```bash
uv run ios-home-layout --connection usb apply --plan layout_plan.json
```

For multiple connected devices:

```bash
uv run ios-home-layout --udid <device-udid> --connection usb apply --plan layout_plan.json
```

## Backup

Create an incremental backup:

```bash
uv run ios-home-layout backup
```

Force a full backup only when needed:

```bash
uv run ios-home-layout --full-backup backup
```

## Apply Backup

Apply a saved plan to the latest local backup:

```bash
uv run ios-home-layout apply-backup --plan layout_plan.json
```

Apply and restore the edited backup to the device:

```bash
uv run ios-home-layout apply-backup --plan layout_plan.json --restore-device
```

## Plan Format

Plans describe the final desired layout:

```json
{
  "schema_version": 2,
  "dock": [
    {"type": "app", "item_id": "com.apple.mobilesafari"}
  ],
  "pages": [
    [
      {"type": "folder", "name": "Tools", "items": ["com.example.ssh", "com.example.calc"]},
      {"type": "app", "item_id": "com.example.mail"}
    ]
  ],
  "notes": []
}
```

Folder removal is represented by not emitting that folder in the final layout. Folder creation is represented by emitting a new folder object. Folder rename is represented by changing the folder `name`.

Folder capacity can be tuned:

```bash
uv run ios-home-layout --connection usb --folder-page-size 9 --max-folder-pages 15 apply --plan layout_plan.json --dry-run
```

## Troubleshooting

If no device is found:

```bash
idevice_id -l
idevicepair validate
```

Unlock the device and tap Trust if prompted.

If SpringBoardServices cannot start, retry over USB:

```bash
uv run ios-home-layout --connection usb plan --instructions "Summarize current layout without changing intent."
```

If an OpenAI-compatible gateway returns an HTML page, make sure `OPENAI_BASE_URL` points at the API base, usually `/v1`.

## Files

- `ios_layout_llm.py`: CLI implementation.
- `pyproject.toml`: Python project metadata and dependencies.
- `uv.lock`: locked dependency versions.
- `.env.example`: environment variable template.

Generated plans, backups, `.env`, virtualenvs, and debug plist files are ignored.
