#!/usr/bin/env python3
"""暂存 Claude Code 本地 marketplace，复用 Codex 安装包的文件清单，不改动 dist/iphone-use。"""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1]
TEMPLATE=ROOT/'claude'
DEFAULT_STAGE=ROOT/'dist'/'claude'
MARKETPLACE='iphone-use-local'
PLUGIN='iphone-use'
# Codex 清单、Codex 安装脚本与带 ${PLUGIN_ROOT} 的 MCP 配置不进入 Claude 包：
# Claude Code 会把插件根目录的 .mcp.json 与 plugin.json 的 mcpServers 合并加载。
CODEX_ONLY={'plugin.json','mcp.json','.mcp.json','.codex-plugin/plugin.json','.agents/plugins/marketplace.json',
            'scripts/install.sh','scripts/register_mcp.py','scripts/package.py'}
VIEWER_FILES=('screen_viewer.py','viewer.html')
HOOK_FILES=('hooks.json','register.ts')


def load_packaging():
    spec=importlib.util.spec_from_file_location('iphone_use_packaging',ROOT/'scripts'/'package.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def plugin_files(packaging):
    """Codex 安装包文件减去 Codex 专属文件，再加上 Claude 补充技能、屏幕查看服务与说明。"""
    for source,rel in packaging.package_files():
        if rel.as_posix() not in CODEX_ONLY:yield source,rel
    for source in sorted((TEMPLATE/'skills').rglob('*')):
        if source.is_file() and source.name!='.DS_Store':yield source,Path('skills')/source.relative_to(TEMPLATE/'skills')
    # 查看服务只需运行时文件；host.ts、构建脚本与依赖留在仓库中。
    for name in VIEWER_FILES:yield TEMPLATE/'viewer'/name,Path('viewer')/name
    # 钩子模块：READY 后自动在右侧浏览器面板打开实时手机屏幕。
    for name in HOOK_FILES:yield TEMPLATE/'hooks'/name,Path('hooks')/name


def write_json(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')


def build(stage=DEFAULT_STAGE):
    packaging=load_packaging()
    manifest=packaging.validate()
    template=json.loads((TEMPLATE/'plugin.json').read_text())
    assert template['name']==manifest['name']==PLUGIN and 'version' not in template
    stage=Path(stage).resolve()
    if stage.exists() and any(stage.iterdir()) and not (stage/'.claude-plugin'/'marketplace.json').is_file():
        raise SystemExit('Refusing to replace a directory that is not a staged Claude marketplace: '+str(stage))
    # 本地 marketplace 原地加载插件；先在同级临时目录完整生成再替换，减少运行中会话读到半成品的窗口。
    building=stage.with_name(stage.name+'.building')
    if building.exists():shutil.rmtree(building)
    plugin_root=building/PLUGIN
    for source,rel in plugin_files(packaging):
        if source.is_symlink():raise SystemExit('Refusing package symlink: '+str(source))
        target=plugin_root/rel;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
    write_json(plugin_root/'.claude-plugin'/'plugin.json',{'name':PLUGIN,'version':manifest['version'],**{key:value for key,value in template.items() if key!='name'}})
    write_json(building/'.claude-plugin'/'marketplace.json',{
        'name':MARKETPLACE,'owner':template['author'],
        'plugins':[{'name':PLUGIN,'source':'./'+PLUGIN,'description':template['description'],'category':'productivity'}]})
    retired=stage.with_name(stage.name+'.retired')
    if retired.exists():shutil.rmtree(retired)
    if stage.exists():stage.rename(retired)
    building.rename(stage)
    if retired.exists():shutil.rmtree(retired)
    return stage


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--stage',default=str(DEFAULT_STAGE),help='Claude marketplace 暂存目录');args=parser.parse_args()
    print(build(args.stage))


if __name__=='__main__':sys.exit(main())
