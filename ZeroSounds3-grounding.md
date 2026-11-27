# Zero Sounds 3 grounding: Xbox Classic style SFX synthesis

This is the design document for Zero Sounds 3. It explains why sound effects on the original Xbox (2001) sound the way they do, defines every model the engine uses, and shows how to build it in Python so that a JavaScript port produces the same bytes. `ZeroSounds3.py` and `ZeroSounds3.jsx` are implementations of this document; where they disagree with it, the Python file is the oracle and this document should be corrected.

Three labels are used throughout:

- **Hardware**: a documented property of the console's audio processor, with the source named.
- **Standard**: taken from a public specification (DLS Level 2, I3DL2, IMA ADPCM).
- **Project choice**: a value or structure chosen for this project. These are parametric defaults pending tuning by ear, not measurements.

## 1. Where this sits in the series

| | Zero Sounds 1 | Zero Sounds 2 | Zero Sounds 3 |
|---|---|---|---|
| Console modelled | SNES | PlayStation | Xbox (2001) |
| Output | 32 kHz | 44.1 kHz | 48 kHz |
| Sample storage | BRR | SPU-ADPCM, 28 samples in 16 bytes | 16-bit PCM or Xbox ADPCM, 65 samples in 36 bytes |
| Interpolation | Gaussian | 4-point Gaussian | 4-point cubic (project choice) |
| Tone shaping | none | none | resonant low-pass per voice, swept by an envelope |
| Envelopes | ADSR | SPU ADSR | two DAHDSR envelopes per voice |
| Stereo | pan | per-channel volume | 3D placement, including moving sources |
| Reverb | echo | SPU-topology, 22.05 kHz | I3DL2-parameter, 48 kHz |
| Voices per sound | 1 to 2 | 2 to 3 | 3 to 4 |

The ZeroBytes method is unchanged: position is seed. A sound's parameters are a pure function of `(sound anchor, worldSeed)`. Nothing is stored and nothing is random. As in Zero Sounds 2, the guarantee covers the audio itself: one seed gives one byte stream in every implementation.

## 2. Why Xbox effects sound the way they do

The PlayStation's character came from limits: 24 voices, lossy 4-bit samples, a soft interpolator, and a half-rate reverb. The Xbox's character comes from having the limits removed and several studio tools added to every voice. Its audio processor, the APU inside the MCPX chip, has three parts: a fixed-function voice processor (VP), a programmable DSP for effects (GP), and a second DSP for encoding the output (EP).

