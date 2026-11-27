#!/usr/bin/env python3
# ============================================================================
# ZEROSOUNDS3.PY - Zero Sounds 3 (Xbox Classic style SFX synthesis)
#
# Reference engine and command-line tool. Deterministic sound effects in the
# style of the original Xbox audio processor (the MCPX APU). Every sound is
# three or four hardware-style voices; every voice is
#
#   procedural source sample -> PCM16 or Xbox ADPCM storage -> pitch stepping
#   in 1/4096-octave units -> DLS2-form resonant low-pass -> amplitude DAHDSR
#   -> 3D placement (interaural delay + head shadow)
#
# mixed at 48 kHz and sent through an I3DL2-parameter reverb. Control values
# (envelopes, LFO, pitch, filter) update once per 32-sample frame, 1500 times
# a second, as on the hardware.
#
# ZeroBytes rules: every parameter comes from hash(sound anchor, worldSeed).
# No random module, no stored presets, and no math.sin/exp/pow in the signal
# path, so the 16-bit output is bit-identical to ZeroSounds3.jsx.
#
# Standard library only. Python 3.8+.
# ============================================================================
import argparse
import json
import os
import struct
import sys
import zlib

ZS_SR = 48000
ZS_FRAME = 32                    # samples per APU frame
ZS_FPS = 1500                    # frames per second
ZS_DEFAULT_SEED = 0x1337BEEF
M32 = 0xFFFFFFFF

# ---------------------------------------------------------------------------
# 1. ZB HASH CORE (identical to ZeroSounds 1 and 2)
# ---------------------------------------------------------------------------
def _imul(a, b):
    return (a * b) & M32


def zb_hash(key, seed=0):
    h = (seed ^ 0x9E3779B9) & M32
    if isinstance(key, int):
        h ^= key & M32
        h = _imul(h, 0xCC9E2D51)
        h = ((h << 15) | (h >> 17)) & M32
        h = _imul(h, 0x1B873593)
        h ^= h >> 16
        h = _imul(h, 0x85EBCA6B)
        h ^= h >> 13
        h = _imul(h, 0xC2B2AE35)
        h ^= h >> 16
        return h
    for ch in key:
        h ^= ord(ch) & 0xFFFF
        h = _imul(h, 0x5BD1E995)
        h ^= h >> 15
    return h


def zb_float(h):
    return (h & 0xFFFFFF) / 16777216.0


def zb_range(h, lo, hi):
    return lo + zb_float(h) * (hi - lo)


def zb_int(h, lo, hi):
    return lo + (h % (hi - lo + 1))


def zb_pick(h, arr):
    return arr[h % len(arr)]


# ---------------------------------------------------------------------------
# 2. DETERMINISTIC MATH - only + - * / and floor, in a fixed order
# ---------------------------------------------------------------------------
ZS_PI = 3.141592653589793
ZS_TWO_PI = 6.283185307179586
ZS_HALF_PI = 1.5707963267948966
ZS_LN2 = 0.6931471805599453
ZS_DB2OCT = 0.16609640474436813     # log2(10) / 20: decibels -> octaves of gain
ZS_LOG2_10 = 3.321928094887362
ZS_S = [-1.0 / 6.0, 1.0 / 120.0, -1.0 / 5040.0, 1.0 / 362880.0,
        -1.0 / 39916800.0, 1.0 / 6227020800.0, -1.0 / 1307674368000.0]
ZS_E = [1.0 / 2.0, 1.0 / 6.0, 1.0 / 24.0, 1.0 / 120.0, 1.0 / 720.0, 1.0 / 5040.0,
        1.0 / 40320.0, 1.0 / 362880.0, 1.0 / 3628800.0, 1.0 / 39916800.0,
        1.0 / 479001600.0, 1.0 / 6227020800.0]


def _floor(x):
    i = int(x)
    return i - 1 if i > x else i


def dsin(x):
    r = x - _floor(x / ZS_TWO_PI + 0.5) * ZS_TWO_PI
    if r > ZS_HALF_PI:
        r = ZS_PI - r
    if r < -ZS_HALF_PI:
        r = -ZS_PI - r
    r2 = r * r
    p = ZS_S[6]
    for k in (5, 4, 3, 2, 1, 0):
        p = p * r2 + ZS_S[k]
    return r * (1.0 + r2 * p)


def dexp2(x):
    n = _floor(x)
    y = (x - n) * ZS_LN2
    p = ZS_E[11]
    for k in (10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0):
        p = p * y + ZS_E[k]
    e = 1.0 + y * (1.0 + y * p)
    while n > 0:
        e *= 2.0
        n -= 1
    while n < 0:
        e *= 0.5
        n += 1
    return e


def zs_rnd(x):
    return _floor(x + 0.5)


# ---------------------------------------------------------------------------
# 3. SOUND TAXONOMY (anchors only - no parameters are stored)
# ---------------------------------------------------------------------------
# level: peak target for the category, as a fraction of full scale.
ZB_CATEGORIES = {
    'ui':          {'index': 0, 'freqLow': 400, 'freqHigh': 1200, 'durLow': 0.05, 'durHigh': 0.18, 'wetLow': 0.10, 'wetHigh': 0.30, 'azMax': 25.0, 'level': 0.60, 'rooms': ['room', 'corridor', 'chamber']},
    'pickup':      {'index': 1, 'freqLow': 600, 'freqHigh': 1700, 'durLow': 0.08, 'durHigh': 0.30, 'wetLow': 0.18, 'wetHigh': 0.40, 'azMax': 35.0, 'level': 0.66, 'rooms': ['chamber', 'hall']},
    'movement':    {'index': 2, 'freqLow': 80,  'freqHigh': 360,  'durLow': 0.05, 'durHigh': 0.20, 'wetLow': 0.05, 'wetHigh': 0.20, 'azMax': 40.0, 'level': 0.56, 'rooms': ['room', 'corridor']},
    'combat':      {'index': 3, 'freqLow': 100, 'freqHigh': 800,  'durLow': 0.06, 'durHigh': 0.35, 'wetLow': 0.10, 'wetHigh': 0.30, 'azMax': 60.0, 'level': 0.84, 'rooms': ['corridor', 'chamber', 'hangar']},
    'impact':      {'index': 4, 'freqLow': 40,  'freqHigh': 180,  'durLow': 0.15, 'durHigh': 0.60, 'wetLow': 0.20, 'wetHigh': 0.45, 'azMax': 50.0, 'level': 0.89, 'rooms': ['chamber', 'hall', 'hangar', 'cavern']},
    'power':       {'index': 5, 'freqLow': 220, 'freqHigh': 900,  'durLow': 0.30, 'durHigh': 0.90, 'wetLow': 0.25, 'wetHigh': 0.50, 'azMax': 30.0, 'level': 0.74, 'rooms': ['hall', 'cavern']},
    'damage':      {'index': 6, 'freqLow': 110, 'freqHigh': 480,  'durLow': 0.10, 'durHigh': 0.60, 'wetLow': 0.10, 'wetHigh': 0.30, 'azMax': 45.0, 'level': 0.78, 'rooms': ['room', 'corridor', 'chamber']},
    'environment': {'index': 7, 'freqLow': 80,  'freqHigh': 600,  'durLow': 0.08, 'durHigh': 0.50, 'wetLow': 0.20, 'wetHigh': 0.45, 'azMax': 60.0, 'level': 0.70, 'rooms': ['corridor', 'hall', 'hangar', 'cavern']},
}
ZB_CATEGORY_ORDER = ['ui', 'pickup', 'movement', 'combat', 'impact', 'power', 'damage', 'environment']

