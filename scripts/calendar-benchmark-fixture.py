"""Simulator-only setup/audit/cleanup. Never exposed to either planning model."""
import json
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

BASE = 'http://127.0.0.1:18101'
TITLE = 'Midscene 对比验证'

def request(path, body=None):
    req = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body).encode(),
        headers={'Content-Type': 'application/json'})
    data = json.load(urllib.request.urlopen(req, timeout=20))
    if isinstance(data.get('value'), dict) and data['value'].get('error'):
        raise RuntimeError(data['value']['error'])
    return data

status = request('/status')
assert status['value']['ios']['simulatorVersion'], 'Simulator required'
prefix = '/session/' + status['sessionId']

def source():
    return ET.fromstring(request(prefix + '/source')['value'])

def visible(tree, kind=None):
    return [e for e in tree.iter() if e.get('visible') == 'true' and (kind is None or e.tag.endswith(kind))]

def named(tree, name, kind=None):
    return [e for e in visible(tree, kind) if name in (e.get('name'), e.get('label'))]

def tap(e):
    x, y, w, h = [float(e.get(k)) for k in ('x', 'y', 'width', 'height')]
    assert 0 <= x+w/2 < 402 and 0 <= y+h/2 < 874
    request(prefix + '/wda/tap', {'x': x+w/2, 'y': y+h/2})
    time.sleep(.65)

def one(elements):
    assert len(elements) == 1, f'Expected one element, found {len(elements)}'
    return elements[0]

def field(tree):
    return named(tree, 'title-field', 'TextField')

def is_day(tree):
    return bool(named(tree, 'current-day'))

def assert_day(tree):
    days = named(tree, 'current-day')
    assert days and all('2026年10月10日' in day.get('label', '') for day in days), 'Wrong benchmark date'

def prepare():
    tree = source()
    # A prior interactive setup may already have opened an empty form.
    if not field(tree):
        assert_day(tree)
        assert not named(tree, 'event-shown:' + TITLE), 'Clean up the prior trial first'
        tap(one(named(tree, 'add-plus-button', 'Button')))
        tree = source()
    title = one(field(tree))
    assert title.get('value', '') in ('', '标题'), 'Form is not empty'
    # Setup-only native picker selection pins initial values across wall-clock hours.
    for index, hour in ((0, '19点'), (1, '20点')):
        tree = source()
        times = sorted([e for e in visible(tree, 'Button') if re.fullmatch(r'\d{1,2}:\d{2}', e.get('label', ''))], key=lambda e:float(e.get('y')))
        if visible(tree, 'PickerWheel'):
            # Collapse the currently expanded row before selecting the desired row.
            wheels = visible(tree, 'PickerWheel')
            preceding = [e for e in times if float(e.get('y')) < float(wheels[0].get('y'))]
            tap(preceding[-1]); tree = source()
            times = sorted([e for e in visible(tree, 'Button') if re.fullmatch(r'\d{1,2}:\d{2}', e.get('label', ''))], key=lambda e:float(e.get('y')))
        tap(times[index])
        wheels = request(prefix + '/elements', {'using':'class name','value':'XCUIElementTypePickerWheel'})['value']
        assert len(wheels) == 2
        for wheel_index, (wheel, value) in enumerate(zip(wheels, (hour, '00分钟'))):
            ident = wheel.get('element-6066-11e4-a52e-4f735466cecf') or wheel['ELEMENT']
            target = int(re.search(r'\d+', value).group())
            for _ in range(24):
                current = int(re.search(r'\d+', visible(source(), 'PickerWheel')[wheel_index].get('value')).group())
                if current == target:
                    break
                request(prefix + '/wda/pickerwheel/' + ident + '/select',
                        {'order': 'next' if current < target else 'previous', 'offset': .15})
            else:
                raise RuntimeError('Could not establish the initial picker value')
        tree = source()
        times = sorted([e for e in visible(tree, 'Button') if re.fullmatch(r'\d{1,2}:\d{2}', e.get('label', ''))], key=lambda e:float(e.get('y')))
        tap(times[index])
    tap(one(field(source())))
    tree = source()
    assert named(tree, '19:00', 'Button') and named(tree, '20:00', 'Button')
    assert len(named(tree, '2026年10月10日', 'Button')) == 2
    assert named(tree, '位置或视频通话')
    return {'ready':True,'date':'2026-10-10','start':'19:00','end':'20:00','title_empty':True,'location_empty':True}

def audit(expected):
    tree = source()
    labels = [e.get('label', '') for e in visible(tree)]
    times = ' '.join(labels)
    passed = (not field(tree) and bool(named(tree, '编辑', 'Button')) and
        TITLE in labels and any(label.startswith(TITLE + ', 会议室 A,') for label in labels) and '2026年10月10日' in times and
        expected[0] in times and expected[1] in times)
    return {'passed':passed,'expected':expected,'visible_labels':list(dict.fromkeys(labels))}

def cleanup():
    for _ in range(10):
        tree = source()
        if field(tree):
            value = one(field(tree)).get('value', '')
            assert value in ('', '标题', TITLE), 'Refusing to discard another event'
            tap(one(named(tree, 'cancel-button', 'Button')))
            continue
        discard = named(tree, '放弃更改', 'Button')
        if discard:
            tap(one(discard)); continue
        deletion = named(tree, '删除日程', 'Button')
        if deletion:
            assert named(tree, TITLE) or named(tree, '确定要删除此日程吗？'), 'Unexpected deletion target'
            # The only deletable event in this fixture is the dedicated title.
            tap(deletion[-1]); continue
        if is_day(tree):
            assert_day(tree)
            matches = named(tree, 'event-shown:' + TITLE, 'Button')
            if not matches:
                return {'clean':True}
            tap(one(matches)); continue
        buttons = [e.get('label') for e in visible(tree, 'Button')]
        if not buttons or set(buttons) == {'今天', '日历', '收件箱'}:
            # Native day view is still rendering after discarding the form.
            time.sleep(.3)
            continue
        raise RuntimeError('Unexpected cleanup state: ' + repr(buttons))
    raise RuntimeError('Cleanup did not converge')

if __name__ == '__main__':
    action = sys.argv[1]
    result = prepare() if action == 'prepare' else cleanup() if action == 'cleanup' else audit(sys.argv[2:4])
    print(json.dumps(result, ensure_ascii=False))
