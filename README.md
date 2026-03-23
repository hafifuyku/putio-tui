# putio-tui

A terminal UI client for [put.io](https://put.io), inspired by Midnight Commander.

![putio-tui screenshot](screenshot.png)

## Install

Download the latest binary from [releases](https://github.com/hafifuyku/putio-tui/releases):

```
curl -L https://github.com/hafifuyku/putio-tui/releases/latest/download/putio-tui -o putio-tui
chmod +x putio-tui
```

Move it somewhere in your PATH:

```
mv putio-tui /usr/local/bin/
```

> Currently macOS ARM64 (Apple Silicon) only.

## Get a put.io token

1. Go to https://app.put.io/oauth
2. Create a new OAuth app (or use an existing one)
3. Copy the OAuth token

## Usage

Pass the token as an argument:

```
putio-tui YOUR_TOKEN
```

Or set it as an environment variable:

```
export PUTIO_TOKEN=YOUR_TOKEN
putio-tui
```

Or save it to a config file (so you never have to pass it again):

```
mkdir -p ~/.config/putio-tui
echo YOUR_TOKEN > ~/.config/putio-tui/token
putio-tui
```

## Keys

| Key | Action |
|-----|--------|
| `j` / `k` / arrows | Navigate |
| `Enter` / `Right` | Open folder or play file in VLC |
| `Left` / `Backspace` | Go back |
| `Tab` | Switch between sidebar and file list |
| `+` / `Space` | Mark/unmark file |
| `*` | Invert selection |
| `m` / `F6` | Move selected files |
| `F7` | Create new folder |
| `D` / `Del` / `F8` | Delete |
| `s` | Sort |
| `/` | Search |
| `a` | Add transfer (magnet/URL) |
| `g` / `G` | Jump to top/bottom |
| `PgUp` / `PgDn` | Page up/down |
| `1` `2` `3` | Switch to files/transfers/history |
| `q` / `F10` | Quit |

## Build from source

Requires Python 3.11+.

```
# Run directly
pip install textual rich
python app.py

# Build standalone binary
pip install pyinstaller
pyinstaller --onefile --name putio-tui app.py
# Binary will be in dist/putio-tui
```