# arch: glass | pad | arp | whoosh | impact | energy | mech.   dir: +1 rising, -1 falling, 0 hashed.
ZB_SOUNDS = {
    'navigate':   {'category': 'ui', 'arch': 'glass',  'styleIndex': 0, 'dir': 0,  'label': 'Navigate'},
    'select':     {'category': 'ui', 'arch': 'arp',    'styleIndex': 1, 'dir': 1,  'label': 'Select'},
    'back':       {'category': 'ui', 'arch': 'pad',    'styleIndex': 2, 'dir': -1, 'label': 'Back'},
    'pause':      {'category': 'ui', 'arch': 'arp',    'styleIndex': 3, 'dir': -1, 'label': 'Pause'},
    'notify':     {'category': 'ui', 'arch': 'pad',    'styleIndex': 4, 'dir': 0,  'label': 'Notify'},
    'error':      {'category': 'ui', 'arch': 'energy', 'styleIndex': 5, 'dir': -1, 'label': 'Error'},
    'save':       {'category': 'ui', 'arch': 'glass',  'styleIndex': 6, 'dir': 0,  'label': 'Save'},
    'ammo':       {'category': 'pickup', 'arch': 'mech',   'styleIndex': 0, 'dir': 0, 'label': 'Ammo'},
    'shield':     {'category': 'pickup', 'arch': 'energy', 'styleIndex': 1, 'dir': 1, 'label': 'Shield'},
    'key':        {'category': 'pickup', 'arch': 'glass',  'styleIndex': 2, 'dir': 0, 'label': 'Key'},
    'health':     {'category': 'pickup', 'arch': 'arp',    'styleIndex': 3, 'dir': 1, 'label': 'Health'},
    'jump':       {'category': 'movement', 'arch': 'whoosh', 'styleIndex': 0, 'dir': 1,  'label': 'Jump'},
    'land':       {'category': 'movement', 'arch': 'impact', 'styleIndex': 1, 'dir': 0,  'label': 'Land'},
    'dash':       {'category': 'movement', 'arch': 'whoosh', 'styleIndex': 2, 'dir': 1,  'label': 'Dash'},
    'footstep':   {'category': 'movement', 'arch': 'impact', 'styleIndex': 3, 'dir': 0,  'label': 'Footstep'},
    'roll':       {'category': 'movement', 'arch': 'whoosh', 'styleIndex': 4, 'dir': -1, 'label': 'Roll'},
    'melee':      {'category': 'combat', 'arch': 'whoosh', 'styleIndex': 0, 'dir': -1, 'label': 'Melee'},
    'gunshot':    {'category': 'combat', 'arch': 'impact', 'styleIndex': 1, 'dir': 0,  'label': 'Gunshot'},
    'plasma':     {'category': 'combat', 'arch': 'energy', 'styleIndex': 2, 'dir': -1, 'label': 'Plasma'},
    'reload':     {'category': 'combat', 'arch': 'mech',   'styleIndex': 3, 'dir': 0,  'label': 'Reload'},
    'ricochet':   {'category': 'combat', 'arch': 'energy', 'styleIndex': 4, 'dir': -1, 'label': 'Ricochet'},
    'shieldHit':  {'category': 'combat', 'arch': 'glass',  'styleIndex': 5, 'dir': 0,  'label': 'Shield hit'},
    'explosion':  {'category': 'impact', 'arch': 'impact', 'styleIndex': 0, 'dir': 0, 'label': 'Explosion'},
    'crash':      {'category': 'impact', 'arch': 'mech',   'styleIndex': 1, 'dir': 0, 'label': 'Crash'},
    'thud':       {'category': 'impact', 'arch': 'impact', 'styleIndex': 2, 'dir': 0, 'label': 'Thud'},
    'glassBreak': {'category': 'impact', 'arch': 'glass',  'styleIndex': 3, 'dir': 0, 'label': 'Glass break'},
    'powerUp':    {'category': 'power', 'arch': 'arp',    'styleIndex': 0, 'dir': 1, 'label': 'Power up'},
    'levelUp':    {'category': 'power', 'arch': 'arp',    'styleIndex': 1, 'dir': 1, 'label': 'Level up'},
    'recharge':   {'category': 'power', 'arch': 'energy', 'styleIndex': 2, 'dir': 1, 'label': 'Recharge'},
    'charge':     {'category': 'power', 'arch': 'energy', 'styleIndex': 3, 'dir': 1, 'label': 'Charge'},
    'checkpoint': {'category': 'power', 'arch': 'glass',  'styleIndex': 4, 'dir': 0, 'label': 'Checkpoint'},
    'hurt':       {'category': 'damage', 'arch': 'energy', 'styleIndex': 0, 'dir': -1, 'label': 'Hurt'},
    'death':      {'category': 'damage', 'arch': 'arp',    'styleIndex': 1, 'dir': -1, 'label': 'Death'},
    'alarm':      {'category': 'damage', 'arch': 'arp',    'styleIndex': 2, 'dir': 1,  'label': 'Alarm'},
    'warning':    {'category': 'damage', 'arch': 'pad',    'styleIndex': 3, 'dir': 0,  'label': 'Warning'},
    'door':       {'category': 'environment', 'arch': 'whoosh', 'styleIndex': 0, 'dir': -1, 'label': 'Door'},
    'terminal':   {'category': 'environment', 'arch': 'arp',    'styleIndex': 1, 'dir': 1,  'label': 'Terminal'},
    'splash':     {'category': 'environment', 'arch': 'whoosh', 'styleIndex': 2, 'dir': -1, 'label': 'Splash'},
    'teleport':   {'category': 'environment', 'arch': 'energy', 'styleIndex': 3, 'dir': 1,  'label': 'Teleport'},
    'switch':     {'category': 'environment', 'arch': 'mech',   'styleIndex': 4, 'dir': 0,  'label': 'Switch'},
}
ZS_ARCHETYPES = ['glass', 'pad', 'arp', 'whoosh', 'impact', 'energy', 'mech']
ZS_ARP_PATTERNS = [[0, 4, 7, 12, 16], [0, 7, 12, 19, 24], [0, 5, 7, 12, 17],
                   [0, 3, 7, 10, 15], [0, 12, 7, 12, 19], [0, 6, 0, 6, 0],
                   [0, 2, 4, 7, 9], [0, 12, 0, 12, 0]]
ZS_FM_RATIOS = [1.0, 2.0, 3.0, 3.5, 4.0, 5.0, 7.0, 0.5, 1.5, 2.5]
ZS_WAVE_SHAPES = ['saw', 'pulse', 'tri', 'organ', 'glass', 'soft', 'formant']
ZS_WAVE_CYCLES = [1024, 512, 256, 128, 64]
ZS_FM_CYCLES = [512, 256, 128, 64]
ZS_FMAX = 6000.0                 # highest fundamental a tonal voice may reach, Hz
# Partial ratios. bar, membrane and tube are the textbook ideal cases; shard is invented.
ZS_MATERIALS = {
    'bar':      [1.0, 2.756, 5.404, 8.933, 13.344, 18.64],
    'membrane': [1.0, 1.594, 2.136, 2.296, 2.653, 2.918],
    'tube':     [1.0, 3.0, 5.0, 7.0, 9.0, 11.0],
    'shard':    [1.0, 1.71, 2.89, 3.94, 5.33, 7.12],
}
ZS_MATERIAL_ORDER = ['bar', 'membrane', 'tube', 'shard']

