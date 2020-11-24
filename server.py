import asyncio
import argparse
import http
import json
import os
import time
import subprocess
import sys

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
        subprocess.run(['../scripts/host/resnap.sh', 'resnap.new.jpg'],
                       check=True)
        os.rename('resnap.new.jpg', 'resnap.jpg')
        print("ok")
    except subprocess.CalledProcessError:
        print("Could not resnap, continuing anyway")


class WebsocketHandler():
    def __init__(self, rm_host, rm_model, args):
        if rm_model == "reMarkable 1.0":
            self.device = "/dev/input/event0"
        elif rm_model == "reMarkable 2.0":
            self.device = "/dev/input/event1"
        else:
            raise NotImplementedError(f"Unsupported reMarkable Device : {rm_model}")

        self.rm_host = rm_host
        self.args = args
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
            tasks = []
            tasks.append(asyncio.create_task(self.ssh_stream()))
            if self.args.autorefresh:
                tasks.append(asyncio.create_task(self.ssh_pagechange()))
            tasks.append(asyncio.create_task(self.read_websocket(websocket)))
            await asyncio.gather(*tasks)
        else:
            await self.read_websocket(websocket)

    async def read_websocket(self, websocket):
        while True:
            msg = await websocket.recv()
            print(f"got {msg}")

    async def ssh_pagechange(self):
        command = f"ssh -o ConnectTimeout=2 {self.rm_host} /opt/bin/inotifywait -m -e CLOSE .local/share/remarkable/xochitl/"
        proc = await asyncio.create_subprocess_shell(
            command, stdout=asyncio.subprocess.PIPE
        )
        print("Started pagechange watcher process")
        try:
            last = time.time()
            while proc.returncode is None:
                buf = await proc.stdout.read(1000)

                if not buf:
                    try:
                        await asyncio.wait_for(proc.wait(), 1)
                    except TimeoutError:
                        continue
                    print(f"pagechange watcher return code {proc.returncode}")
                    break
                if b'metadata' in buf:
                    now = time.time()
                    if now - last > 2:
                        print("page change")
                        await self.websocket_broadcast(json.dumps(("redraw",)))
                        last = now
            print("Disconnected from ReMarkable.")

        finally:
            print("Disconnected from all.")
            if proc.returncode is None:
                proc.kill()

    async def ssh_stream(self):
        # The async subprocess library only accepts a string command, not a list.
        command = f"ssh -o ConnectTimeout=2 {self.rm_host} cat {self.device}"

        x = 0
        y = 0
        pressure = 0
        throttle = 0
        eraser = False

        proc = await asyncio.create_subprocess_shell(
            command, stdout=asyncio.subprocess.PIPE
        )
        print("Started event stream process")

        try:
            # Keep looping as long as the process is alive.
            # Terminated websocket connection is handled with a throw.
            while proc.returncode is None:
                buf = await proc.stdout.read(16)

                if not buf:
                    try:
                        await asyncio.wait_for(proc.wait(), 1)
                    except TimeoutError:
                        continue
                    break

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
                                await self.websocket_broadcast(
                                        json.dumps(("redraw",)))

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
                            await self.websocket_broadcast(
                                    json.dumps((x, y, pressure)))
            print("Disconnected from ReMarkable.")

        finally:
            print("Disconnected from all.")
            self.running = False
            if proc.returncode is None:
                proc.kill()



async def screenshot(path, request):
    cachetime = 2
    if path.endswith("id=0"):
        cachetime = 30
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
    if path.startswith("/screenshot"):
        return await screenshot(path, request)
    if path != "/":
        return (http.HTTPStatus.NOT_FOUND, [], "")

    body = open("index.html", "rb").read()
    headers = [
        ("Content-Type", "text/html"),
        ("Content-Length", str(len(body))),
        ("Connection", "close"),
    ]

    return (http.HTTPStatus.OK, headers, body)


def run():
    parser = argparse.ArgumentParser(
            description='stream remarkable')
    parser.add_argument(
            '-r', '--rm_host', default='remarkable',
            help='remarkable host')
    parser.add_argument(
            '-s', '--server_host', default='localhost',
            help='websocket server listen host')
    parser.add_argument(
            '-p', '--port', default=6789,
            help='websocket server port')
    parser.add_argument(
            '-n', '--no-autorefresh', dest='autorefresh', default=True,
            action="store_false",
            help='trigger refresh on page change etc')
    parser.add_argument(
            '--auth', type=str, help='basic user:pass auth')
    args = parser.parse_args()
    # rm_model = check(rm_host)
    rm_model = "reMarkable 2.0"
    handler = WebsocketHandler(args.rm_host, rm_model, args)
    create_protocol = None
    if args.auth:
        creds = args.auth.split(':')
        if len(creds) != 2:
            print('--auth arg must be of form user:pass')
            sys.exit(1)
        create_protocol = websockets.basic_auth_protocol_factory(
            realm="remarkable stream",
            credentials=tuple(creds),
        )
    start_server = websockets.serve(
        handler.websocket_handler, args.server_host, args.port,
        ping_interval=1000, process_request=http_handler,
        create_protocol=create_protocol
    )

    print(f"Visit http://{args.server_host}:{args.port}/")

    asyncio.get_event_loop().run_until_complete(start_server)
    asyncio.get_event_loop().run_forever()


if __name__ == "__main__":
    run()
