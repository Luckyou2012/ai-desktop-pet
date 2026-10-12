##

This project came out of a random whim I had one day. I told DeepSeek AI about
the idea and let it help me actually build it. The README below was also
written with AI assistance because I am not very good at writing docs.

## Dependencies

Install the required Python packages:

```
pip install pillow requests psutil
```

Python 3.10 or newer on Windows 10 / 11 is required.

## Configuring `config.json`

Create a file named `config.json` next to `main.py`. Fill in the fields
below:

```json
{
  "deepseek_api_key": "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "deepseek_model": "deepseek-chat",
  "deepseek_url": "https://api.deepseek.com/v1/chat/completions",
  "language": "en",
  "autostart": false,
  "personality": "A small, curious creature that likes to follow the mouse cursor.",
  "memory_lines": 20,
  "snapshot_memory": 120,
  "pet_size": 160,
  "gravity": 1.1,
  "draggable": true
}
```

| Field | Meaning |
|---|---|
| `deepseek_api_key` | Your DeepSeek API key. Required. |
| `deepseek_model` | The model name, for example `deepseek-chat`. |
| `deepseek_url` | The chat completions endpoint. |
| `language` | Reply language code, for example `en`, `zh`, `ja`. |
| `autostart` | If true, the app registers itself in the Windows Run key. If false, any previous registration is removed. |
| `personality` | The core personality text used in the system prompt. It is rewritten by the model as the pet learns. |
| `memory_lines` | How many recent dialogue lines are fed back to the model. |
| `snapshot_memory` | How many samples of the pet's own position are kept. |
| `pet_size` | Base size of the pet in pixels. The model can scale it further at runtime. |
| `gravity` | Downward acceleration per physics tick. Higher values fall faster. |
| `draggable` | Whether the pet can be dragged with the mouse. |

## Configuring `animation.json`

`animation.json` defines the keyframe animations. Each trigger has an entry
under `triggers`. Two kinds of entries are supported:

- `"f": [[frame indices...], fps]` — a looping frame sequence.
- `"if": [[frame indices...], fps]` — a one-shot frame sequence.
- `"d": [ ... ]` — a list of keyframe deformations applied to the frames.

The PNG frames are read from `assets/frames/`. Frames are named
`0.png`, `1.png`, ..., up to whatever index you use in the sequences.

A minimal example:

```json
{
  "triggers": {
    "idle": {
      "f": [[0], 2],
      "d": [
        0, 0.0, 0.0, 0, 1.0, 0.0, 0, 0.0, 1.0, 0,
        1.0, 1.0, 0, 0, 1200,
        1, 0.0, 0.04, 0, 1.0, 0.04, 0, 0.0, 1.0, 0,
        1.0, 1.0, 0, 0, 1200
      ]
    },
    "left":  { "f": [[1, 2, 3, 2], 8] },
    "right": { "f": [[1, 2, 3, 2], 8] },
    "jump":  { "if": [[5], 8] },
    "drag":  { "f": [[4], 4] },
    "collide": { "if": [[7], 10] }
  }
}
```

If `animation.json` is missing, the pet will simply not render anything and
will not crash.

## Running

```
python main.py
```

Right-click the pet to open the menu (Talk, Language, Close).