# Amplitude-envelope (EG1) families, seconds. decay and release are the time to fall 96 dB.
ZS_ENV = {
    'perc':  {'attack': [0.001, 0.006], 'hold': [0.0, 0.010], 'decay': [0.25, 0.90], 'sustain': [0.0, 0.0],   'release': [0.10, 0.35]},
    'pluck': {'attack': [0.001, 0.004], 'hold': [0.0, 0.005], 'decay': [0.50, 1.60], 'sustain': [0.0, 0.20],  'release': [0.20, 0.60]},
    'sus':   {'attack': [0.006, 0.050], 'hold': [0.0, 0.0],   'decay': [0.30, 0.90], 'sustain': [0.55, 0.90], 'release': [0.10, 0.40]},
    'swell': {'attack': [0.050, 0.220], 'hold': [0.0, 0.0],   'decay': [0.40, 1.00], 'sustain': [0.60, 0.90], 'release': [0.20, 0.60]},
    'hit':   {'attack': [0.0005, 0.002], 'hold': [0.0, 0.004], 'decay': [0.06, 0.22], 'sustain': [0.0, 0.0],  'release': [0.04, 0.10]},
    'tail':  {'attack': [0.002, 0.012], 'hold': [0.0, 0.020], 'decay': [0.80, 2.40], 'sustain': [0.0, 0.0],   'release': [0.40, 1.00]},
}
# Reverb environments in I3DL2 terms. These are this project's own values, set in the
# neighbourhood of the standard environment presets; they are not copies of that table.
ZS_ROOMS = {
    'room':     {'decay': 0.40, 'hfRatio': 0.83, 'reflDb': -5.0,  'reflDelay': 0.002, 'revDb': 2.0, 'revDelay': 0.003, 'diffusion': 1.00, 'density': 1.00, 'inLp': 0.70},
    'corridor': {'decay': 1.00, 'hfRatio': 0.59, 'reflDb': -4.0,  'reflDelay': 0.007, 'revDb': 3.0, 'revDelay': 0.011, 'diffusion': 1.00, 'density': 0.80, 'inLp': 0.75},
    'chamber':  {'decay': 1.50, 'hfRatio': 0.64, 'reflDb': -2.0,  'reflDelay': 0.012, 'revDb': 2.0, 'revDelay': 0.017, 'diffusion': 0.90, 'density': 1.00, 'inLp': 0.80},
    'hall':     {'decay': 2.00, 'hfRatio': 0.70, 'reflDb': -6.0, 'reflDelay': 0.020, 'revDb': 2.0, 'revDelay': 0.029, 'diffusion': 1.00, 'density': 1.00, 'inLp': 0.70},
    'hangar':   {'decay': 2.40, 'hfRatio': 0.35, 'reflDb': -2.0,  'reflDelay': 0.020, 'revDb': 4.0, 'revDelay': 0.030, 'diffusion': 0.85, 'density': 0.70, 'inLp': 0.45},
    'cavern':   {'decay': 2.60, 'hfRatio': 0.90, 'reflDb': -1.0,  'reflDelay': 0.015, 'revDb': 1.0, 'revDelay': 0.022, 'diffusion': 0.80, 'density': 0.90, 'inLp': 0.95},
}


# ---------------------------------------------------------------------------
# 4. PARAMETER DERIVATION  (world -> category -> sound -> layer -> slot)
# ---------------------------------------------------------------------------
def zs_derive_eg1(h, family):
    e = ZS_ENV[family]
    return {
        'delay': 0.0,
        'attack': zb_range(h(20), e['attack'][0], e['attack'][1]),
        'hold': zb_range(h(21), e['hold'][0], e['hold'][1]),
        'decay': zb_range(h(22), e['decay'][0], e['decay'][1]),
        'sustain': zb_range(h(23), e['sustain'][0], e['sustain'][1]),
        'release': zb_range(h(24), e['release'][0], e['release'][1]),
    }


def zs_derive_source(kind, h, seed, fmax, o):
    if kind == 'wave':
        cycle = 64
        for c in ZS_WAVE_CYCLES:
            if fmax * c / ZS_SR <= 8.0:
                cycle = c
                break
        hcap = _floor(0.45 * ZS_SR / fmax)
        if hcap > (cycle >> 1) - 1:
            hcap = (cycle >> 1) - 1
        harm = zb_pick(h(13), [12, 24, 48, 96])
        if harm > hcap:
            harm = hcap
        if harm < 1:
            harm = 1
        shape = o['shape'] if 'shape' in o else zb_pick(h(11), ZS_WAVE_SHAPES)
        return {'kind': 'wave', 'cycle': cycle, 'shape': shape,
                'duty': zb_range(h(12), 0.125, 0.5), 'harmonics': harm,
                'formant': zb_range(h(14), 3.0, 14.0), 'seed': seed}
    if kind == 'fm':
        cycle = 64
        for c in ZS_FM_CYCLES:
            if fmax * c / ZS_SR <= 4.0:
                cycle = c
                break
        cands = [r for r in ZS_FM_RATIOS if r * fmax <= 9000.0]
        if len(cands) == 0:
            cands = [1.0]
        ratio = zb_pick(h(11), cands)
        index0 = zb_range(h(12), 1.0, 6.0)
        icap = (18000.0 / fmax - 1.0) / ratio      # keep the widest sideband under 18 kHz
        if icap < 0.3:
            icap = 0.3
        if index0 > icap:
            index0 = icap
        index_loop = zb_range(h(13), 0.1, 1.0)
        if index_loop > index0:
            index_loop = index0
        return {'kind': 'fm', 'cycle': cycle, 'ratio': ratio, 'index0': index0, 'indexLoop': index_loop,
                'attackCycles': 2 * zb_int(h(14), 3, 24), 'seed': seed}
    if kind == 'noise':
        return {'kind': 'noise', 'length': 16384, 'color': zb_range(h(11), 0.10, 1.0), 'seed': seed}
    if kind == 'modal':
        material = o['material'] if 'material' in o else zb_pick(h(11), ZS_MATERIAL_ORDER)
        return {'kind': 'modal', 'material': material, 'f0': zb_range(h(12), 180.0, 700.0),
                't60': zb_range(h(13), 0.12, 0.50), 'tilt': zb_range(h(14), 0.2, 1.2),
                'damp': zb_range(h(15), 0.3, 1.5), 'partials': zb_int(h(16), 4, 6), 'seed': seed}
    return {'kind': 'thump', 'length': _floor(zb_range(h(11), 0.12, 0.35) * ZS_SR),
            'f1': zb_range(h(12), 90.0, 260.0), 'f2': zb_range(h(13), 30.0, 60.0), 'seed': seed}


