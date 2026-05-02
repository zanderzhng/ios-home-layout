# iOS Home Layout

Use an OpenAI-compatible LLM and `libimobiledevice` to organize an iPhone or iPad Home Screen over USB.

## Status

Tested on macOS.

## Features

- Create and apply a Home Screen layout plan.
- Move apps between pages and folders.
- Save and restore `backup.json`.

## Requirements

- macOS
- A trusted USB-connected iPhone or iPad
- `libimobiledevice`, `uv`, and an OpenAI-compatible API key

```bash
brew install libimobiledevice uv
idevice_id -l
```

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

## Usage

Create a plan:

```bash
uv run ios-home-layout plan "Group work apps, media apps, games, and utilities into folders."
```

Apply it:

```bash
uv run ios-home-layout apply
```

Save the current layout:

```bash
uv run ios-home-layout backup
```

Restore that saved layout:

```bash
uv run ios-home-layout apply backup.json
```

Use `--udid <device-udid>` when more than one device is connected.

## Troubleshooting

If the device is not found, unlock it, trust the computer, then run:

```bash
idevice_id -l
idevicepair validate
```
