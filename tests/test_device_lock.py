"""One program at a time per saved device identity (sdk/pihub/device_lock.py)."""

import errno
import logging
import subprocess
import sys
import textwrap

import pytest

from pihub import Device, Program
from pihub import device_lock
from pihub.device_lock import DeviceInUseError, acquire, lock_path_for

pytestmark = pytest.mark.skipif(device_lock.fcntl is None, reason="needs fcntl (Linux, macOS)")


@pytest.fixture(autouse=True)
def not_a_service(monkeypatch):
    monkeypatch.delenv(device_lock.SERVICE_ENV, raising=False)


@pytest.fixture
def config_path(tmp_path):
    return tmp_path / "thepihub" / "config.json"


def test_the_first_program_gets_the_lock_and_a_second_is_told_who_has_it(config_path):
    first = acquire(config_path, "device 'pi'", script="/home/pi/weather_station.py")

    with pytest.raises(DeviceInUseError) as raised:
        acquire(config_path, "device 'pi'")

    message = str(raised.value)
    assert "device 'pi'" in message and "weather_station.py" in message and "pid" in message
    assert "Ctrl+C" in message and "XDG_CONFIG_HOME" in message  # both ways out
    first.release()


def test_releasing_it_lets_the_next_program_in(config_path):
    acquire(config_path, "device 'pi'").release()
    again = acquire(config_path, "device 'pi'")
    assert again is not None and again.held
    again.release()
    assert not again.held


def test_a_holder_that_is_a_service_is_named_with_the_command_that_stops_it(config_path, monkeypatch):
    monkeypatch.setenv(device_lock.SERVICE_ENV, "birdbox")
    holder = acquire(config_path, "device 'pi'")
    monkeypatch.delenv(device_lock.SERVICE_ENV)

    with pytest.raises(DeviceInUseError) as raised:
        acquire(config_path, "device 'pi'")

    assert "systemctl --user stop pihub-birdbox" in str(raised.value)
    holder.release()


def test_another_process_holds_it_and_its_exit_or_crash_frees_it(config_path):
    code = textwrap.dedent(
        """
        import sys, time
        from pathlib import Path
        from pihub.device_lock import acquire
        lock = acquire(Path(sys.argv[1]), "device 'pi'", script="child.py")
        print("held", flush=True)
        time.sleep(60)
        """
    )
    child = subprocess.Popen([sys.executable, "-c", code, str(config_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "held"
        with pytest.raises(DeviceInUseError) as raised:
            acquire(config_path, "device 'pi'")
        assert f"pid {child.pid}" in str(raised.value) and "child.py" in str(raised.value)
    finally:
        child.kill()  # a crash, not a clean exit
        child.wait()

    freed = acquire(config_path, "device 'pi'")
    assert freed is not None
    freed.release()


def test_a_service_waits_for_the_other_program_and_takes_over(config_path, monkeypatch, caplog):
    monkeypatch.setenv(device_lock.SERVICE_ENV, "birdbox")
    holder = acquire(config_path, "device 'pi'", wait=False)
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            holder.release()  # the experiment in the terminal ends

    with caplog.at_level(logging.WARNING, logger="pihub.device_lock"):
        taken = acquire(config_path, "device 'pi'", sleep=sleep)

    assert taken is not None and taken.held
    assert sleeps == [device_lock.WAIT_SECONDS] * 3
    assert caplog.text.count("waiting for it to stop") == 1  # said once, not every five seconds
    taken.release()


def test_a_terminal_program_does_not_wait_even_when_asked_not_to_by_default(config_path):
    holder = acquire(config_path, "device 'pi'")
    slept = []
    with pytest.raises(DeviceInUseError):
        acquire(config_path, "device 'pi'", sleep=slept.append)
    assert slept == []
    holder.release()


def test_no_fcntl_means_no_guard_and_no_error(config_path, monkeypatch):
    monkeypatch.setattr(device_lock, "fcntl", None)
    assert acquire(config_path, "device 'pi'") is None
    assert acquire(config_path, "device 'pi'") is None


def test_a_filesystem_that_refuses_locks_means_no_guard_and_no_error(config_path, monkeypatch):
    def refuse(fd, operation):
        raise OSError(errno.ENOLCK, "no locks available")

    monkeypatch.setattr(device_lock.fcntl, "flock", refuse)
    assert acquire(config_path, "device 'pi'") is None


def test_the_lock_file_sits_beside_the_saved_identity(config_path):
    held = acquire(config_path, "device 'pi'")
    assert lock_path_for(config_path) == config_path.with_name("device.lock")
    assert lock_path_for(config_path).exists()
    held.release()


# --- Device.start() / stop() -------------------------------------------------------------------------------------


class QuietTransport:
    """Enough transport for `start()`: sends go nowhere, and it never receives."""

    connected = True

    def send(self, envelope):
        pass

    def disconnect(self):
        pass


@pytest.fixture
def device(tmp_path, monkeypatch):
    """A device that builds its connection from a saved identity, as a real one does (here, from a stand-in)."""
    config = tmp_path / "cfg" / "thepihub" / "config.json"
    monkeypatch.setattr(Device, "_build_authenticated_transport", lambda self: QuietTransport())
    device = Device(id="pi", config_path=config, reconnect=False, webrtc=False, device_information=False)
    device.add_program(Program(id="p", name="P"))
    return device


def test_start_refuses_when_another_program_holds_the_identity_and_can_be_retried(device):
    holder = acquire(device._config_path, "device 'pi'")

    with pytest.raises(DeviceInUseError):
        device.start()

    holder.release()
    device.start()  # the refusal did not leave it half started
    device.stop()


def test_stop_gives_the_identity_back(device):
    device.start()
    with pytest.raises(DeviceInUseError):
        acquire(device._config_path, "device 'pi'")

    device.stop()

    again = acquire(device._config_path, "device 'pi'")
    assert again is not None
    again.release()


def test_a_start_that_fails_does_not_keep_the_identity_locked(device, monkeypatch):
    def fail(self):
        raise RuntimeError("no network")

    monkeypatch.setattr(Device, "_start_runtime_service", fail)

    with pytest.raises(RuntimeError, match="no network"):
        device.start()

    again = acquire(device._config_path, "device 'pi'")
    assert again is not None
    again.release()


def test_a_device_given_its_own_transport_takes_no_lock(tmp_path):
    config = tmp_path / "cfg" / "thepihub" / "config.json"
    holder = acquire(config, "device 'pi'")

    device = Device(id="pi", transport=QuietTransport(), config_path=config, webrtc=False, device_information=False)
    device.add_program(Program(id="p", name="P"))
    device.start()  # the tests, and console devices, share nothing with a neighbour
    device.stop()
    holder.release()