def zs_derive(name, category, arch, style_index, dir_in, world_seed):
    cat = ZB_CATEGORIES[category]
    cat_seed = zb_hash(cat['index'], world_seed)
    sound_seed = zb_hash(style_index, cat_seed)

    def hs(k):
        return zb_hash(100 + k, sound_seed)

    base_freq = zb_range(hs(0), cat['freqLow'], cat['freqHigh'])
    hold = zb_range(hs(1), cat['durLow'], cat['durHigh'])
    reverb = {'room': zb_pick(hs(2), cat['rooms']), 'wet': zb_range(hs(3), cat['wetLow'], cat['wetHigh'])}
    direction = dir_in
    if direction == 0:
        direction = 1 if (hs(7) & 1) else -1
    az = zb_range(hs(9), -cat['azMax'], cat['azMax'])

    def layer(i, kind, role, family, o):
        seed = zb_hash(i + 1, sound_seed)

        def h(k):
            return zb_hash(k, seed)

        notes = o['notes'] if 'notes' in o else [{'t': 0.0, 'ratio': 1.0, 'gain': 1.0}]
        peg = o.get('pegOct', 0.0)
        vib = o.get('vibOct', 0.0)
        has_freq = 'freq' in o
        freq = None
        fmax = 0.0
        if has_freq:
            top = 0.0
            for nt in notes:
                if nt['ratio'] > top:
                    top = nt['ratio']
            top = top * dexp2((peg if peg > 0.0 else 0.0) + vib)
            freq = o['freq']
            if freq > ZS_FMAX / top:
                freq = ZS_FMAX / top
            fmax = freq * top
        source = zs_derive_source(kind, h, seed, fmax, o)
        if not has_freq:
            step0 = o['step0']
        elif kind == 'modal':
            step0 = freq / source['f0']
            if step0 > 4.0:
                step0 = 4.0
            if step0 < 0.25:
                step0 = 0.25
        else:
            step0 = freq * source['cycle'] / ZS_SR
        hd = o['hold'] if 'hold' in o else hold
        eg1 = zs_derive_eg1(h, family)
        if 'delay' in o:
            eg1['delay'] = o['delay']
        eg2 = o['eg2'] if 'eg2' in o else {'attack': 0.0, 'decay': zb_range(h(25), 0.05, 0.40), 'sustain': 0.0, 'release': 0.10}
        flt = o['filter'] if 'filter' in o else None
        if flt is None:
            flt = {'on': False, 'cutoff': 8000.0, 'resDb': 0.0, 'egOct': 0.0, 'lfoOct': 0.0}
        fmt_pool = ['adpcm', 'adpcm', 'pcm16'] if (kind == 'noise' or kind == 'thump') else ['pcm16', 'pcm16', 'adpcm']
        az0 = o['az'] if 'az' in o else 0.0
        az1 = o['azTo'] if 'azTo' in o else az0
        return {
            'role': role, 'source': source, 'format': zb_pick(h(27), fmt_pool),
            'freq': freq, 'step0': step0, 'notes': notes, 'hold': hd,
            'eg1': eg1, 'eg2': eg2,
            'pegReg': zs_rnd(peg * 128.0),          # EG2 -> pitch, in units of 32/4096 octave
            'filter': flt,
            'lfo': {'rate': o.get('lfoRate', zb_range(h(30), 4.0, 9.0)), 'delay': o.get('lfoDelay', 0.0),
                    'pitchOct': vib, 'trem': o.get('trem', 0.0)},
            'vol': o.get('vol', 0.5),
            'az0': az0, 'az1': az1, 'moveTime': o['moveTime'] if 'moveTime' in o else hd,
            'send': o.get('send', 1.0),
        }

    def rise(t):
        # EG2 shape that climbs 0 -> 1 over t seconds and stays there
        return {'attack': t if t > 0.004 else 0.004, 'decay': 1.0, 'sustain': 1.0, 'release': 0.25}

    def fall(t):
        # EG2 shape that starts at 1 and falls to 0 over t seconds
        return {'attack': 0.0, 'decay': t if t > 0.004 else 0.004, 'sustain': 0.0, 'release': 0.10}

    L = []
    if arch == 'glass':
        bright = zb_range(hs(10), 2.5, 7.0)
        ft = zb_range(hs(11), 0.08, 0.45)
        L.append(layer(0, 'fm', 'body', 'pluck', {
            'freq': base_freq, 'vol': 0.50, 'az': az, 'vibOct': zb_range(hs(12), 0.0, 0.004),
            'eg2': fall(ft),
            'filter': {'on': True, 'cutoff': base_freq * bright, 'resDb': zb_range(hs(13), 2.0, 10.0),
                       'egOct': zb_range(hs(14), 0.5, 2.0), 'lfoOct': 0.0}}))
        L.append(layer(1, 'fm', 'shimmer', 'pluck', {
            'freq': base_freq * zb_pick(hs(15), [2.0, 2.0, 3.0, 1.5, 4.0]) * (1.0 + zb_range(hs(16), 0.002, 0.007)),
            'vol': 0.20, 'az': -az}))
        L.append(layer(2, 'wave', 'air', 'pluck', {
            'freq': base_freq * zb_pick(hs(17), [4.0, 5.0, 6.0, 8.0]), 'shape': 'glass',
            'vol': 0.08, 'az': az * 0.5, 'delay': zb_range(hs(18), 0.0, 0.02)}))
        L.append(layer(3, 'wave', 'sub', 'perc', {'freq': base_freq * 0.5, 'shape': 'sine', 'vol': 0.26, 'send': 0.3}))
    elif arch == 'pad':
        peg = direction * zb_range(hs(10), 0.0, 0.4)
        det = zb_range(hs(11), 0.004, 0.011)
        open_oct = zb_range(hs(12), 0.8, 2.5)
        f = {'on': True, 'cutoff': base_freq * zb_range(hs(13), 1.5, 4.0), 'resDb': zb_range(hs(14), 4.0, 14.0),
             'egOct': open_oct, 'lfoOct': zb_range(hs(15), 0.0, 0.5)}
        e2 = rise(hold * zb_range(hs(16), 0.3, 1.0)) if direction > 0 else fall(hold * zb_range(hs(16), 0.6, 2.0))
        L.append(layer(0, 'wave', 'body', 'sus', {'freq': base_freq, 'vol': 0.42, 'az': az, 'pegOct': peg, 'eg2': e2, 'filter': f}))
        L.append(layer(1, 'wave', 'chorus', 'sus', {'freq': base_freq * (1.0 + det), 'vol': 0.30, 'az': -az, 'pegOct': peg, 'eg2': e2, 'filter': f}))
        L.append(layer(2, 'wave', 'sub', 'sus', {'freq': base_freq * 0.5, 'shape': 'sine', 'vol': 0.24, 'pegOct': peg, 'eg2': e2, 'send': 0.3}))
    elif arch == 'arp':
        pat = zb_pick(hs(5), ZS_ARP_PATTERNS)
        count = zb_int(hs(4), 2, len(pat))
        gap = zb_range(hs(6), 0.045, 0.11)
        notes = []
        for i in range(count):
            notes.append({'t': i * gap, 'ratio': dexp2(direction * pat[i] / 12.0), 'gain': 1.0})
        kind = 'fm' if (hs(8) & 1) else 'wave'
        note_hold = hold if hold < gap * 1.5 else gap * 1.5
        f = {'on': True, 'cutoff': base_freq * zb_range(hs(10), 2.0, 6.0), 'resDb': zb_range(hs(11), 3.0, 12.0),
             'egOct': zb_range(hs(12), 0.5, 2.5), 'lfoOct': 0.0}
        e2 = fall(zb_range(hs(13), 0.04, 0.20))
        L.append(layer(0, kind, 'body', 'perc', {'freq': base_freq, 'vol': 0.36, 'az': az, 'hold': note_hold, 'notes': notes, 'eg2': e2, 'filter': f}))
        L.append(layer(1, 'wave' if kind == 'fm' else 'fm', 'octave', 'perc', {'freq': base_freq * 2.0, 'vol': 0.14, 'az': -az, 'hold': note_hold, 'notes': notes}))
        L.append(layer(2, 'wave', 'wash', 'swell', {'freq': base_freq * (0.5 if direction > 0 else 1.0), 'shape': 'soft', 'vol': 0.12,
                                                    'hold': (count - 1) * gap + note_hold, 'az': az * 0.3}))
    elif arch == 'whoosh':
        sweep = zb_range(hs(10), 1.5, 3.5)
        start = zb_range(hs(11), 250.0, 700.0) if direction > 0 else zb_range(hs(11), 2500.0, 7000.0)
        frac = zb_range(hs(12), 0.7, 1.6)
        res = zb_range(hs(13), 6.0, 16.0)
        wide = az + (60.0 if (hs(14) & 1) else -60.0)
        st = zb_range(hs(15), 0.6, 1.6)
        e2 = rise(hold * frac)
        L.append(layer(0, 'noise', 'body', 'swell', {
            'step0': st, 'vol': 0.55, 'az': -wide, 'azTo': wide, 'moveTime': hold * 1.5, 'eg2': e2,
            'filter': {'on': True, 'cutoff': start, 'resDb': res, 'egOct': direction * sweep, 'lfoOct': 0.0}}))
        L.append(layer(1, 'noise', 'width', 'swell', {
            'step0': st * zb_range(hs(16), 0.5, 0.8), 'vol': 0.32, 'az': wide * 0.6, 'azTo': -wide * 0.6, 'moveTime': hold * 1.5, 'eg2': e2,
            'filter': {'on': True, 'cutoff': start * 0.6, 'resDb': res * 0.5, 'egOct': direction * sweep, 'lfoOct': 0.0}}))
        L.append(layer(2, 'fm' if (hs(17) & 1) else 'wave', 'tone', 'swell', {
            'freq': base_freq, 'vol': 0.16, 'az': az, 'pegOct': direction * zb_range(hs(18), 0.4, 1.4), 'eg2': e2, 'send': 0.6}))
    elif arch == 'impact':
        big = hold >= 0.15
        k = 0.5 if big else 1.0
        L.append(layer(0, 'noise', 'crack', 'tail' if big else 'hit', {
            'step0': zb_range(hs(10), 0.8, 2.0) * k, 'vol': 0.50, 'az': az, 'eg2': fall(zb_range(hs(11), 0.03, 0.20)),
            'filter': {'on': True, 'cutoff': zb_range(hs(12), 500.0, 2500.0) * k, 'resDb': zb_range(hs(13), 0.0, 8.0),
                       'egOct': zb_range(hs(14), 1.0, 3.0), 'lfoOct': 0.0}}))
        L.append(layer(1, 'modal', 'body', 'perc', {'freq': base_freq * zb_range(hs(15), 1.5, 4.0), 'vol': 0.42, 'az': az * 0.6,
                                                    'hold': hold if hold > 0.12 else 0.12}))
        L.append(layer(2, 'thump', 'sub', 'perc', {'step0': zb_range(hs(16), 0.6, 1.2) * (0.7 if big else 1.0), 'vol': 0.72, 'send': 0.5 if big else 0.25,
                                                   'hold': hold if hold > 0.15 else 0.15}))
        if big:
            L.append(layer(3, 'noise', 'debris', 'tail', {
                'step0': zb_range(hs(17), 0.3, 0.8), 'vol': 0.42, 'az': -az, 'azTo': az, 'moveTime': 1.0, 'eg2': fall(zb_range(hs(18), 0.3, 1.2)),
                'filter': {'on': True, 'cutoff': zb_range(hs(19), 250.0, 900.0), 'resDb': 2.0, 'egOct': zb_range(hs(20), 1.0, 2.5), 'lfoOct': 0.0}}))
    elif arch == 'energy':
        peg = direction * zb_range(hs(10), 0.7, 2.5)
        frac = zb_range(hs(11), 0.6, 1.2)
        kind = 'fm' if (hs(12) & 1) else 'wave'
        rate = zb_range(hs(13), 5.0, 30.0)
        vib = zb_range(hs(14), 0.0, 0.08)
        e2 = rise(hold * frac)
        f = {'on': True, 'cutoff': base_freq * zb_range(hs(15), 1.5, 5.0), 'resDb': zb_range(hs(16), 6.0, 18.0),
             'egOct': peg * zb_range(hs(17), 0.6, 1.2), 'lfoOct': zb_range(hs(18), 0.0, 0.8)}
        L.append(layer(0, kind, 'carrier', 'sus', {'freq': base_freq, 'vol': 0.46, 'az': az, 'pegOct': peg, 'vibOct': vib, 'lfoRate': rate,
                                                   'eg2': e2, 'filter': f, 'trem': zb_range(hs(19), 0.0, 0.5)}))
        L.append(layer(1, kind, 'chorus', 'sus', {'freq': base_freq * (1.0 + zb_range(hs(20), 0.004, 0.012)), 'vol': 0.24, 'az': -az,
                                                  'pegOct': peg, 'vibOct': vib, 'lfoRate': rate, 'eg2': e2, 'filter': f}))
        L.append(layer(2, 'wave', 'sub', 'sus', {'freq': base_freq * 0.5, 'shape': 'sine', 'vol': 0.26, 'pegOct': peg, 'eg2': e2, 'send': 0.3}))
        L.append(layer(3, 'noise', 'fizz', 'swell', {
            'step0': zb_range(hs(21), 1.0, 2.0), 'vol': 0.07, 'az': az * 0.5, 'eg2': e2,
            'filter': {'on': True, 'cutoff': 1500.0 if direction > 0 else 6000.0, 'resDb': 6.0, 'egOct': direction * 2.0, 'lfoOct': 0.0}}))
    else:  # mech
        count = zb_int(hs(10), 2, 4)
        notes = []
        t = 0.0
        for i in range(count):
            notes.append({'t': t, 'ratio': zb_range(hs(30 + i), 0.7, 1.5), 'gain': 1.0 if i == count - 1 else zb_range(hs(40 + i), 0.5, 0.9)})
            t = t + zb_range(hs(50 + i), 0.035, 0.035 + hold * 0.6)
        last = notes[count - 1]['t']
        L.append(layer(0, 'noise', 'click', 'hit', {
            'step0': zb_range(hs(11), 0.8, 1.8), 'vol': 0.42, 'az': az, 'hold': 0.01, 'notes': notes, 'eg2': fall(zb_range(hs(12), 0.01, 0.05)),
            'filter': {'on': True, 'cutoff': zb_range(hs(13), 900.0, 3000.0), 'resDb': zb_range(hs(14), 4.0, 12.0),
                       'egOct': zb_range(hs(15), 0.5, 1.4), 'lfoOct': 0.0}}))
        L.append(layer(1, 'modal', 'clank', 'perc', {'freq': base_freq * zb_range(hs(16), 2.0, 5.0), 'vol': 0.40, 'az': -az * 0.5,
                                                     'hold': 0.06, 'notes': notes}))
        L.append(layer(2, 'thump', 'weight', 'perc', {'step0': zb_range(hs(17), 0.9, 1.6), 'vol': 0.40, 'hold': 0.08, 'send': 0.3,
                                                      'notes': [{'t': last, 'ratio': 1.0, 'gain': 1.0}]}))
        L.append(layer(3, 'wave', 'servo', 'sus', {'freq': base_freq, 'vol': 0.10, 'az': az, 'hold': last if last > 0.04 else 0.04,
                                                   'pegOct': direction * zb_range(hs(18), 0.2, 1.0), 'eg2': rise(last), 'send': 0.6}))

    return {'name': name, 'category': category, 'archetype': arch, 'direction': direction,
            'worldSeed': world_seed & M32, 'soundSeed': sound_seed, 'baseFreq': base_freq, 'hold': hold,
            'azimuth': az, 'level': cat['level'], 'reverb': reverb, 'layers': L}


