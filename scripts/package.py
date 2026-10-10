#!/usr/bin/env python3
"""Build separate source and install packages, keeping the complete widget."""
import argparse
import json
from pathlib import Path
import re
import shutil
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]
FILES=('plugin.json','mcp.json','.mcp.json','.codex-plugin/plugin.json','.agents/plugins/marketplace.json','README.md','README.en.md','ANALYTICS.md','CHANGELOG.md','LICENSE','THIRD_PARTY_NOTICES.md','ui/package.json','ui/package-lock.json','ui/build.mjs','ui/src/app.ts','ui/index.html','ui/style.css','ui/tsconfig.json','ui/tests/widget.test.mjs')
DIRS=('assets','server','skills')
INSTALL_SCRIPTS=('phone.py','wda.sh','update_app_catalog.py','check_screen_ui.py','package.py','install.sh','register_mcp.py')
SOURCE_DIRS=('scripts','tests','.github')
TOOLING_FILES=('package.json','package-lock.json','forward.mjs','screen-stream.mjs')


def package_files(source_package=False):
    """Local docs, evals, experiments and dependency installs are never shipped."""
    sources=[ROOT/name for name in FILES]
    for dirname in DIRS+(SOURCE_DIRS if source_package else ()):
        sources.extend((ROOT/dirname).rglob('*'))
    if source_package:
        sources.append(ROOT/'.gitignore')
    else:
        sources.extend(ROOT/'scripts'/name for name in INSTALL_SCRIPTS)
    sources.extend(ROOT/'tooling'/name for name in TOOLING_FILES)
    for source in sorted(set(sources)):
        rel=source.relative_to(ROOT)
        if any(part in ('__pycache__','node_modules','.pytest_cache') for part in rel.parts):continue
        if source.is_symlink():raise SystemExit('Refusing package symlink: '+str(source))
        if not source.is_file():continue
        if source.suffix in ('.pyc','.jsonl') or source.name in ('.DS_Store','posthog.local.json'):continue
        yield source,rel


def validate():
    manifest=json.loads((ROOT/'plugin.json').read_text());overlay=json.loads((ROOT/'.codex-plugin/plugin.json').read_text())
    ui=manifest['extensions']['com.openai']['interface']
    assert manifest['name']=='iphone-use' and re.fullmatch(r'\d+\.\d+\.\d+',manifest['version'])
    assert len(ui['shortDescription'])<=30
    assert ui==overlay['interface']
    assert all(manifest[k]==overlay[k] for k in ('name','version','description'))
    assert overlay['mcpServers']=='./.mcp.json' and overlay['skills']=='./skills/'
    portable=json.loads((ROOT/'mcp.json').read_text());legacy=json.loads((ROOT/'.mcp.json').read_text())
    assert portable['mcpServers']==legacy['mcpServers']
    assert list(portable['mcpServers'])==['iphone_use']
    for config in portable['mcpServers'].values():
        assert config['type']=='stdio' and config['args']==['${PLUGIN_ROOT}/server/iphone_use.py']
    for skill in ('iphone-use','iphone-use-setup'):
        path=ROOT/'skills'/skill/'SKILL.md';body=path.read_text()
        assert body.startswith('---\n') and re.search(r'^name: '+skill+r'$',body,re.M) and re.search(r'^description: .+',body,re.M)
    assert (ROOT/'tooling/package-lock.json').is_file()
    sys.path.insert(0,str(ROOT/'server'))
    from iphone_use import TOOLS,SCHEMAS,VERSION,SCREEN_URI
    assert VERSION==manifest['version'] and len(TOOLS)==20
    assert sum(t.get('_meta',{}).get('ui',{}).get('visibility')!=['app'] for t in TOOLS)==18
    assert (ROOT/'assets/phone-screen.html').is_file()
    assert next(t for t in TOOLS if t['name']=='pua_ready')['_meta']['ui']['resourceUri']==SCREEN_URI
    assert len(set(t['name'] for t in TOOLS))==len(TOOLS)
    for t in TOOLS:assert t['inputSchema']['additionalProperties'] is False
    return manifest


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--validate-only',action='store_true');parser.add_argument('--stage-only',action='store_true');args=parser.parse_args()
    manifest=validate()
    if args.validate_only:
        print('Plugin manifests, 2 skills, 18 model tools and 2 app-only preview tools validated.');return
    stage=ROOT/'dist'/manifest['name']
    if stage.exists():shutil.rmtree(stage)
    stage.mkdir(parents=True)
    for source,rel in package_files():
        target=stage/rel;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
    if args.stage_only:print(stage);return
    archive=ROOT/'dist'/f"{manifest['name']}-{manifest['version']}-source.zip"
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for source,rel in package_files(source_package=True):
            z.write(source,str(Path(manifest['name'])/rel))
    print(archive)


if __name__=='__main__':main()
