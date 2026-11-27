#!/usr/bin/env python3
"""Refresh the generated parts of the repository.

1. Renders every named sound with ZeroSounds3.py at the default seed and writes
   the CRC-32 table into ZeroSounds3.jsx (between /*ORACLE*/ and /*END*/).
2. Inlines ZeroSounds3.jsx into ZeroSounds3-demo.html (between // <COMPONENT>
   and // </COMPONENT>), with the import and export lines removed.

Run it after changing either engine or adding a sound:  python build-demo.py
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ZeroSounds3 as zs  # noqa: E402

JSX = os.path.join(HERE, 'ZeroSounds3.jsx')
DEMO = os.path.join(HERE, 'ZeroSounds3-demo.html')


def main():
    sums = zs.zb_checksums(zs.ZS_DEFAULT_SEED, os.cpu_count() or 1)
    with open(JSX, encoding='utf-8') as fh:
        jsx = fh.read()
    jsx, n = re.subn(r'/\*ORACLE\*/.*?/\*END\*/', lambda m: '/*ORACLE*/' + json.dumps(sums) + '/*END*/', jsx, flags=re.S)
    if n != 1:
        sys.exit('ORACLE marker not found in ZeroSounds3.jsx')
    with open(JSX, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(jsx)

    body = re.sub(r"^import React.*?\n", "const { useState, useRef, useCallback, useEffect, useMemo } = React;\n", jsx, count=1, flags=re.M)
    body = body[:body.index('\nexport {')].rstrip() + '\n'
    with open(DEMO, encoding='utf-8') as fh:
        html = fh.read()
    start = html.index('// <COMPONENT>') + len('// <COMPONENT>')
    end = html.index('// </COMPONENT>')
    html = html[:start] + '\n' + body + '\n' + html[end:]
    with open(DEMO, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(html)
    print('oracle: %d checksums at seed 0x%s' % (len(sums), zs.zs_hex8(zs.ZS_DEFAULT_SEED)))
    print('demo:   %d bytes' % len(html.encode('utf-8')))


if __name__ == '__main__':
    main()