def zb_derive_params(name, world_seed=ZS_DEFAULT_SEED):
    """Named sound -> patch. O(1), pure."""
    if name not in ZB_SOUNDS:
        raise KeyError('Unknown sound: %s' % name)
    s = ZB_SOUNDS[name]
    return zs_derive(name, s['category'], s['arch'], s['styleIndex'], s['dir'], world_seed & M32)


def zb_randomize(n, world_seed=ZS_DEFAULT_SEED):
    """Unnamed sound at position n of the infinite patch space."""
    v = zb_hash(n, (world_seed ^ 0x5A5A5A5A) & M32)
    category = ZB_CATEGORY_ORDER[zb_int(zb_hash(0, v), 0, 7)]
    arch = ZS_ARCHETYPES[zb_int(zb_hash(1, v), 0, 6)]
    return zs_derive('patch#%d' % n, category, arch, 1000 + (v & 0xFFFF), 0, world_seed & M32)


# ---------------------------------------------------------------------------
# 5. PROCEDURAL SOURCE SAMPLES (16-bit, written at 48 kHz)
# ---------------------------------------------------------------------------
def zs_wave_amp(src, k):
    jit = 0.85 + 0.3 * zb_float(zb_hash(300 + k, src['seed']))
    shape = src['shape']
    if shape == 'sine':
        return 1.0 if k == 1 else 0.0
    if shape == 'saw':
        a = 1.0 / k
    elif shape == 'pulse':
        a = dsin(ZS_PI * k * src['duty']) / k
    elif shape == 'tri':
        a = 0.0 if k % 2 == 0 else (1.0 if (k >> 1) % 2 == 0 else -1.0) / (k * k)
    elif shape == 'organ':
        a = zb_range(zb_hash(200 + k, src['seed']), 0.2, 1.0) if k in (1, 2, 3, 4, 6, 8) else 0.0
    elif shape == 'glass':
        a = 1.0 / k if k in (1, 3, 4, 7, 9, 13) else 0.0
    elif shape == 'soft':
        a = 1.0 / (k * k)
    else:  # formant: a saw with one resonant bump
        d = (k - src['formant']) / 2.0
        a = (1.0 + 4.0 / (1.0 + d * d)) / k
    return a * jit


