"""Public-web egress proxy. Resolve once, validate every address, connect by IP."""
import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit


def public_address(address):
    ip = ipaddress.ip_address(address.split('%')[0])
    if ip.version == 6 and ip in ipaddress.ip_network('64:ff9b::/96'):
        return False
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_unspecified) and not (
        getattr(ip, 'ipv4_mapped', None) or getattr(ip, 'sixtofour', None) or getattr(ip, 'teredo', None))


async def public_connection(host, port):
    if port not in (80, 443):
        raise ValueError('Only web ports are allowed')
    addresses = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not public_address(item[4][0]) for item in addresses):
        raise ValueError('Only public destinations are allowed')
    # Use a validated numeric address. Never resolve the hostname again at connect.
    for family, _, _, _, address in addresses:
        try:
            return await asyncio.wait_for(
                asyncio.open_connection(address[0], port, family=family), 10)
        except OSError:
            continue
    raise OSError('Destination unavailable')


async def copy_stream(reader, writer):
    while data := await asyncio.wait_for(reader.read(65536), 60):
        writer.write(data)
        await writer.drain()


async def proxy(reader, writer):
    upstream = None
    tasks = []
    try:
        async with asyncio.timeout(180):
            header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 10)
            if len(header) > 16384:
                raise ValueError('Header too large')
            lines = header.decode('iso-8859-1').split('\r\n')
            method, target, version = lines[0].split(' ')
            if version not in ('HTTP/1.0', 'HTTP/1.1'):
                raise ValueError('Invalid protocol')
            if method == 'CONNECT':
                url = urlsplit('https://' + target)
                if url.port != 443 or url.username or url.password or url.path:
                    raise ValueError('Invalid tunnel')
                remote, upstream = await public_connection(url.hostname, 443)
                writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            else:
                url = urlsplit(target)
                if url.scheme != 'http' or url.username or url.password or (url.port or 80) != 80:
                    raise ValueError('Invalid URL')
                remote, upstream = await public_connection(url.hostname, 80)
                path = (url.path or '/') + ('?' + url.query if url.query else '')
                headers = [line for line in lines[1:] if line and line.split(':', 1)[0].lower()
                           not in ('host', 'connection', 'proxy-connection', 'proxy-authorization')]
                request = '\r\n'.join([f'{method} {path} {version}', f'Host: {url.netloc}',
                                        'Connection: close', *headers, '', ''])
                upstream.write(request.encode('iso-8859-1'))
                await upstream.drain()
            await writer.drain()
            tasks = [asyncio.create_task(copy_stream(reader, upstream)),
                     asyncio.create_task(copy_stream(remote, writer))]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except (ValueError, OSError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        if upstream is None:
            writer.write(b'HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if upstream:
            upstream.close()
        writer.close()
        await writer.wait_closed()


async def main():
    slots = asyncio.Semaphore(128)

    async def limited(reader, writer):
        if slots.locked():
            writer.close()
            return
        async with slots:
            await proxy(reader, writer)

    async with await asyncio.start_server(limited, '0.0.0.0', 3128, limit=16384) as server:
        await server.serve_forever()


if __name__ == '__main__':
    asyncio.run(main())
