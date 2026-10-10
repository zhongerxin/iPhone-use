#!/usr/bin/env python3
"""Check bundled module syntax and run isolated widget DOM tests."""
from pathlib import Path
import re
import subprocess
import tempfile

root=Path(__file__).resolve().parents[1]
for name in ('phone-screen.html','phone-screen.ja.html'):
    html=(root/'assets'/name).read_text()
    modules=re.findall(r'<script type="module">([\s\S]*?)</script>',html)
    assert len(modules)==1,'Expected one bundled widget module'
    assert 'console.debug(' not in modules[0], 'Preview SDK must not debug-log frame payloads'
    assert not re.search(r'<(?:script|link)[^>]+(?:src|href)=["\']https?://',html),'Widget must be self-contained'
    assert len(re.findall(r'<img\b',html))==1,'The phone picture is the only image'
    buttons=re.findall(r'<button\b[^>]*>',html)
    assert len(buttons)==3 and all('type="button"' in b and 'aria-label="' in b for b in buttons),'Toolbar is three labelled buttons'
    assert '<input' not in html and '<form' not in html and '<a ' not in html,'Widget has no other controls'
    assert 'filter:blur' not in html.replace(' ',''),'Edge light must not animate under a live blur filter'
    assert '--glow-mask' in html,'Edge light is shaped by one mask picture'
    with tempfile.TemporaryDirectory() as directory:
        script=Path(directory)/'screen.mjs'
        script.write_text(modules[0])
        subprocess.run(['node','--check',str(script)],check=True)
subprocess.run(['node','--test',str(root/'ui/tests/widget.test.mjs')],check=True)