def zs_gen_source(src):
    """-> (pcm: list of int, loop_start: int, -1 = one-shot)"""
    kind = src['kind']
    if kind == 'wave':
        P = src['cycle']
        tab = [dsin(ZS_TWO_PI * m / P) for m in range(P)]
        y = [0.0] * P
        for k in range(1, src['harmonics'] + 1):
            a = zs_wave_amp(src, k)
            if a != 0.0:
                for n in range(P):
                    y[n] = y[n] + a * tab[(k * n) % P]
        peak = 0.0
        for n in range(P):
            v = y[n] if y[n] >= 0.0 else -y[n]
            if v > peak:
                peak = v
        return [zs_rnd(26000.0 * y[n] / peak) for n in range(P)], 0
    if kind == 'fm':
        P = src['cycle']
        n_att = src['attackCycles'] * P
        N = n_att + 2 * P
        out = [0] * N
        for n in range(N):
            t = 1.0 - n / n_att if n < n_att else 0.0
            idx = src['indexLoop'] + (src['index0'] - src['indexLoop']) * (t * t)
            ph = ZS_TWO_PI * n / P
            out[n] = zs_rnd(26000.0 * dsin(ph + idx * dsin(src['ratio'] * ph)))
        return out, n_att
    if kind == 'noise':
        N = src['length']
        y = [0.0] * N
        z = 0.0
        peak = 0.0
        color = src['color']
        seed = src['seed']
        for n in range(N):
            w = zb_float(zb_hash(n, seed)) * 2.0 - 1.0
            z = z + color * (w - z)
            y[n] = z
            v = z if z >= 0.0 else -z
            if v > peak:
                peak = v
        return [zs_rnd(24000.0 * y[n] / peak) for n in range(N)], 0
    if kind == 'modal':
        t60 = src['t60']
        N = _floor(t60 * 1.25 * ZS_SR)
        ratios = ZS_MATERIALS[src['material']]
        y = [0.0] * N
        for j in range(src['partials']):
            f = src['f0'] * ratios[j] * (0.97 + 0.06 * zb_float(zb_hash(400 + j, src['seed'])))
            if f * 4.0 > 20000.0:
                continue
            a = 1.0 / (1.0 + j * src['tilt'])
            r = dexp2(-(3.0 * ZS_LOG2_10) / (t60 / (1.0 + j * src['damp']) * ZS_SR))
            w = ZS_TWO_PI * f / ZS_SR
            env = a
            for n in range(N):
                y[n] = y[n] + env * dsin(w * n)
                env = env * r
        peak = 0.0
        for n in range(N):
            v = y[n] if y[n] >= 0.0 else -y[n]
            if v > peak:
                peak = v
        out = [0] * N
        for n in range(N):
            fade = (N - n) / 64.0 if N - n < 64 else 1.0
            out[n] = zs_rnd(28000.0 * y[n] / peak * fade)
        return out, -1
    # thump: sine whose pitch falls f1 -> f2, phase integrated in closed form
    N = src['length']
    c1 = src['f1'] / ZS_SR
    c2 = src['f2'] / ZS_SR
    out = [0] * N
    for n in range(N):
        t = 1.0 - n / N
        t2 = t * t
        ph = ZS_TWO_PI * (c2 * n + (c1 - c2) * N * 0.25 * (1.0 - t2 * t2))
        out[n] = zs_rnd(28000.0 * (dsin(ph) * t2))
    return out, -1


# ---------------------------------------------------------------------------
# 6. XBOX ADPCM (IMA ADPCM in 36-byte blocks: 4-byte header + 64 nibbles = 65 samples)
# ---------------------------------------------------------------------------
ZS_IMA_STEP = [7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
               50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230,
               253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963,
               1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327,
               3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487,
               12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767]
ZS_IMA_INDEX = [-1, -1, -1, -1, 2, 4, 6, 8]


def zs_adpcm_codec(s):
    """Encode then decode. -> (pcm: list of int, bytes used)"""
    N = len(s)
    out = [0] * N
    index = 0
    blocks = 0
    b = 0
    while b < N:
        blocks += 1
        pred = s[b]                  # the block header carries the first sample verbatim
        out[b] = pred
        end = b + 65 if b + 65 < N else N
        for i in range(b + 1, end):
            step = ZS_IMA_STEP[index]
            diff = s[i] - pred
            nib = 0
            if diff < 0:
                nib = 8
                diff = -diff
            d = step >> 3
            if diff >= step:
                nib |= 4
                diff -= step
                d += step
            if diff >= (step >> 1):
                nib |= 2
                diff -= step >> 1
                d += step >> 1
            if diff >= (step >> 2):
                nib |= 1
                d += step >> 2
            pred = pred - d if (nib & 8) else pred + d
            if pred > 32767:
                pred = 32767
            elif pred < -32768:
                pred = -32768
            index += ZS_IMA_INDEX[nib & 7]
            if index < 0:
                index = 0
            elif index > 88:
                index = 88
            out[i] = pred
        b += 65
    return out, blocks * 36


# ---------------------------------------------------------------------------
# 7. VOICE: envelopes, LFO, pitch stepping, DLS2-form low-pass
# ---------------------------------------------------------------------------
def zs_eg1_pre(e, t):
    """Amplitude envelope before key-off, 0..1."""
    t = t - e['delay']
    if t < 0.0:
        return 0.0
    if t < e['attack']:
        return t / e['attack']
    t = t - e['attack']
    if t < e['hold']:
        return 1.0
    t = t - e['hold']
    db = 96.0 * t / e['decay']
    sus = 96.0 * (1.0 - e['sustain'])
    if db > sus:
        db = sus
    return dexp2(-db * ZS_DB2OCT)


def zs_eg1(e, t, T):
    if t < T:
        return zs_eg1_pre(e, t)
    return zs_eg1_pre(e, T) * dexp2(-(96.0 * (t - T) / e['release']) * ZS_DB2OCT)


def zs_eg2_pre(e, t):
    """Modulation envelope before key-off, 0..1, linear segments."""
    if t < e['attack']:
        return t / e['attack']
    v = 1.0 - (t - e['attack']) / e['decay']
    return v if v > e['sustain'] else e['sustain']


def zs_eg2(e, t, T):
    if t < T:
        return zs_eg2_pre(e, t)
    v = zs_eg2_pre(e, T) * (1.0 - (t - T) / e['release'])
    return v if v > 0.0 else 0.0


def zs_render_voice(pcm, loop_start, layer, note):
    """One key-on of one voice -> (out: list of float, amp per frame, cutoff Hz per frame or None)"""
    T = layer['hold']
    e1 = layer['eg1']
    e2 = layer['eg2']
    lfo = layer['lfo']
    flt = layer['filter']
    F = _floor((T + e1['release']) * ZS_FPS) + 1
    n = F * ZS_FRAME
    L = len(pcm)
    looped = loop_start >= 0
    loop_len = L - loop_start
    ext = [0] + pcm + (pcm[loop_start:loop_start + 4] if looped else [0, 0, 0, 0])
    base = layer['step0'] * note['ratio']
    gain = layer['vol'] * note['gain']
    q = 0.7071067811865476 * dexp2(flt['resDb'] * ZS_DB2OCT)
    g = dexp2(-0.5 * flt['resDb'] * ZS_DB2OCT)
    filt = flt['on']

    amp = [0.0] * (F + 1)
    steps = [0] * F
    cb1 = [0.0] * F
    cb2 = [0.0] * F
    cK = [0.0] * F
    cuts = [0.0] * F if filt else None
    for k in range(F):
        t = k / 1500.0
        lv = 0.0
        if t >= lfo['delay']:
            lv = dsin(ZS_TWO_PI * lfo['rate'] * (t - lfo['delay']))
        m = zs_eg2(e2, t, T)
        amp[k] = zs_eg1(e1, t, T) * (1.0 - lfo['trem'] * (0.5 + 0.5 * lv)) * gain
        units = zs_rnd(layer['pegReg'] * 32.0 * m + lfo['pitchOct'] * 4096.0 * lv)
        st = zs_rnd(base * dexp2(units / 4096.0) * 65536.0)
        if st < 1:
            st = 1
        elif st > 1048576:
            st = 1048576
        steps[k] = st
        if filt:
            fc = flt['cutoff'] * dexp2(flt['egOct'] * m + flt['lfoOct'] * lv)
            if fc < 30.0:
                fc = 30.0
            elif fc > 8000.0:
                fc = 8000.0
            cuts[k] = fc
            th = ZS_TWO_PI * fc / ZS_SR
            r = dexp2(-(th / (2.0 * q)) / ZS_LN2)
            b1 = -2.0 * r * dsin(th + ZS_HALF_PI)
            b2 = r * r
            cb1[k] = b1
            cb2[k] = b2
            cK[k] = g * (1.0 + b1 + b2)
    amp[F] = 0.0

    out = [0.0] * n
    pos = 0
    y1 = 0.0
    y2 = 0.0
    i = 0
    done = False
    for k in range(F):
        st = steps[k]
        a = amp[k]
        da = (amp[k + 1] - a) / 32.0
        b1 = cb1[k]
        b2 = cb2[k]
        K = cK[k]
        for j in range(32):
            idx = pos >> 16
            if idx > L:
                if looped:
                    pos -= loop_len << 16
                    idx -= loop_len
                else:
                    done = True
                    break
            fr = (pos & 0xFFFF) / 65536.0
            s0 = ext[idx]
            s1 = ext[idx + 1]
            s2 = ext[idx + 2]
            s3 = ext[idx + 3]
            c1 = 0.5 * (s2 - s0)
            c2 = s0 - 2.5 * s1 + 2.0 * s2 - 0.5 * s3
            c3 = 0.5 * (s3 - s0) + 1.5 * (s1 - s2)
            x = ((c3 * fr + c2) * fr + c1) * fr + s1
            if filt:
                y = K * x - b1 * y1 - b2 * y2
                y2 = y1
                y1 = y
                x = y
            out[i] = x * (a + da * j)
            i += 1
            pos += st
        if done:
            break
    return out, amp, cuts


