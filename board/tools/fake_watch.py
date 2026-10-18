"""Stand-in for the StopWatch: connects to /ws/watch and lets you type events.

    python tools/fake_watch.py ws://localhost:8000/ws/watch
    commands: i = impact, o = I'm OK, s = SOS, q = quit
"""
import asyncio
import json
import sys

import websockets


async def main(url: str) -> None:
    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        print(f"connected to {url}; type i/o/s/q + Enter")

        async def rx():
            async for msg in ws:
                if isinstance(msg, bytes):
                    print(f"  <- audio {len(msg)} bytes ({len(msg) / 32000:.1f} s at 16 kHz)")
                else:
                    print(f"  <- {msg}")

        async def hb():
            while True:
                await ws.send(json.dumps({"type": "hb", "battery": 77}))
                await asyncio.sleep(2)

        tasks = [asyncio.create_task(rx()), asyncio.create_task(hb())]
        loop = asyncio.get_running_loop()
        while True:
            line = (await loop.run_in_executor(None, sys.stdin.readline)).strip()
            if line == "q" or line == "":
                break
            msg = {"i": {"type": "impact", "peak_g": 3.4}, "o": {"type": "ok"}, "s": {"type": "sos"}}.get(line)
            if msg:
                await ws.send(json.dumps(msg))
                print(f"  -> {msg}")
        for t in tasks:
            t.cancel()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "ws://localhost:8000/ws/watch"))
