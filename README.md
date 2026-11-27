# ZeroSounds3-JSX

**Deterministic Xbox Classic style sound effect synthesis for games and applications.**

Zero Sounds 3 is the sequel to [ZeroSounds2-JSX](https://github.com/MushroomFleet/zerosounds2-JSX). The first project synthesised SNES-style effects and the second modelled the PlayStation's sound chip. This one models the audio processor of the original Xbox (2001) for a more modern sound: clean 48 kHz sources, resonant filter sweeps, three or four layered voices per sound, placement around the listener, and long smooth reverb.

It keeps the ZeroBytes method, *position-is-seed, zero bytes store infinity*. Every sound is derived from a hash of its place in the library and a world seed. There is no `Math.random()`, no stored presets and no sample files. One 32-bit integer defines a library of 40 named sounds and an unlimited space of unnamed ones, and the same seed produces the same 16-bit audio, byte for byte, in the browser and in Python.

## How it works

Each sound is three or four hardware-style voices. For every voice the engine:

1. Hashes its way down `worldSeed -> category -> sound -> voice -> parameter`.
2. Computes a source sample at 48 kHz: an FM tone, a band-limited wavetable, a noise loop, a struck object or a pitch-drop sine.
3. Stores it as 16-bit PCM, or encodes it to Xbox ADPCM (65 samples in 36 bytes) and decodes it again.
4. Plays it with pitch stepping in 1/4096-octave units, a resonant low-pass filter, an amplitude envelope and a second envelope that sweeps pitch and filter. These controls update once per 32-sample frame, 1,500 times a second, as on the console.
5. Places it around the listener: the far ear hears it later, quieter and duller. Some voices move while they play.

The voices are mixed and sent through a reverb described by decay time and damping. The result is rendered offline to a 48 kHz stereo buffer and then played.

To make the bytes reproducible, the signal path avoids the platform's `sin`, `exp` and `pow`, which are allowed to round differently between machines, and uses its own versions built from plain arithmetic. The full design, with the formulas, is in [`ZeroSounds3-grounding.md`](ZeroSounds3-grounding.md).

**Accuracy note.** The sample rate, frame rate, pitch units, two-envelope voice, low-pass recursion and its 30 Hz to 8 kHz range, and the ADPCM block format follow the documented hardware. The interpolator, the head model used for placement, the reverb's internal structure and its room values are choices made for this project. This is a style synthesiser, not an emulator. The grounding document labels each part.

## Contents

| File | What it is |
|---|---|
| [`ZeroSounds3-grounding.md`](ZeroSounds3-grounding.md) | Design document: why Xbox effects sound as they do, every model and formula, tuning guide, verification method |
| [`ZeroSounds3.py`](ZeroSounds3.py) | Reference engine and command-line tool. Renders WAV files and acts as the oracle the JavaScript engine is checked against. Standard library only |
| [`ZeroSounds3.jsx`](ZeroSounds3.jsx) | React component, plus the same engine in JavaScript and a small Web Audio player |
| [`ZeroSounds3-demo.html`](ZeroSounds3-demo.html) | Standalone interactive demo with the component inlined. No build step |
| [`build-demo.py`](build-demo.py) | Refreshes the checksum table in the `.jsx` from the Python engine and re-inlines the `.jsx` into the demo |
| `README.md` | This file |

## Using the demo

Open `ZeroSounds3-demo.html` in a browser. It works straight from disk; it needs an internet connection to load React, Babel and the fonts from their CDNs.

- **Press a sound** to render and play it. The inspector shows the stereo waveform, a top-down view of where each voice sits around you, the length, sample memory used, reverb, a CRC-32 of the audio, and each voice's source, storage format, pitch and filter with its loudness and filter cutoff drawn over time.
- **Change the world seed** by typing hex, dragging the slider or pressing **Next seed**. All 40 sounds are re-derived.
- **Environment** overrides the reverb room for every sound, to hear the same effect in a corridor or a hangar. "Chosen by the seed" restores the derived rooms.
- **Download WAV** saves exactly what you heard.
- **Unnamed patches** steps through the sounds that have numbers instead of names.
- **Check all 40 sounds** renders everything twice and compares checksums. At seed `0x1337BEEF` it also compares against checksums produced by `ZeroSounds3.py`, so a pass means the browser and Python engines agree bit for bit.

Headphones make the placement much easier to hear.

## Using the React component

Copy `ZeroSounds3.jsx` into your project. It has no dependencies beyond React.

```jsx
import ZeroSounds3 from './components/ZeroSounds3';

export default function App() {
  return <ZeroSounds3 initialSeed={0xDEADBEEF} onPlay={(name) => console.log(name)} />;
}
```

| Prop | Type | Default | Meaning |
|---|---|---|---|
| `initialSeed` | number | `0x1337BEEF` | World seed on mount (unsigned 32-bit) |
| `initialVolume` | number | `0.7` | Master volume, 0 to 1 |
| `initialRoom` | string | `'auto'` | `'auto'`, or a room name to override every sound's reverb |
| `onPlay` | function | `null` | Called as `(name, patch, render)` after a sound starts |
| `className` | string | `''` | Extra class on the root element |

The component injects its own styles under the `zs3` class prefix. It uses the Michroma, Saira and Azeret Mono fonts if the page provides them and falls back to system fonts otherwise.

## Using the engine in a game

The engine functions are exported from the same file and do not need React or the DOM.

```js
import { zbDeriveParams, zbRandomize, zsRender, zsEncodeWav, ZS3Engine } from './ZeroSounds3';

const player = new ZS3Engine();          // create after a user gesture
const cache = new Map();

function playSound(name, worldSeed) {
  const key = `${worldSeed}:${name}`;
  if (!cache.has(key)) cache.set(key, zsRender(zbDeriveParams(name, worldSeed)));
  player.play(cache.get(key));
}

playSound('select', game.worldSeed);
```

A render took about 18 ms on average under Node.js in testing, so cache renders or pre-render at load rather than rendering per frame.

The seed tricks from the earlier versions still work, because the whole library follows the seed:

```js
// The palette shifts as the player descends
playSound('footstep', zbHash(player.depth, baseSeed));

// Every item has its own sound, identical on every client
player.play(zsRender(zbRandomize(item.id, worldSeed)));
```

Patches are plain data, so you can edit one before rendering it:

```js
const patch = zbDeriveParams('door', worldSeed);
patch.reverb.room = 'hangar';            // the room this door is in
patch.layers[0].az0 = -70;               // start the main voice far left
player.play(zsRender(patch));
```

### API

```js
zbHash(key, seed)                 // u32; numeric or string key (same as ZeroSounds 1 and 2)
zbFloat(h)  zbRange(h, lo, hi)  zbInt(h, lo, hi)

zbDeriveParams(name, worldSeed)   // named sound -> patch
zbRandomize(n, worldSeed)         // unnamed sound number n -> patch
zsRender(patch)                   // -> { left, right: Int16Array, sampleRate: 48000,
                                  //      gain, peak, sampleBytes, layerEnvs, layerCuts }
zsChecksum(render)                // CRC-32 of the interleaved PCM
zsEncodeWav(render)               // -> Uint8Array, a 16-bit stereo WAV file
zbVerifyOne(name, worldSeed)      // -> { crc, deterministic, oracle, ok }
zbVerifyAll(worldSeed)            // -> { [name]: result }

const player = new ZS3Engine();
player.play(render);              // creates the AudioContext on first use
player.setVolume(0.5);
```

A patch looks like this (see the grounding document for every field):

```js
{ name, category, archetype, direction, worldSeed, soundSeed, baseFreq, hold, azimuth, level,
  reverb: { room, wet },
  layers: [{ role, source, format, freq, step0, notes: [{ t, ratio, gain }], hold,
             eg1: { delay, attack, hold, decay, sustain, release },
             eg2: { attack, decay, sustain, release },
             pegReg, filter: { on, cutoff, resDb, egOct, lfoOct },
             lfo: { rate, delay, pitchOct, trem },
             vol, az0, az1, moveTime, send }] }
```

## Using the Python engine

The Python engine needs nothing outside the standard library.

```bash
python ZeroSounds3.py --list                              # sound names by category
python ZeroSounds3.py --sound select --out out/           # one WAV
python ZeroSounds3.py --sound jump --sound plasma --seed 0xDEADBEEF --out out/
python ZeroSounds3.py --all --out out/                    # all 40
python ZeroSounds3.py --random 7 --out out/               # unnamed patch 7
python ZeroSounds3.py --params select                     # the derived patch as JSON
python ZeroSounds3.py --verify                            # determinism and level check
python ZeroSounds3.py --checksums                         # CRC-32 per sound, as JSON
```

Files are written as `<name>_<SEED>.wav`, 16-bit stereo at 48 kHz, and match the browser's output for the same seed byte for byte. `--verify` and `--checksums` use one worker process per CPU core; pass `--jobs 1` to turn that off.

From Python:

```python
import ZeroSounds3 as zs

patch = zs.zb_derive_params('explosion', 0xDEADBEEF)
render = zs.zs_render(patch)           # dict with 'left' and 'right' lists of int16 values
zs.zs_write_wav('explosion.wav', render)
```

`--verify` over all 40 sounds took about 16 seconds on two cores in testing; the inner loops are plain Python so they can be read against the JavaScript.

## Sound library

40 named sounds in 8 categories. Each is a four-field anchor, not a preset.

| Category | Sounds |
|---|---|
| **ui** | navigate, select, back, pause, notify, error, save |
| **pickup** | ammo, shield, key, health |
| **movement** | jump, land, dash, footstep, roll |
| **combat** | melee, gunshot, plasma, reload, ricochet, shieldHit |
| **impact** | explosion, crash, thud, glassBreak |
| **power** | powerUp, levelUp, recharge, charge, checkpoint |
| **damage** | hurt, death, alarm, warning |
| **environment** | door, terminal, splash, teleport, switch |

Seven archetypes decide how a sound's voices are layered:

| Archetype | Voices |
|---|---|
| `glass` | FM body through a closing filter, detuned shimmer, high "air" partials, sine sub |
| `pad` | Two detuned wavetables through a swept resonant filter, sine sub |
| `arp` | Body and octave voice re-keyed for 2 to 5 notes, with a held wash underneath |
| `whoosh` | Two filtered noise voices that cross the stereo field in opposite directions, plus a gliding tone |
| `impact` | Noise crack, struck-object body, pitch-drop sub, and a debris tail on long sounds |
| `energy` | Gliding carrier and chorus with a swept filter and fast LFO, sine sub, noise fizz |
| `mech` | 2 to 4 timed clicks and clanks, a weight thump and a servo tone |

Six reverb rooms: room, corridor, chamber, hall, hangar, cavern.

### Adding a sound

Add one line to `ZB_SOUNDS` in both `ZeroSounds3.py` and `ZeroSounds3.jsx`, using a `styleIndex` not already taken in that category, then run `python build-demo.py`.

```js
grenade: { category: 'combat', arch: 'impact', styleIndex: 6, dir: 0, label: 'Grenade' },
```

`dir` is `1` for rising, `-1` for falling, `0` to let the hash choose.

## Parameters

Both engines use the same names and ranges.

| Parameter | Range | Meaning |
|---|---|---|
| `worldSeed` | 0 to `0xFFFFFFFF` | Defines the whole library |
| `baseFreq` | per category, 40 to 1700 Hz | Root pitch |
| `hold` | per category, 0.05 to 0.9 s | Time until key-off |
| `azimuth` | per category, up to ±60° | Where the sound sits; positive is right |
| `level` | 0.56 to 0.89 | Peak level for the category, as a fraction of full scale |
| `reverb.room` | room, corridor, chamber, hall, hangar, cavern | Reverb environment |
| `reverb.wet` | 0.05 to 0.50 | Reverb amount |
| `source.kind` | fm, wave, noise, modal, thump | Source sample type |
| `format` | pcm16, adpcm | How the source sample is stored |
| `step0` | up to 8 | Playback rate (1.0 plays the sample at 48 kHz) |
| `pegReg` | about -320 to 320 | Envelope-to-pitch depth; 128 is one octave |
| `filter.cutoff` | 30 to 8000 Hz at the filter | Low-pass cutoff before modulation |
| `filter.resDb` | 0 to 18 dB | Resonance |
| `filter.egOct` | about -3.5 to 3.5 | Envelope-to-cutoff depth in octaves |
| `eg1` times | 0.0005 to 2.4 s | Amplitude envelope; decay and release are the time to fall 96 dB |
| `az0`, `az1`, `moveTime` | degrees, seconds | A voice's start position, end position and travel time |
| `send` | 0 to 1 | How much of the voice feeds the reverb |

## Requirements

| Interface | Needs |
|---|---|
| Demo | A current browser; internet access for the React, Babel and font CDNs |
| React component | React 17 or later (hooks). No other packages |
| Python engine | Python 3.8 or later. No packages |
| `build-demo.py` | Same as the Python engine |

## Verification

What has been checked:

- 40 named sounds and 4 unnamed patches at three seeds (132 renders) gave identical CRC-32s from the Python engine and from the JavaScript engine under Node.js.
- `--verify` passed at five seeds: every sound reproducible and within the level bounds.
- The demo was mounted in headless Chromium with React and Babel served locally: 40 pads rendered, sounds and an unnamed patch played, the environment override changed the render, and the in-page check reported all 40 matching the Python checksums.

What has not been checked: no listening tests, and no comparison against real hardware or an Xbox emulator. Treat the parameter ranges as a starting point for tuning by ear. The web fonts were not loaded in the browser test, so the demo's typography was only seen with fallback fonts.

## Continuing development

The repository is meant to be cloned and developed further, for example with Claude Code. `ZeroSounds3-grounding.md` is the context document, `ZeroSounds3.py` is the behavioural oracle (any port must reproduce its checksums), and the engine block in `ZeroSounds3.jsx` is the natural boundary for a TypeScript module. There is no `ZeroSounds3-TINS.md` implementation plan yet.

## License

See `LICENSE`. No licence file has been added to this repository yet; add the one you intend before publishing.

## 📚 Citation

### Academic Citation

If you use this codebase in your research or project, please cite:

```bibtex
@software{zerosounds3_jsx,
  title = {ZeroSounds3-JSX: ZeroBytes-Aligned Deterministic Xbox Classic Style Audio Synthesis},
  author = {Drift Johnson},
  year = {2026},
  url = {https://github.com/MushroomFleet/zerosounds3-JSX},
  version = {1.0.0}
}
```

### Donate

[![Ko-Fi](https://cdn.ko-fi.com/cdn/kofi3.png?v=3)](https://ko-fi.com/driftjohnson)