# ---------------------------------------------------------------------------
# 8. 3D PLACEMENT: interaural delay, far-ear level drop and head shadow
# ---------------------------------------------------------------------------
ZS_ITD = 48000.0 * 0.0875 / 343.0      # head radius / speed of sound, in samples


def zs_spatialise(mono, layer):
    """mono list -> (left, right) lists, 34 samples longer"""
    n = len(mono)
    F = (n + 34 + ZS_FRAME - 1) // ZS_FRAME
    total = F * ZS_FRAME
    dl = [0.0] * (F + 1)
    dr = [0.0] * (F + 1)
    sh = [0.0] * (F + 1)
    az0 = layer['az0']
    az1 = layer['az1']
    mt = layer['moveTime']
    for k in range(F + 1):
        u = (k / 1500.0) / mt
        if u > 1.0:
            u = 1.0
        rad = (az0 + (az1 - az0) * u) * ZS_PI / 180.0
        s = dsin(rad)
        sh[k] = s
        dl[k] = ZS_ITD * ((rad if rad > 0.0 else 0.0) + (s if s > 0.0 else 0.0))
        dr[k] = ZS_ITD * ((-rad if rad < 0.0 else 0.0) + (-s if s < 0.0 else 0.0))
    left = [0.0] * total
    right = [0.0] * total
    zl = 0.0
    zr = 0.0
    i = 0
    for k in range(F):
        d0 = dl[k]
        dd0 = (dl[k + 1] - d0) / 32.0
        d1 = dr[k]
        dd1 = (dr[k + 1] - d1) / 32.0
        s0 = sh[k]
        ds = (sh[k + 1] - s0) / 32.0
        for j in range(32):
            s = s0 + ds * j
            sp = s if s > 0.0 else 0.0
            sn = -s if s < 0.0 else 0.0
            # left ear is the far ear when the source is to the right (s > 0)
            p = i - (d0 + dd0 * j)
            ip = _floor(p)
            f = p - ip
            a = mono[ip] if 0 <= ip < n else 0.0
            b = mono[ip + 1] if -1 <= ip < n - 1 else 0.0
            x = a + (b - a) * f
            zl = zl + (1.0 - 0.75 * sp) * (x - zl)
            left[i] = zl * (1.0 - 0.5 * sp)
            p = i - (d1 + dd1 * j)
            ip = _floor(p)
            f = p - ip
            a = mono[ip] if 0 <= ip < n else 0.0
            b = mono[ip + 1] if -1 <= ip < n - 1 else 0.0
            x = a + (b - a) * f
            zr = zr + (1.0 - 0.75 * sn) * (x - zr)
            right[i] = zr * (1.0 - 0.5 * sn)
            i += 1
    return left, right


# ---------------------------------------------------------------------------
# 9. I3DL2-PARAMETER REVERB (input low-pass -> 6 early taps -> 2 all-pass -> 4-line FDN)
# ---------------------------------------------------------------------------
ZS_ER_TIMES = [0.0, 0.0031, 0.0057, 0.0089, 0.0127, 0.0173]
ZS_ER_GAINS = [1.0, 0.80, 0.70, 0.55, 0.45, 0.35]
ZS_FDN_LENS = [1847, 2311, 2797, 3251]


def zs_reverb(inp, room):
    p = ZS_ROOMS[room]
    n = len(inp)
    refl = dexp2(p['reflDb'] * ZS_DB2OCT)
    rev = dexp2(p['revDb'] * ZS_DB2OCT)
    in_lp = p['inLp']
    dens = 0.6 + 0.4 * p['density']
    taps = [_floor((p['reflDelay'] + ZS_ER_TIMES[j] * dens) * ZS_SR) + 1 for j in range(6)]
    late_tap = _floor((p['reflDelay'] + p['revDelay']) * ZS_SR) + 1
    ring_n = late_tap + 1
    for j in range(6):
        if taps[j] + 1 > ring_n:
            ring_n = taps[j] + 1
    ring = [0.0] * ring_n
    wr = 0
    na1 = _floor(241 * dens)
    na2 = _floor(83 * dens)
    a1 = [0.0] * na1
    a2 = [0.0] * na2
    w1 = 0
    w2 = 0
    ga = 0.7 * p['diffusion']
    lens = [_floor(ZS_FDN_LENS[j] * dens) for j in range(4)]
    gl = [0.0] * 4
    pl = [0.0] * 4
    for j in range(4):
        gj = dexp2(-(3.0 * lens[j] / (p['decay'] * ZS_SR)) * ZS_LOG2_10)
        hj = dexp2(-(3.0 * lens[j] / (p['decay'] * p['hfRatio'] * ZS_SR)) * ZS_LOG2_10)
        rr = hj / gj
        gl[j] = gj
        pl[j] = (1.0 - rr) / (1.0 + rr)
    l0 = [0.0] * lens[0]
    l1 = [0.0] * lens[1]
    l2 = [0.0] * lens[2]
    l3 = [0.0] * lens[3]
    n0, n1, n2, n3 = lens
    g0, g1, g2, g3 = gl
    p0, p1, p2, p3 = pl
    q0 = 1.0 - p0
    q1 = 1.0 - p1
    q2 = 1.0 - p2
    q3 = 1.0 - p3
    i0 = i1 = i2 = i3 = 0
    d0 = d1 = d2 = d3 = 0.0
    t0, t1, t2, t3, t4, t5 = taps
    e0, e1, e2, e3, e4, e5 = ZS_ER_GAINS
    lp = 0.0
    out_l = [0.0] * n
    out_r = [0.0] * n
    for i in range(n):
        lp = lp + in_lp * (inp[i] - lp)
        ring[wr] = lp
        er_l = e0 * ring[(wr - t0) % ring_n] + e2 * ring[(wr - t2) % ring_n] + e4 * ring[(wr - t4) % ring_n]
        er_r = e1 * ring[(wr - t1) % ring_n] + e3 * ring[(wr - t3) % ring_n] + e5 * ring[(wr - t5) % ring_n]
        v = ring[(wr - late_tap) % ring_n]
        wr += 1
        if wr == ring_n:
            wr = 0
        z = a1[w1]
        u = v - ga * z
        a1[w1] = u
        v = u * ga + z
        w1 += 1
        if w1 == na1:
            w1 = 0
        z = a2[w2]
        u = v - ga * z
        a2[w2] = u
        v = u * ga + z
        w2 += 1
        if w2 == na2:
            w2 = 0
        d0 = l0[i0] * q0 + d0 * p0
        d1 = l1[i1] * q1 + d1 * p1
        d2 = l2[i2] * q2 + d2 * p2
        d3 = l3[i3] * q3 + d3 * p3
        s = 0.5 * (d0 + d1 + d2 + d3)
        l0[i0] = v + (d0 - s) * g0
        l1[i1] = v + (d1 - s) * g1
        l2[i2] = v + (d2 - s) * g2
        l3[i3] = v + (d3 - s) * g3
        i0 += 1
        if i0 == n0:
            i0 = 0
        i1 += 1
        if i1 == n1:
            i1 = 0
        i2 += 1
        if i2 == n2:
            i2 = 0
        i3 += 1
        if i3 == n3:
            i3 = 0
        out_l[i] = refl * er_l + rev * (d0 - d2)
        out_r[i] = refl * er_r + rev * (d1 - d3)
    return out_l, out_r


