"""Forward 127.0.0.1:<listen> to 127.0.0.1:<target>, so a second port reaches the same service.

Used when the browser reaches Open WebUI through a tunnel on another local port
(an IDE's port forwarding picked 18088): Open WebUI's WEBUI_URL is set to that
port, and this alias makes the same URL work on this host for the scripted checks.

    python t1/port_alias.py 18088 18087
"""
import asyncio
import sys

LISTEN, TARGET = int(sys.argv[1]), int(sys.argv[2])


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


async def handle(client_r, client_w):
    try:
        up_r, up_w = await asyncio.open_connection("127.0.0.1", TARGET)
    except OSError:
        client_w.close()
        return
    await asyncio.gather(pipe(client_r, up_w), pipe(up_r, client_w), return_exceptions=True)


async def main():
    server = await asyncio.start_server(handle, "127.0.0.1", LISTEN)
    async with server:
        await server.serve_forever()


asyncio.run(main())
