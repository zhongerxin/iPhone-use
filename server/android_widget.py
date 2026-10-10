"""Android presentation for the shared, platform-neutral frame widget."""
def android_html(html):
    html=html.replace('iPhone','Android')
    if 'data-platform="android"' not in html:
        html=html.replace('<main id="app"','<main id="app" data-platform="android"')
    if 'android-preview-style' in html:return html
    # Device screenshots already contain any hardware cutout/rounded-corner mask.
    # Never superimpose iPhone decorations or clip Android screen pixels.
    style='''<style id="android-preview-style">
.dynamic-island,.side-button,.receiver{display:none!important}
#device{border-radius:10px;aspect-ratio:auto}
#device::before{border-radius:8px}
#screen{border-radius:0}
</style>'''
    return html.replace('</head>',style+'</head>')
