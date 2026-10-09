#!/usr/bin/env python3
"""Build Android independently, with an explicit shared-runtime allowlist."""
import argparse
import json
from pathlib import Path
import re
import shutil
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ANDROID_FILES = (
    'plugin.json', '.codex-plugin/plugin.json', 'mcp.json', '.mcp.json',
    'README.md', 'THIRD_PARTY_NOTICES.md', 'requirements.txt',
    'skills/android-use/SKILL.md', 'skills/android-use-setup/SKILL.md',
)
# ScreenHub supplies the existing lease, pause, frame and gesture-event protocol.
# Its iPhone capture implementation is overridden by AndroidScreen.
SERVER_FILES = (
    'phone_protocol.py', 'wda_screen.py', 'android_use.py', 'android_client.py',
    'android_controller.py', 'android_screen.py', 'android_video.py', 'android_widget.py',
)


def validate():
    manifest = json.loads((ROOT / 'android/plugin.json').read_text())
    overlay = json.loads((ROOT / 'android/.codex-plugin/plugin.json').read_text())
    assert manifest['name'] == 'android-use' and re.fullmatch(r'\d+\.\d+\.\d+', manifest['version'])
    assert all(manifest[key] == overlay[key] for key in ('name', 'version', 'description'))
    assert manifest['extensions']['com.openai']['interface'] == overlay['interface']
    assert overlay['mcpServers'] == './.mcp.json' and overlay['skills'] == './skills/'
    config = json.loads((ROOT / 'android/.mcp.json').read_text())
    assert config['mcpServers'] == json.loads((ROOT / 'android/mcp.json').read_text())['mcpServers']
    assert list(config['mcpServers']) == ['android_use']
    assert config['mcpServers']['android_use'] == {
        'type': 'stdio', 'command': 'sh', 'args': ['${PLUGIN_ROOT}/scripts/android_mcp.sh'],
    }
    for skill in ('android-use', 'android-use-setup'):
        assert ('name: ' + skill + '\n') in (ROOT / 'android/skills' / skill / 'SKILL.md').read_text()
    sys.path.insert(0, str(ROOT / 'server'))
    from android_use import VERSION, TOOLS, SCREEN_URI
    assert VERSION == manifest['version'] and len(TOOLS) == 19
    assert sum(t.get('_meta', {}).get('ui', {}).get('visibility') == ['app'] for t in TOOLS) == 2
    assert next(t for t in TOOLS if t['name'] == 'pua_ready')['_meta']['ui']['resourceUri'] == SCREEN_URI
    return manifest


def stage_plugin(stage):
    sources = [(ROOT / 'android' / name, name) for name in ANDROID_FILES]
    sources += [(ROOT / 'server' / name, 'server/' + name) for name in SERVER_FILES]
    sources += [(ROOT / name, name) for name in ('assets/phone-screen.html', 'assets/icon.svg', 'scripts/android_mcp.sh', 'LICENSE')]
    sources.append((ROOT / 'THIRD_PARTY_NOTICES.md', 'SHARED_NOTICES.md'))
    for source, name in sources:
        if source.is_symlink():
            raise SystemExit('Refusing package symlink: ' + str(source))
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    sys.path.insert(0, str(ROOT / 'server'))
    from android_widget import android_html
    widget = stage / 'assets/phone-screen.html'
    widget.write_text(android_html(widget.read_text()))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--validate-only', action='store_true')
    args = parser.parse_args()
    manifest = validate()
    if args.validate_only:
        print('Android manifests, skills and runtime contracts validated.')
        return
    market = ROOT / 'dist/android-marketplace'
    catalog = market / '.agents/plugins/marketplace.json'
    catalog.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / 'android/.agents/plugins/marketplace.json', catalog)
    stage = market / 'android-use'
    if stage.exists():
        shutil.rmtree(stage)
    stage_plugin(stage)
    with zipfile.ZipFile(ROOT / 'dist' / ('android-use-' + manifest['version'] + '.zip'), 'w', zipfile.ZIP_DEFLATED) as archive:
        for source in sorted(stage.rglob('*')):
            if source.is_file():
                archive.write(source, Path('android-use') / source.relative_to(stage))
    print(stage)


if __name__ == '__main__':
    main()
