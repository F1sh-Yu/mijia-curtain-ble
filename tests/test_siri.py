import builtins
import json
from pathlib import Path
import runpy
import socket
import threading
import tempfile
from unittest.mock import AsyncMock, Mock

import pytest


def test_ssh_client_uses_local_socket_without_bluetooth(monkeypatch, capsys):
    original = builtins.__import__

    def no_bluetooth(name, *args, **kwargs):
        if name.split(".")[0] in {"mibleak", "bleak", "CoreBluetooth"}:
            raise AssertionError("SSH client must not load Bluetooth")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_bluetooth)
    namespace = runpy.run_path(str(Path(__file__).resolve().parents[1] / "siri-curtain"))
    received = []
    with tempfile.TemporaryDirectory(dir="/tmp") as folder, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        path = Path(folder) / "control.sock"
        namespace["request"].__globals__["SOCKET"] = path
        server.bind(str(path))
        server.listen(1)
        server.settimeout(3)

        def respond():
            conn, _ = server.accept()
            with conn:
                received.append(conn.recv(1024))
                conn.sendall((json.dumps({"ok": True, "message": "status received"}) + "\n").encode())

        worker = threading.Thread(target=respond, daemon=True)
        worker.start()
        namespace["request"]("status")
        worker.join(timeout=3)
    assert received == [b"status\n"]
    assert capsys.readouterr().out == "status received\n"


@pytest.mark.parametrize("position,opens", [(100, 0), (0, 1), (50, 1)])
async def test_ensure_open_checks_position_and_sends_once(position, opens):
    namespace = runpy.run_path(str(Path(__file__).resolve().parents[1] / "siri-curtain"))
    curtain = AsyncMock()
    curtain.status.return_value = {"position": position}
    await namespace["ensure_open"](curtain)
    curtain.status.assert_awaited_once()
    assert curtain.open.await_count == opens


@pytest.mark.parametrize("position", [None, "error -1", -1, 101, True, float("nan")])
async def test_ensure_open_does_not_move_on_invalid_status(position):
    namespace = runpy.run_path(str(Path(__file__).resolve().parents[1] / "siri-curtain"))
    curtain = AsyncMock()
    curtain.status.return_value = {"position": position}
    with pytest.raises(ValueError):
        await namespace["ensure_open"](curtain)
    curtain.open.assert_not_awaited()


async def test_ensure_open_does_not_retry_timeout():
    namespace = runpy.run_path(str(Path(__file__).resolve().parents[1] / "siri-curtain"))
    curtain = AsyncMock()
    curtain.status.return_value = {"position": 0}
    curtain.open.side_effect = TimeoutError
    with pytest.raises(TimeoutError):
        await namespace["ensure_open"](curtain)
    curtain.open.assert_awaited_once()


async def test_ensure_open_does_not_move_when_read_fails():
    namespace = runpy.run_path(str(Path(__file__).resolve().parents[1] / "siri-curtain"))
    curtain = AsyncMock()
    curtain.status.side_effect = TimeoutError
    with pytest.raises(TimeoutError):
        await namespace["ensure_open"](curtain)
    curtain.open.assert_not_awaited()


@pytest.fixture
def retry_runner(monkeypatch):
    import mibleak.client
    import mibleak.curtain
    namespace = runpy.run_path(str(Path(__file__).resolve().parents[1] / "siri-curtain"))
    devices = [AsyncMock() for _ in range(3)]
    curtains = [AsyncMock() for _ in range(3)]
    for curtain in curtains:
        curtain.status.return_value = {"position": 0}
    factory = Mock(side_effect=devices)
    monkeypatch.setattr(mibleak.client, "MiMeshDevice", factory)
    monkeypatch.setattr(mibleak.curtain, "Curtain", lambda dev: curtains[devices.index(dev)])
    monkeypatch.setenv("CURTAIN_ADDRESS", "00000000-0000-0000-0000-000000000000")
    monkeypatch.setenv("CURTAIN_MAC", "AA:BB:CC:DD:EE:FF")
    monkeypatch.setattr(Path, "read_text", lambda self: json.dumps([
        {"mac": "AA:BB:CC:DD:EE:FF", "gatt_ltmk": "00" * 32}]))
    monkeypatch.setattr(namespace["asyncio"], "sleep", AsyncMock())
    return namespace["run"], devices, curtains, factory


async def test_connect_timeout_retries_before_single_open(retry_runner):
    run, devices, curtains, factory = retry_runner
    devices[0].connect.side_effect = TimeoutError
    await run("ensure-open")
    assert factory.call_count == 2
    devices[0].disconnect.assert_awaited_once()
    curtains[0].open.assert_not_awaited()
    curtains[1].open.assert_awaited_once()


async def test_read_disconnect_reconnects_and_rechecks(retry_runner):
    from bleak.exc import BleakError
    run, devices, curtains, factory = retry_runner
    curtains[0].status.side_effect = BleakError("disconnected")
    curtains[1].status.return_value = {"position": 100}
    await run("ensure-open")
    assert factory.call_count == 2
    for curtain in curtains:
        curtain.open.assert_not_awaited()


async def test_retry_exhaustion_never_opens(retry_runner):
    run, devices, curtains, factory = retry_runner
    for dev in devices:
        dev.connect.side_effect = TimeoutError
    with pytest.raises(Exception, match="连接.*3"):
        await run("ensure-open")
    assert factory.call_count == 3
    for dev, curtain in zip(devices, curtains):
        dev.disconnect.assert_awaited_once()
        curtain.open.assert_not_awaited()


@pytest.mark.parametrize("command", ["ensure-open", "open", "close"])
async def test_command_timeout_never_reconnects_or_resends(retry_runner, command):
    run, devices, curtains, factory = retry_runner
    method = curtains[0].close if command == "close" else curtains[0].open
    method.side_effect = TimeoutError
    with pytest.raises(TimeoutError):
        await run(command)
    assert factory.call_count == 1
    method.assert_awaited_once()
