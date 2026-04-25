# iOS Home Screen LLM

Use an OpenAI-compatible LLM and `libimobiledevice` to inspect, plan, and organize iPhone/iPad Home Screen layouts.

The default backend talks directly to `com.apple.springboardservices`, so normal use does not create an iOS backup. A slower `idevicebackup2` backend is kept as a fallback.

## Features

- Inspect the current Home Screen layout over USB.
- Ask an LLM to rearrange top-level icons.
- Move loose apps into existing folders based on folder names and contents.
- Keep sensitive configuration in `.env`.
- Validate model output before applying it.
- Use `uv` for repeatable Python setup.

## Safety Model

- `plan` is a dry run and writes a local JSON plan only.
- `apply` writes to the connected device.
- `folderize --apply` writes to the connected device.
- The direct SpringBoardServices backend does not create a backup.
- The backup backend edits a local backup copy and only restores when `--restore-device` is provided.

SpringBoardServices does not reliably hide apps by omitting them from the icon state. iOS can auto-fill omitted installed apps back onto Home Screen pages. For folder organization, the tool uses a folder named `其他` as the fallback destination when present.

## Requirements

- macOS or Linux with `libimobiledevice` installed.
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

If the device only appears with `idevice_id -n`, SpringBoardServices may fail over the network path. USB is recommended.

## Setup

```bash
git clone <your-repo-url>
cd ios-home-screen-llm
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

## Inspect

```bash
uv run ios-layout-llm --connection usb inspect
```

Useful connection options:

```bash
uv run ios-layout-llm --connection auto inspect
uv run ios-layout-llm --connection usb inspect
uv run ios-layout-llm --connection network inspect
```

For multiple connected devices:

```bash
uv run ios-layout-llm --udid <device-udid> --connection usb inspect
```

## Top-Level Rearrangement

Create a plan:

```bash
uv run ios-layout-llm --connection usb plan \
  --instructions "Put daily apps in the dock and first page. Move games and rarely used apps later."
```

Apply the saved plan:

```bash
uv run ios-layout-llm --connection usb apply --plan layout_plan.json
```

One-shot plan and apply:

```bash
uv run ios-layout-llm --connection usb rearrange \
  --instructions "Organize for work: communication, calendar, notes, files, and browser first."
```

## Folder Organization

`folderize` is for workflows where page 1 should stay fixed and existing page-2 folders are the destination folders.

Dry run:

```bash
uv run ios-layout-llm --connection usb folderize \
  --instructions "基于现有布局，第一页文件夹和程序不动，第二页的文件夹不动，基于文件夹名称整理app放到文件夹里，没有合适文件夹的放到其他。"
```

Apply:

```bash
uv run ios-layout-llm --connection usb folderize \
  --instructions "基于现有布局，第一页文件夹和程序不动，第二页的文件夹不动，基于文件夹名称整理app放到文件夹里，没有合适文件夹的放到其他。" \
  --apply
```

The generated folder plan is written to `folder_plan.json`, which is ignored by Git.

## Backup Fallback

Use this only if the direct SpringBoardServices backend does not work for your device/iOS version.

```bash
uv run ios-layout-llm --backend backup plan \
  --instructions "Put daily apps in the dock and first page."

uv run ios-layout-llm --backend backup apply \
  --plan layout_plan.json \
  --restore-device
```

Backups are incremental by default in backup mode. Force a full backup only when needed:

```bash
uv run ios-layout-llm --backend backup --full-backup inspect
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
uv run ios-layout-llm --connection usb inspect
```

If an OpenAI-compatible gateway returns an HTML page, make sure `OPENAI_BASE_URL` points at the API base, usually `/v1`.

## Files

- `ios_layout_llm.py`: CLI implementation.
- `pyproject.toml`: Python project metadata and dependencies.
- `uv.lock`: locked dependency versions.
- `.env.example`: environment variable template.

Generated plans, backups, `.env`, virtualenvs, and debug plist files are ignored.
