"""Live browser checks. Run ./run.sh check-browser after starting the browser."""
import asyncio
import json
import os
import socket
import urllib.error
import urllib.request

BASE = 'http://127.0.0.1:8080'


def call(path, data=None, auth=True):
    headers = {'Content-Type': 'application/json'}
    if auth:
        headers['Authorization'] = 'Bearer ' + os.environ['BROWSER_API_KEY']
    request = urllib.request.Request(BASE + path, headers=headers,
                                     data=json.dumps(data).encode() if data is not None else None)
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


try:
    call('/openapi.json', auth=False)
    raise AssertionError('Anonymous tool access allowed')
except urllib.error.HTTPError as error:
    assert error.code == 401

page = call('/navigate', {'url': 'https://example.com'})
try:
    assert page['untrusted_page']['title'] == 'Example Domain'
    link = next(c for c in page['untrusted_page']['controls'] if 'iana.org' in (c.get('href') or ''))
    page = call('/action', {'session_id': page['session_id'], 'action': 'click', 'ref': link['ref']})
    assert 'iana.org' in page['url'] and page['untrusted_page']['title'] == 'Example Domains'
finally:
    call('/action', {'session_id': page['session_id'], 'action': 'close'})

for url in ['http://127.0.0.1', 'https://192.168.1.1', 'http://host.docker.internal',
            'http://169.254.169.254', 'http://[::1]']:
    page = call('/navigate', {'url': url})
    try:
        assert 'error' in page or page.get('status') == 403, (url, page)
    finally:
        call('/action', {'session_id': page['session_id'], 'action': 'close'})

for url in ['file:///etc/passwd', 'http://example.com:22', 'http://example.com:invalid']:
    try:
        call('/navigate', {'url': url})
        raise AssertionError('Invalid URL accepted')
    except urllib.error.HTTPError as error:
        assert error.code == 400

try:
    with socket.create_connection(('1.1.1.1', 443), timeout=3):
        raise AssertionError('Direct internet egress allowed')
except (OSError, TimeoutError):
    pass


async def fixture():
    # Exercise the real DOM helpers without submitting data to an external site.
    import browser_server as browser
    async with browser.lifespan(browser.app):
        identifier, session = await browser.session_for()
        page = session['page']
        html = '<title>Fixture</title><label>Name<input aria-label="Name"></label>'
        html += '<button onclick="document.querySelector(\'output\').textContent=document.querySelector(\'input\').value">Show</button><output></output>'
        html += '<p>' + 'Neutral page text. ' * 1200 + 'TAIL_MARKER</p>'
        await page.route('https://example.com/fixture', lambda route: route.fulfill(body=html, content_type='text/html'))
        await browser.navigate(browser.Navigate(url='https://example.com/fixture', session_id=identifier))
        result = await browser.snapshot(identifier, session)
        field = next(c['ref'] for c in result['untrusted_page']['controls'] if c['tag'] == 'input')
        result = await browser.perform_action(browser.Action(session_id=identifier, action='fill', ref=field, text='fixture passed'))
        button = next(c['ref'] for c in result['untrusted_page']['controls'] if c['tag'] == 'button')
        result = await browser.perform_action(browser.Action(session_id=identifier, action='click', ref=button))
        assert 'fixture passed' in result['untrusted_page']['text']
        result = await browser.perform_action(browser.Action(session_id=identifier, action='read', offset=16000))
        assert 'TAIL_MARKER' in result['untrusted_page']['text']
        await browser.perform_action(browser.Action(session_id=identifier, action='close'))


asyncio.run(fixture())
print('Browser navigation, DOM actions, long-page reads, authentication, and network boundaries passed.')
