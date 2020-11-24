import asyncio
import functools
import http
import json
import os
import time
import subprocess

import websockets


def check(rm_hostname):
    try:
        model = subprocess.run(
            [
                "ssh",
                "-o",
                "ConnectTimeout=2",
                rm_hostname,
                "cat",
                "/proc/device-tree/model",
            ],
            check=True,
            capture_output=True,
        )
        return model.stdout[:14].decode("utf-8")
    except subprocess.CalledProcessError:
        print(f"Error: Can't connect to reMarkable tablet on hostname : {rm_hostname}")
        os._exit(1)


async def refresh_ss(refresh):
    now = time.time()
    try:
        if os.stat("resnap.jpg").st_mtime >= now - refresh:
            return
    except FileNotFoundError:
        pass
    try:
        print("running resnap")
        subprocess.run(['../scripts/host/resnap.sh', 'resnap.new.jpg'], check=True)
        os.rename('resnap.new.jpg', 'resnap.jpg')
        print("ok")
    except subprocess.CalledProcessError:
        print("Could not resnap, continuing anyway")


class WebsocketHandler():
    def __init__(self, rm_host, rm_model):
        if rm_model == "reMarkable 1.0":
            self.device = "/dev/input/event0"
        elif rm_model == "reMarkable 2.0":
            self.device = "/dev/input/event1"
        else:
            raise NotImplementedError(f"Unsupported reMarkable Device : {rm_model}")

        self.rm_host = rm_host
        self.websockets = []
        self.running = False

    async def websocket_broadcast(self, msg):
        for websocket in self.websockets:
            try:
                await websocket.send(msg)
            except:
                print("Client closed")
                self.websockets.remove(websocket)
                if not self.websockets:
                    raise EOFError


    async def websocket_handler(self, websocket, path):
        self.websockets.append(websocket)
        if not self.running:
            self.running = True
            await asyncio.create_task(self.ssh_handler())
        while True:
            msg = await websocket.recv()
            print(f"got {msg}")

    async def ssh_handler(self):
        # The async subprocess library only accepts a string command, not a list.
        command = f"ssh -o ConnectTimeout=2 {self.rm_host} cat {self.device}"

        x = 0
        y = 0
        pressure = 0
        throttle = 0
        eraser = False

        self.proc = await asyncio.create_subprocess_shell(
            command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        print("Started process")

        try:
            # Keep looping as long as the process is alive.
            # Terminated websocket connection is handled with a throw.
            while self.proc.returncode is None:
                buf = await self.proc.stdout.read(16)

                # TODO expect 16-bit chunks, or no data.
                # There are synchronisation signals in the data stream, maybe use those
                # if we drift somehow.
                if len(buf) == 16:
                    timestamp = buf[0:4]
                    a = buf[4:8]
                    b = buf[8:12]
                    c = buf[12:16]

                    # Using notes from https://github.com/ichaozi/RemarkableFramebuffer
                    # or https://github.com/canselcik/libremarkable/wiki
                    typ = b[0]
                    code = b[2] + b[3] * 0x100
                    val = c[0] + c[1] * 0x100 + c[2] * 0x10000 + c[3] * 0x1000000


                    # print("Type %d, code %d, val %d" % (typ, code, val))
                    # Pen side
                    if typ == 1:
                        # code = 320 = normal, 321 = erase
                        # val = 0 = off, 1 = closes in
                        # resend picture on 321 off?
                        if code == 321:
                            if val == 1:
                                eraser = True
                                print("eraser on")
                            else:
                                eraser = False
                                print("eraser off")
                                await self.websocket_broadcast(json.dumps(("redraw",)))

                    # 0= 20966, 1 = 15725
                    # Absolute position.
                    if typ == 3:
                        if code == 0:
                            y = val
                        elif code == 1:
                            x = val
                        elif code == 24:
                            pressure = val

                        throttle = throttle + 1
                        if not eraser and throttle % 6 == 0:
                            await self.websocket_broadcast(json.dumps((x, y, pressure)))
            print("Disconnected from ReMarkable.")

        finally:
            print("Disconnected from all.")
            self.running = False
            self.proc.kill()

    async def join(self):
        if self.proc:
            await self.proc.wait()


async def screenshot(path, request):
    cachetime=3
    if path.endswith("id=0"):
        cachetime=60
    await refresh_ss(cachetime)
    body = open("resnap.jpg", "rb").read()
    headers = [
        ("Content-Type", "image/jpeg"),
        ("Content-Length", str(len(body))),
        ("Connection", "close"),
    ]
    return (http.HTTPStatus.OK, headers, body)

async def http_handler(path, request):
    # only serve index file or defer to websocket handler.
    if path == "/websocket":
        return None
    elif path.startswith("/screenshot"):
        return await screenshot(path, request)
    elif path != "/":
        return (http.HTTPStatus.NOT_FOUND, [], "")

    body = open("index.html", "rb").read()
    headers = [
        ("Content-Type", "text/html"),
        ("Content-Length", str(len(body))),
        ("Connection", "close"),
    ]

    return (http.HTTPStatus.OK, headers, body)


def run(rm_host="remarkable", host="localhost", port=6789):
    # rm_model = check(rm_host)
    rm_model = "reMarkable 2.0"
    handler = WebsocketHandler(rm_host, rm_model)
    start_server = websockets.serve(
        handler.websocket_handler, host, port, ping_interval=1000,
        process_request=http_handler
    )

    print(f"Visit http://{host}:{port}/")

    asyncio.get_event_loop().run_until_complete(start_server)
    asyncio.get_event_loop().run_forever()


if __name__ == "__main__":
    run()