# ---------------------------------------------------------------------------
# 10. RENDER
# ---------------------------------------------------------------------------
def zs_render(patch):
    """patch -> dict(left, right: lists of int16 values, sampleRate, gain, peak, sampleBytes, layerEnvs, layerCuts)"""
    voices = []
    layer_envs = []
    layer_cuts = []
    sample_bytes = 0
    n = 0
    for layer in patch['layers']:
        pcm, loop_start = zs_gen_source(layer['source'])
        if layer['format'] == 'adpcm':
            pcm, used = zs_adpcm_codec(pcm)
            sample_bytes += used
        else:
            sample_bytes += len(pcm) * 2
        parts = []
        m = 0
        for note in layer['notes']:
            off = _floor(note['t'] * ZS_SR)
            out, amp, cuts = zs_render_voice(pcm, loop_start, layer, note)
            parts.append((off, out, amp, cuts))
            if off + len(out) > m:
                m = off + len(out)
        mono = [0.0] * m
        for off, out, amp, cuts in parts:
            for i in range(len(out)):
                mono[off + i] += out[i]
        left, right = zs_spatialise(mono, layer)
        voices.append((mono, left, right, layer))
        layer_envs.append(parts[0][2])
        layer_cuts.append(parts[0][3])
        if len(left) > n:
            n = len(left)

    room = ZS_ROOMS[patch['reverb']['room']]
    tail = room['decay'] * 1.15 + room['reflDelay'] + room['revDelay']
    if tail > 3.0:
        tail = 3.0
    n += _floor(tail * ZS_SR) + 64
    mix_l = [0.0] * n
    mix_r = [0.0] * n
    snd = [0.0] * n
    for mono, left, right, layer in voices:
        for i in range(len(left)):
            mix_l[i] += left[i]
            mix_r[i] += right[i]
        send = layer['send']
        if send > 0.0:
            for i in range(len(mono)):
                snd[i] += mono[i] * send
    wet_l, wet_r = zs_reverb(snd, patch['reverb']['room'])
    wet = patch['reverb']['wet']
    peak = 0.0
    for i in range(n):
        l = mix_l[i] + wet_l[i] * wet
        r = mix_r[i] + wet_r[i] * wet
        mix_l[i] = l
        mix_r[i] = r
        if l < 0.0:
            l = -l
        if r < 0.0:
            r = -r
        if l > peak:
            peak = l
        if r > peak:
            peak = r
    # Peak-normalise to the category level, as an asset pipeline would.
    gain = 32767.0 * 0.89 * patch['level'] / peak if peak > 0.0 else 0.0
    last = -1
    for i in range(n):
        l = zs_rnd(mix_l[i] * gain)
        r = zs_rnd(mix_r[i] * gain)
        mix_l[i] = l
        mix_r[i] = r
        if l > 12 or l < -12 or r > 12 or r < -12:
            last = i
    end = 1 if last < 0 else (n if last + 481 > n else last + 481)
    return {'left': mix_l[:end], 'right': mix_r[:end], 'sampleRate': ZS_SR, 'gain': gain,
            'peak': peak, 'sampleBytes': sample_bytes, 'layerEnvs': layer_envs, 'layerCuts': layer_cuts}


def zs_pcm_bytes(render):
    left = render['left']
    right = render['right']
    n = len(left)
    inter = [0] * (2 * n)
    inter[0::2] = left
    inter[1::2] = right
    return struct.pack('<%dh' % (2 * n), *inter)


def zs_checksum(render):
    """CRC-32 of the interleaved little-endian int16 stream."""
    return zlib.crc32(zs_pcm_bytes(render)) & M32


def zs_hex8(v):
    return '%08X' % (v & M32)


def zs_encode_wav(render):
    data = zs_pcm_bytes(render)
    sr = render['sampleRate']
    return (b'RIFF' + struct.pack('<I', 36 + len(data)) + b'WAVEfmt ' +
            struct.pack('<IHHIIHH', 16, 1, 2, sr, sr * 4, 4, 16) + b'data' + struct.pack('<I', len(data)) + data)


def zs_write_wav(path, render):
    with open(path, 'wb') as fh:
        fh.write(zs_encode_wav(render))


def zb_verify_one(name, world_seed=ZS_DEFAULT_SEED):
    """Render twice and compare; also check the sound is audible and unclipped."""
    ra = zs_render(zb_derive_params(name, world_seed))
    a = zs_checksum(ra)
    b = zs_checksum(zs_render(zb_derive_params(name, world_seed)))
    pk = 0
    for ch in (ra['left'], ra['right']):
        for v in ch:
            if v > pk:
                pk = v
            elif -v > pk:
                pk = -v
    return {'crc': zs_hex8(a), 'deterministic': a == b, 'peak': pk, 'seconds': len(ra['left']) / ZS_SR,
            'ok': a == b and 4000 <= pk <= 32767}


def _crc_job(args):
    kind, key, seed = args
    patch = zb_derive_params(key, seed) if kind == 'name' else zb_randomize(key, seed)
    return zs_hex8(zs_checksum(zs_render(patch)))


def _verify_job(args):
    return zb_verify_one(args[0], args[1])


def _pool_map(fn, jobs, workers):
    if workers > 1 and len(jobs) > 1:
        try:
            import multiprocessing
            with multiprocessing.Pool(workers) as pool:
                return pool.map(fn, jobs)
        except (ImportError, OSError):
            pass
    return [fn(j) for j in jobs]


def zb_verify_all(world_seed=ZS_DEFAULT_SEED, workers=1):
    names = list(ZB_SOUNDS.keys())
    res = _pool_map(_verify_job, [(nm, world_seed) for nm in names], workers)
    return dict(zip(names, res))


def zb_checksums(world_seed=ZS_DEFAULT_SEED, workers=1, randoms=()):
    names = list(ZB_SOUNDS.keys())
    jobs = [('name', nm, world_seed) for nm in names] + [('random', k, world_seed) for k in randoms]
    res = _pool_map(_crc_job, jobs, workers)
    out = dict(zip(names, res[:len(names)]))
    for j, k in enumerate(randoms):
        out['patch#%d' % k] = res[len(names) + j]
    return out


# ---------------------------------------------------------------------------
# COMMAND LINE
# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description='Zero Sounds 3: deterministic Xbox Classic style sound effects.')
    ap.add_argument('--list', action='store_true', help='list sound names by category')
    ap.add_argument('--sound', action='append', default=[], help='render a named sound (repeatable)')
    ap.add_argument('--all', action='store_true', help='render all named sounds')
    ap.add_argument('--random', action='append', type=int, default=[], metavar='N', help='render unnamed patch N (repeatable)')
    ap.add_argument('--seed', default=hex(ZS_DEFAULT_SEED), help='world seed, decimal or 0x hex (default 0x1337BEEF)')
    ap.add_argument('--out', default='.', help='output directory for WAV files')
    ap.add_argument('--params', metavar='NAME', help='print the derived patch as JSON')
    ap.add_argument('--verify', action='store_true', help='render every sound twice; check determinism and level')
    ap.add_argument('--checksums', action='store_true', help='print CRC-32 per sound as JSON')
    ap.add_argument('--jobs', type=int, default=os.cpu_count() or 1, help='worker processes for --verify and --checksums')
    a = ap.parse_args(argv)
    seed = int(a.seed, 0) & M32

    if a.list:
        for c in ZB_CATEGORY_ORDER:
            print('%-12s %s' % (c, ', '.join(k for k, v in ZB_SOUNDS.items() if v['category'] == c)))
        return 0
    if a.params:
        print(json.dumps(zb_derive_params(a.params, seed), indent=2))
        return 0
    if a.checksums:
        print(json.dumps(zb_checksums(seed, a.jobs, a.random)))
        return 0
    if a.verify:
        res = zb_verify_all(seed, a.jobs)
        bad = 0
        for nm, r in res.items():
            flag = 'ok  ' if r['ok'] else 'FAIL'
            if not r['ok']:
                bad += 1
            print('%s %-11s crc %s  peak %5d  %.2f s' % (flag, nm, r['crc'], r['peak'], r['seconds']))
        print('seed 0x%s: %d of %d sounds passed' % (zs_hex8(seed), len(res) - bad, len(res)))
        return 1 if bad else 0

    jobs = [(nm, zb_derive_params(nm, seed)) for nm in (list(ZB_SOUNDS.keys()) if a.all else a.sound)]
    jobs += [('patch%d' % k, zb_randomize(k, seed)) for k in a.random]
    if not jobs:
        ap.print_help()
        return 0
    os.makedirs(a.out, exist_ok=True)
    for nm, patch in jobs:
        r = zs_render(patch)
        path = os.path.join(a.out, '%s_%s.wav' % (nm, zs_hex8(seed)))
        zs_write_wav(path, r)
        print('%s  %.2f s  crc %s  %.1f KB sample memory' % (path, len(r['left']) / ZS_SR, zs_hex8(zs_checksum(r)), r['sampleBytes'] / 1024.0))
    return 0


if __name__ == '__main__':
    sys.exit(main())
