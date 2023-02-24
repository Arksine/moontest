#! /usr/bin/python3
# Unix Domain Socket Connection Test for Moonraker
#
# Copyright (C) 2022 Eric Callahan <arksine.code@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license
from __future__ import annotations
import os
import sys
import argparse
import ast
import asyncio
import pathlib
import json
import logging

from typing import Any, Dict, List, Optional

SOCKET_LIMIT = 20 * 1024 * 1024
MENU = [
    "List API Request Presets",
    "Select API Request Preset",
    "Manual API Entry",
    "Start Notification View",
]

class MoonrakerConnection:
    def __init__(
        self, sockpath: pathlib.Path, presets: List[Dict[str, Any]]
    ) -> None:
        self.sockpath = sockpath
        self.api_presets = presets
        self.pending_req: Dict[str, Any] = {}
        self.connected = False
        self.kb_fd = sys.stdin.fileno()
        self.out_fd = sys.stdout.fileno()
        os.set_blocking(self.kb_fd, False)
        os.set_blocking(self.out_fd, False)
        self.kb_buf = b""
        self.kb_fut: Optional[asyncio.Future[str]] = None
        self.pending_reqs: Dict[int, asyncio.Future[Dict[str, Any]]] = {}
        self.print_lock = asyncio.Lock()
        self.mode: int = 0
        self.need_print_help: bool = True
        self.print_notifications: bool = False
        self.manual_entry: Dict[str, Any] = {}
        self.max_method_len: int = max(
            [len(p.get("method", "")) for p in self.api_presets]
        )

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._loop.add_reader(self.kb_fd, self._process_keyboard)
        await self._connect()
        while True:
            try:
                if self.mode == 0:
                    if self.need_print_help:
                        self.need_print_help = False
                        await self._print_help()
                    await self._mode_menu()
                elif self.mode == 1:
                    await self._mode_select_preset()
                elif self.mode in (2, 3, 4):
                    await self._mode_manual_entry()
                elif self.mode == 5:
                    await self._mode_watch_notify()
                else:
                    await self.print(f"Invalid mode: {self.mode}")
            except Exception:
                logging.exception("Unix Test Error")
                await self.close()
                break

    async def _mode_menu(self) -> None:
        req = await self.input("Menu Index (? for Help): ")
        if req == "1":
            await self._print_presets()
        elif req == "2":
            self.mode = 1
        elif req == "3":
            self.manual_entry = {}
            self.mode = 2
        elif req == "4":
            self.mode = 5
        else:
            if req != "?":
                await self.print(f"Invalid Entry: {req}")
            await self._print_help()

    async def _mode_select_preset(self) -> None:
        req = await self.input("Preset Index (Press Enter to return to main menu): ")
        if not req:
            self.mode = 0
            self.need_print_help = True
            return
        if not req.isdigit():
            await self.print(f"Error: invalid selection {req}")
            return
        ret = await self._send_preset(int(req) - 1)
        if ret:
            await self.print(f"Response: {ret}\n")

    async def _mode_manual_entry(self) -> None:
        if self.mode == 2:
            req = await self.input("Method Name (Press Enter to return to main menu): ")
            if not req:
                self.mode = 0
                self.need_print_help = True
                self.manual_entry = {}
                return
            self.manual_entry["method"] = req
            self.mode = 3
        elif self.mode == 3:
            if "params" not in self.manual_entry:
                self.manual_entry["params"] = {}
            req = await self.input("Parameter Name (Press Enter to send request): ")
            if not req:
                # send request and print response
                ret = await self._send_manual_request()
                await self.print(f"Response: {ret}\n")
                self.manual_entry = {}
                self.mode = 2
                return
            self.manual_entry["params"][req] = None
            self.mode = 4
        elif self.mode == 4:
            params: Dict[str, Any] = self.manual_entry.get("params", {})
            if not params:
                self.mode = 3
                return
            last_key = list(params.keys())[-1]
            req = await self.input(f"Parameter '{last_key}' Value: ")
            if not req:
                await self.print(f"No value selected, removing parameter {last_key}")
                params.pop(last_key, None)
            else:
                try:
                    val = ast.literal_eval(req)
                except Exception as e:
                    await self.print(f"Error: invalid value {req}, raised {e}")
                    return
                params[last_key] = val
            self.mode = 3

    async def _mode_watch_notify(self) -> None:
        await self.print("Watching notifications, Press Enter to stop")
        await asyncio.sleep(1.)
        self.print_notifications = True
        ret = await self.input()
        self.print_notifications = False
        self.mode = 0
        self.need_print_help = True

    async def _print_help(self) -> None:
        msg = "\nMain Menu:\nIndex     Description"
        for idx, desc in enumerate(MENU):
            msg += f"\n{idx + 1:<10}{desc}"
        msg += (
            "\n?         Help (show this message)"
            "\nCTRL+C    Quit this application\n"
        )
        await self.print(msg)

    async def _print_presets(self) -> None:
        msg = (
            "\nAvailable API Presets\nIndex   "
            f"{'Method':<{self.max_method_len}}Params"
        )
        for idx, preset in enumerate(self.api_presets):
            method = preset.get("method", "invalid")
            params = preset.get("params", "")
            msg += f"\n{idx + 1:<10}{method:<{self.max_method_len}}{params}"
        await self.print(msg + "\n")

    async def _connect(self) -> None:
        print(f"Connecting to Moonraker at {self.sockpath}")
        while True:
            try:
                reader, writer = await asyncio.open_unix_connection(
                    self.sockpath, limit=SOCKET_LIMIT
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1.)
                continue
            break
        self.writer = writer
        self._loop.create_task(self._process_stream(reader))
        self.connected = True
        await self.print("Connected to Moonraker")
        self.manual_entry = {
            "method": "server.connection.identify",
            "params": {
                "client_name": "Unix Socket Test",
                "version": "0.0.1",
                "type": "other",
                "url": "https://github.com/Arksine/moontest"
            }
        }
        ret = await self._send_manual_request(False)
        self.manual_entry = {}
        await self.print(f"Client Identified With Moonraker: {ret}")

    async def _process_stream(
        self, reader: asyncio.StreamReader
    ) -> None:
        errors_remaining: int = 10
        while not reader.at_eof():
            try:
                data = await reader.readuntil(b'\x03')
                decoded = data[:-1].decode(encoding="utf-8")
                item: Dict[str, Any] = json.loads(decoded)
            except (ConnectionError, asyncio.IncompleteReadError):
                break
            except asyncio.CancelledError:
                raise
            except Exception:
                errors_remaining -= 1
                if not errors_remaining or not self.connected:
                    break
                continue
            errors_remaining = 10
            if "id" in item:
                fut = self.pending_reqs.pop(item["id"], None)
                if fut is not None:
                    fut.set_result(item)
            elif self.print_notifications:
                self._loop.create_task(self.print(f"Notification: {item}\n"))
        await self.print("Unix Socket Disconnection from _process_stream()")
        await self.close()

    def _make_rpc_msg(self, method: str, **kwargs) -> Dict[str, Any]:
        msg = {"jsonrpc": "2.0", "method": method}
        uid = id(msg)
        msg["id"] = uid
        self.pending_req = msg
        if kwargs:
            msg["params"] = kwargs
        return msg

    async def _send_manual_request(
        self, echo_request: bool = True
    ) -> Dict[str, Any]:
        if not self.manual_entry:
            return
        params = self.manual_entry.get("params")
        method = self.manual_entry["method"]
        message = self._make_rpc_msg(method, **params)
        fut = self._loop.create_future()
        self.pending_reqs[message["id"]] = fut
        if echo_request:
            await self.print(f"Sending: {message}")
        await self._write_message(message)
        return await fut

    async def _send_preset(self, index: int) -> Dict[str, Any]:
        if index < 0 or index >= len(self.api_presets):
            await self.print(f"Error: Preset index {index} out of range.")
            return {}
        preset = self.api_presets[index]
        if "method" not in self.api_presets[index]:
            await self.print(f"Error: Invalid Preset {preset}")
            return
        params: Dict[str, Any] = preset.get("params", {})
        if not isinstance(params, dict):
            params = {}
        message = self._make_rpc_msg(preset["method"], **params)
        fut = self._loop.create_future()
        self.pending_reqs[message["id"]] = fut
        await self.print(f"Sending: {message}")
        await self._write_message(message)
        return await fut

    async def _write_message(self, message: Dict[str, Any]) -> None:
        data = json.dumps(message).encode() + b"\x03"
        try:
            self.writer.write(data)
            await self.writer.drain()
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.close()

    async def input(self, prompt: str = "") -> str:
        if prompt:
            await self.print(prompt, is_line=False)
        self.kb_fut = self._loop.create_future()
        ret = await self.kb_fut
        self.kb_fut = None
        return ret

    async def print(self, message: str, is_line: bool = True) -> None:
        async with self.print_lock:
            if is_line:
                message += "\n"
            while message:
                fut = self._loop.create_future()
                self._loop.add_writer(self.out_fd, self._req_stdout, fut)
                await fut
                ret = os.write(self.out_fd, message.encode())
                message = message[ret:]
            sys.stdout.flush()

    def _req_stdout(self, fut: asyncio.Future) -> None:
        fut.set_result(None)
        self._loop.remove_writer(self.out_fd)

    def _process_keyboard(self) -> None:
        data = os.read(self.kb_fd, 4096)
        parts = data.split(b"\n", 1)
        parts[0] = self.kb_buf + parts[0]
        self.kb_buf = parts.pop()
        if parts and self.kb_fut is not None:
            self.kb_fut.set_result(parts[0].decode())

    async def close(self):
        if not self.connected:
            return
        self.connected = False
        self.writer.close()
        await self.writer.wait_closed()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Unix Socket Test Utility")
    parser.add_argument(
        "-s", "--socketfile", default="~/printer_data/comms/moonraker.sock",
        metavar='<socketfile>',
        help="Path to Moonraker Unix Domain Socket"
    )
    parser.add_argument(
        "-p", "--presets", default=None, metavar='<presetfile>',
        help="Path to API Presets Json File"
    )
    args = parser.parse_args()
    sockpath = pathlib.Path(args.socketfile).expanduser().resolve()
    pfile: Optional[str] = args.presets
    if pfile is None:
        parent = pathlib.Path(__file__).parent
        presetpath = parent.joinpath("unix_api_presets.json")
    else:
        presetpath = pathlib.Path(args.presets).expanduser().resolve()
    presets: List[Dict[str, Any]] = []
    if presetpath.exists():
        try:
            presets = json.loads(presetpath.read_text())
        except Exception:
            print(f"Failed to load API Presets from file {presetpath}")
        else:
            if not isinstance(presets, list):
                print(f"Invalid JSON object in preset file {presetpath}")
                presets = []
    conn = MoonrakerConnection(sockpath, presets)
    try:
        asyncio.run(conn.run())
    except KeyboardInterrupt:
        print("\n")
        pass