Hardware facts used by this design, all from the [xboxdevwiki APU page](https://xboxdevwiki.net/APU):

- Processing is 24-bit PCM at 48,000 Hz.
- A frame is 32 samples, so there are 1,500 frames a second. Envelope counters tick at this rate.
- Up to 256 voices, of which up to 64 can be 3D.
- Voice input can be 8-bit, 16-bit or 24-bit PCM, or ADPCM.
- Pitch is a signed 16-bit register with 4,096 units per octave, relative to 48 kHz: `f = 48000 * 2^(p / 4096)`.
- Each voice has two DAHDSR envelopes (delay, attack, hold, decay, sustain, release). One controls amplitude. The other modulates pitch and the low-pass cutoff. The pitch modulation depth is applied as `pitch_mod * 32 * env` in the same 1/4096-octave units.
- Each voice can run a DLS2 low-pass filter with resonance up to 22.5 dB, a parametric equaliser, or both, depending on voice type. The cutoff register is clamped at 30 Hz and 8,000 Hz. The low-pass is a two-pole recursion `y[i] = K*x[i] - b1*y[i-1] - b2*y[i-2]` with `b1 = -2*r*cos(theta)`, `b2 = r^2`, `K = g*(1 + b1 + b2)`.
- Each voice has an LFO, and 3D voices have HRTF filtering.
- Voices mix into 32 bins, which the GP reads for effects such as I3DL2 reverb.

From the [xboxdevwiki Xbox ADPCM page](https://xboxdevwiki.net/Xbox_ADPCM): Xbox ADPCM is IMA ADPCM with 64 nibbles per block. A mono block is 36 bytes and decodes to 65 samples, because the block header carries one sample verbatim.

What that produces to the ear:

1. **Clean, full-bandwidth sources.** 48 kHz output and 16-bit samples mean no grain to hide behind. ADPCM is still common because 64 MB of memory is shared with graphics, but IMA at 4 bits is far gentler than the PlayStation's codec.
2. **Movement inside each sound.** A resonant filter swept by its own envelope gives the "wow" and "zap" of the era's interface and energy-weapon sounds. PlayStation voices had no filter at all.
3. **Layering.** With 256 voices, an effect is routinely several samples played together: a transient, a body, a sub and a tail.
4. **Position.** Sounds are placed around the listener rather than panned, and they move.
5. **Long, dark, smooth reverb** described by physical parameters (decay time, high-frequency damping) rather than by delay-line register tables.

Not modelled: 5.1 output and Dolby Digital encoding (the output here is stereo), the parametric equaliser, and distance attenuation. The wiki marks its own statements about the encoder as needing citation, so nothing in this design depends on them.

## 3. Signal path

```
worldSeed -> category -> sound -> voice -> slot          (hashing, section 5)
                                   |
        source sample, 16-bit, 48 kHz                    (section 6)
                                   |
        stored as PCM16 or Xbox ADPCM, then decoded      (section 7)
                                   |
   +---- control frame, 1500 Hz ---------------------+
   | EG1 amplitude, EG2 modulation, LFO              |   (section 8)
   | -> pitch step, filter coefficients, gain        |
   +-------------------------------------------------+
                                   |
        pitch stepping + 4-point cubic interpolation
                                   |
        DLS2-form resonant low-pass
                                   |
        amplitude (EG1, tremolo, volume)
                                   |
        3D placement -> left, right                      (section 9)
                                   |
   dry mix  <----------------------+-----> reverb send (mono)
      |                                         |
      +<------------- I3DL2-parameter reverb <--+        (section 10)
      |
   peak-normalise to category level, round to 16 bits    (section 11)
```

## 4. Determinism rules

The output must be bit-identical in Python and JavaScript. The rules that make this true:

1. **No library transcendentals in the signal path.** `sin`, `exp` and `pow` may round differently between platforms. The engine uses its own `dsin` (range reduction, then a degree-15 odd polynomial) and `dexp2` (2 to the power x: split into integer and fraction, a degree-13 polynomial for the fraction, then repeated doubling or halving). Both use only `+ - * /` and `floor`, evaluated in a fixed order.
2. **IEEE double arithmetic in one order.** The four basic operations are correctly rounded in both languages, so the same expression with the same operand order gives the same bits. Expressions are written identically in both files. Do not rearrange them, and do not use vectorised sums in a port (NumPy's `sum` adds in a different order).
3. **Integers where position matters.** The sample position is a 16.16 fixed-point integer. Pitch steps are integers. The ADPCM codec is all integer.
4. **Named rounding.** `zs_rnd(x) = floor(x + 0.5)` everywhere. Never the language's `round`.
5. **Display code is exempt.** The demo's drawing code may use `Math.log` because it never touches samples.

The 24-bit hardware path is represented by doubles, which carry more precision than 24 bits. The only quantisation points are the 16-bit source samples and the final 16-bit output.

## 5. Hash core and parameter derivation

The hash is the one from Zero Sounds 1 and 2, so seeds and habits carry over.

```
zb_hash(key, seed) -> u32        murmur-style finaliser for integer keys
zb_float(h)  = (h & 0xFFFFFF) / 2^24
zb_range(h, lo, hi) = lo + zb_float(h) * (hi - lo)
zb_int(h, lo, hi)   = lo + h % (hi - lo + 1)
zb_pick(h, list)    = list[h % len(list)]
```

Seeds are chained, one level per line:

```
cat_seed   = zb_hash(category.index, world_seed)
sound_seed = zb_hash(style_index, cat_seed)
hs(k)      = zb_hash(100 + k, sound_seed)      sound-level slot k
layer_seed = zb_hash(i + 1, sound_seed)        voice i
h(k)       = zb_hash(k, layer_seed)            voice-level slot k
```

A named sound is a four-field anchor, `(category, archetype, styleIndex, dir)`. The category supplies ranges (pitch, hold time, reverb amount, how far off-centre it may sit, peak level, and which rooms it may use). The archetype decides how voices are layered. `dir` is +1 for rising, -1 for falling, 0 to let the hash choose. Unnamed patch `n` hashes `n` to pick a category and archetype and uses `1000 + (v & 0xFFFF)` as its style index.

Sound-level slots: 0 base frequency, 1 hold, 2 room, 3 wet amount, 4 to 6 arpeggio shape, 7 direction, 9 azimuth, 10 and up archetype-specific. Voice-level slots: 11 to 16 source, 20 to 24 amplitude envelope, 25 default modulation envelope, 27 storage format, 30 LFO rate.

### Archetypes

| Archetype | Voices | Used for |
|---|---|---|
| `glass` | FM body through a closing filter, detuned FM shimmer, high wavetable "air", sine sub | chimes, pickups, shield hits |
| `pad` | two detuned wavetables through a swept resonant filter, sine sub | soft interface tones |
| `arp` | body and octave voice re-keyed for 2 to 5 notes, plus a held wash underneath | confirmations, level-ups |
| `whoosh` | two noise voices through a swept resonant filter that cross the stereo field in opposite directions, plus a gliding tone | movement, doors, melee |
| `impact` | noise crack, struck-object body, pitch-drop sub, and for long holds a debris tail | hits, explosions, footsteps |
| `energy` | carrier and detuned chorus with pitch glide, swept filter and fast LFO, sine sub, filtered noise fizz | plasma, charging, teleports |
| `mech` | 2 to 4 timed clicks and clanks, a final weight thump, and a servo tone | reloads, switches |

## 6. Source samples

Sources are written at 48 kHz as 16-bit integers. Five kinds:

**wave**: one cycle of an additive wavetable, `P` samples, looped. `P` is the largest of 1024, 512, 256, 128, 64 that keeps the playback step at or below 8 at the voice's highest pitch. The number of harmonics is capped at `floor(0.45 * 48000 / fmax)`, so the top harmonic stays under 21.6 kHz at the highest pitch the voice reaches. This is the main reason the output is cleaner than Zero Sounds 2. Shapes: sine, saw, pulse, tri, organ, glass, soft (1/k²), and formant (a saw with one resonant bump at a hashed harmonic). Each harmonic amplitude is multiplied by a hashed factor between 0.85 and 1.15. Harmonic `k` at sample `n` is read from one sine table as `tab[(k*n) mod P]`, which is exact and fast.

**fm**: two-operator FM, `out[n] = sin(ph + idx(n) * sin(ratio * ph))`, `ph = 2*pi*n/P`. An attack section in which the index falls from `index0` to `indexLoop` along `t²` is followed by a two-cycle loop at `indexLoop`. `index0` is capped so that the widest significant sideband, `(ratio * index0 + 1) * fmax`, stays under 18 kHz.

**noise**: 16,384 samples (0.34 s) of white noise from `zb_hash(n, seed)`, through a one-pole low-pass whose coefficient is the hashed "colour", looped.

**modal**: a struck object. Four to six sine partials at the ratios of an ideal free bar, circular membrane or closed tube, or an invented inharmonic set called shard. Each partial has a hashed ±3% detune and its own exponential decay, with higher partials dying faster. One-shot.

**thump**: a sine whose frequency falls from `f1` to `f2`, with the phase integrated in closed form and a `t²` amplitude. One-shot.

Tonal sources are normalised to a peak of 26,000, noise to 24,000 and one-shots to 28,000, leaving headroom for the codec.

## 7. Storage: PCM16 or Xbox ADPCM

Each voice's source is either left as 16-bit PCM or passed through an Xbox ADPCM encode and decode. The hash picks the format, weighted two to one towards ADPCM for noise and thumps and two to one towards PCM for tonal sources. This mirrors practice on the console (project choice for the weights).

The codec is standard IMA ADPCM. For each block of 65 samples the first sample goes into the header unchanged and the remaining 64 are coded as 4-bit nibbles against the 89-entry step table. The step index carries across blocks. The encoder is the textbook greedy one: at each sample it emits the nibble a decoder would need and then updates its own predictor exactly as the decoder will, so encode and decode cannot drift. Memory used is `ceil(N / 65) * 36` bytes for ADPCM and `2N` bytes for PCM; the total is reported as `sampleBytes`.

## 8. The voice

### Control frames

Everything that changes over time is evaluated once per 32-sample frame at `t = k / 1500` seconds: both envelopes, the LFO, the pitch step and the filter coefficients. Pitch and filter coefficients are held for the frame. Amplitude is interpolated linearly across the frame to avoid zipper noise (project choice; the hardware's own smoothing is not documented in the sources used).

### Amplitude envelope, EG1

DAHDSR with DLS2-style curves (standard): the attack is linear in amplitude; decay and release are linear in decibels. `decay` and `release` are the time to fall the full 96 dB, so a decay of 0.5 s is 40 dB down after about 0.2 s.

```
before key-off, at time t:
  t < delay                 -> 0
  attack                    -> (t - delay) / attack
  hold                      -> 1
  decay                     -> dB = min(96 * td / decay, 96 * (1 - sustain));  amp = 2^(-dB * DB2OCT)
after key-off at T:
  amp = amp_at(T) * 2^(-(96 * (t - T) / release) * DB2OCT)
```

`DB2OCT = log2(10) / 20`. The voice ends at `T + release`. Writing the release as a factor on the level at key-off avoids needing a logarithm.

### Modulation envelope, EG2

Linear segments from 0 to 1: attack, decay to a sustain level, release to 0. Two shapes cover every use: `rise(t)` climbs to 1 over `t` seconds and stays; `fall(t)` starts at 1 and falls to 0 over `t` seconds. As on the hardware, this one envelope drives both pitch and filter cutoff for its voice.

### LFO

A sine at `rate` Hz starting after `delay`. It can modulate pitch (`pitchOct`), filter cutoff (`filter.lfoOct`) and amplitude (`trem`).

### Pitch

```
units = rnd(pegReg * 32 * eg2 + lfo.pitchOct * 4096 * lfo)       1/4096-octave units
step  = rnd(step0 * note.ratio * 2^(units / 4096) * 65536)       16.16 fixed point
```

`pegReg` is the envelope-to-pitch depth in the hardware's unit of 32/4096 octave, so 128 is one octave. Only the modulation is quantised to register units; quantising the base pitch would need a logarithm.

The position is an integer advanced by `step` each sample. Reading uses 4-point cubic (Catmull-Rom) interpolation across samples `idx-1` to `idx+2`. The interpolator on the real chip is not described in the sources used, so this is a project choice. For looped sources the sample array is extended by four loop samples and the position wraps only after it passes the end, so all four taps are always real samples and the loop has no seam.

### Filter

The recursion is the hardware's two-pole form. From cutoff `fc` (clamped to 30 to 8,000 Hz, as the hardware's register is) and resonance `res` in dB:

```
fc    = cutoff * 2^(egOct * eg2 + lfoOct * lfo)
theta = 2*pi*fc / 48000
Q     = 0.7071 * 10^(res / 20)
r     = exp(-theta / (2Q))
b1 = -2*r*cos(theta);  b2 = r*r;  K = g * (1 + b1 + b2)
y  = K*x - b1*y1 - b2*y2
```

`g = 10^(-res / 40)` lowers the passband by half the resonance so that a resonant sweep does not overwhelm the mix. The mapping from `Q` to pole radius and the value of `g` are project choices; the recursion and the limits are hardware. Because the cutoff tops out at 8 kHz, voices that should stay bright have the filter switched off.

## 9. 3D placement

Each voice has a start azimuth, an end azimuth and a move time. Azimuth is in degrees, positive to the right. The model is a rigid sphere, not the console's measured HRTF filters (project choice), with three cues for the ear on the far side:

- **Delay.** Woodworth's formula, `ITD = (a / c) * (theta + sin(theta))`, with head radius `a = 0.0875 m` and `c = 343 m/s`. At 48 kHz the constant `a / c` is 12.24 samples, so the largest delay is about 31 samples, which happens to be almost exactly one APU frame.
- **Level.** The far ear is scaled by `1 - 0.5 * |sin(theta)|`.
- **Shadow.** The far ear passes through a one-pole low-pass with coefficient `1 - 0.75 * |sin(theta)|`.

Delay, level and shadow are computed per frame and interpolated per sample. The delayed read uses linear interpolation. A moving source therefore gets a small, physically consistent pitch shift from the changing delay.

The reverb send is the voice's mono signal times its `send` level, taken before placement, so the reverb does not inherit the dry position.

## 10. Reverb

The reverb is described by I3DL2 parameters (standard): decay time, high-frequency decay ratio, reflections level and delay, reverb level and delay, diffusion and density. The structure that realises them is a project choice:

```
mono send -> one-pole input low-pass (inLp)
          -> delay line
               -> 6 early-reflection taps, alternating left and right, starting at reflDelay
               -> late tap at reflDelay + revDelay
                    -> 2 Schroeder all-passes, gain 0.7 * diffusion
                    -> 4-line feedback delay network, Householder feedback
left  = refl * (taps 0, 2, 4) + rev * (line0 - line2)
right = refl * (taps 1, 3, 5) + rev * (line1 - line3)
```

The four line lengths are 1847, 2311, 2797 and 3251 samples, scaled by `0.6 + 0.4 * density`. Each line's loop gain sets the decay: `g = 10^(-3 * len / (decay * 48000))`, which is 60 dB of decay in `decay` seconds. High-frequency damping is a one-pole low-pass in each loop whose coefficient is chosen so the gain at the top of the band corresponds to a decay time of `decay * hfRatio`. This treats "high frequency" as the Nyquist limit rather than I3DL2's 5 kHz reference, so real damping at 5 kHz is lighter than the parameter says (approximation).

Rooms, all project values set in the neighbourhood of the standard environment presets, with the longest decays shortened to keep effects short:

| Room | Decay s | HF ratio | Reflections dB, ms | Reverb dB, ms | Diffusion | Density | Input LP |
|---|---|---|---|---|---|---|---|
| room | 0.40 | 0.83 | -5, 2 | 2, 3 | 1.00 | 1.00 | 0.70 |
| corridor | 1.00 | 0.59 | -4, 7 | 3, 11 | 1.00 | 0.80 | 0.75 |
| chamber | 1.50 | 0.64 | -2, 12 | 2, 17 | 0.90 | 1.00 | 0.80 |
| hall | 2.00 | 0.70 | -6, 20 | 2, 29 | 1.00 | 1.00 | 0.70 |
| hangar | 2.40 | 0.35 | -2, 20 | 4, 30 | 0.85 | 0.70 | 0.45 |
| cavern | 2.60 | 0.90 | -1, 15 | 1, 22 | 0.80 | 0.90 | 0.95 |

## 11. Mix and output

The final signal is `dry + wet * reverb.wet`. Its peak is found, and the whole sound is scaled so the peak equals `0.89 * category.level` of full scale, then rounded to 16 bits. This is what an asset pipeline does when it normalises effects, and it makes clipping impossible whatever the hash produces. Category levels keep footsteps quieter than explosions. The file is trimmed 480 samples after the last sample above 12 LSB.

Measured at the default seed, the reverb contributes between about 1% of the energy (footstep in a corridor) and about 50% (door in a cavern).

## 12. Python blueprint

One file, standard library only. Patches are plain dictionaries with the same key names as the JavaScript objects, so `--params` output can be diffed against the browser.

```
zb_derive_params(name, seed) -> patch
zb_randomize(n, seed)        -> patch
zs_gen_source(source)        -> (pcm, loop_start)
zs_adpcm_codec(pcm)          -> (pcm, bytes)
zs_render_voice(pcm, loop_start, layer, note) -> (samples, amp per frame, cutoff per frame)
zs_spatialise(mono, layer)   -> (left, right)
zs_reverb(send, room)        -> (left, right)
zs_render(patch)             -> {left, right, sampleRate, gain, peak, sampleBytes, layerEnvs, layerCuts}
zs_checksum(render)          -> CRC-32 of the interleaved little-endian int16 stream
```

Points that are easy to get wrong:

- Python's `int()` truncates toward zero. Use a real floor for negative values; the delayed read in `zs_spatialise` depends on it.
- Python's `%` is always non-negative for a positive modulus and JavaScript's is not. Ring-buffer reads in JavaScript add the buffer length before taking the remainder.
- In JavaScript, `pos >> 16` overflows above 2^31. The port uses `Math.floor(pos / 65536)`.
- `a / b * c` and `a * c / b` round differently. Copy expressions exactly.
- Sum notes into the voice buffer in note order, and voices into the mix in voice order.

A render takes roughly 0.2 to 0.5 s in CPython and about 18 ms in Node. The inner loops are deliberately plain so they can be read against the JavaScript line by line.

## 13. Tuning guide

| To change | Edit | Effect |
|---|---|---|
| Overall brightness | `ZS_FMAX`, the harmonic cap `0.45` | higher values allow brighter, more alias-prone tones |
| How "wet" the era sounds | category `wetLow`, `wetHigh`; room `revDb` | larger values push sounds further back |
| Filter character | `resDb` ranges per archetype; `g` in the filter | more resonance gives a more vocal sweep |
| Sweep speed | the `rise` and `fall` times per archetype | shorter is snappier |
| Stereo width | category `azMax`; the 60° offset in `whoosh` | wider placement and travel |
| Head model strength | the 0.5 and 0.75 factors in `zs_spatialise` | stronger level and shadow cues |
| Punch of impacts | `impact` voice volumes; `tail` envelope family | louder sub or longer debris |
| Loudness balance | category `level` | relative peak level between categories |
| ADPCM share | `fmt_pool` in `layer()` | more ADPCM adds slight roughness and saves memory |

After any change, run `python build-demo.py` to refresh the checksum table and the demo, then compare the engines.

## 14. Verification method

1. **Reproducibility.** Render every sound twice and compare CRC-32s (`--verify`).
2. **Level.** Each render's peak must be between 4,000 and 32,767 (`--verify`).
3. **Cross-engine parity.** Extract the block between `// <ENGINE>` and `// </ENGINE>` from the `.jsx`, run it under Node, and compare CRC-32s with `--checksums` for every named sound and several unnamed patches at several seeds.
4. **In-page check.** The component stores the Python checksums for the default seed and compares its own renders against them.

A CRC match covers derivation, sources, codec, voice, placement, reverb and normalisation at once, because any difference anywhere changes the output bytes.

## 15. Integration notes

- **Games.** Render at load and cache. Renders are plain `Int16Array` pairs; the supplied `ZS3Engine` plays them through Web Audio, or they can be handed to any audio system.
- **Editing patches.** A patch is data. Changing `reverb.room` per level, `layers[i].az0` per emitter position, or `level` per mix group needs no engine change. Edited patches are still deterministic but no longer match the stored checksums.
- **TypeScript.** The engine block has no React or DOM dependencies and is the natural `.ts` module. The patch shape in section 5 and the render result in section 12 are the two interfaces to type.
- **Real-time use.** The control-frame structure maps directly onto an `AudioWorklet` that processes 32 samples at a time, if live parameter changes are wanted later. Bit-identity would then apply per voice rather than per file.

## 16. Open questions

- No listening tests have been done. Every range in this document is a starting point.
- The cubic interpolator, the amplitude smoothing and the sphere head model stand in for hardware behaviour that the sources used do not document.
- The reverb's damping reference is approximate (section 10).
- A parametric equaliser per voice, distance roll-off and a 5.1 mix are natural extensions and are not present.
