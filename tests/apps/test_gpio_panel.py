"""apps/gpio_panel.py in --simulate mode: gpiozero's mock pins stand in for a real button and LED, so the whole path (a pin changing shows on
the dashboard, a dashboard Toggle changes a pin) runs with no hardware."""

from __future__ import annotations

import json
import os
import signal
import time
from typing import Callable, List

import pytest

gpiozero = pytest.importorskip("gpiozero")

from pihub.protocol.bindings import Binding  # noqa: E402
from pihub.protocol.messages import CommandMessage  # noqa: E402

from apps import gpio_panel  # noqa: E402

DEVICE = "panel-1"
PANEL = gpio_panel.PROGRAM_ID


class Transport:
    def __init__(self) -> None:
        self.sent: List[str] = []

    def send(self, data: str) -> None:
        self.sent.append(data)

    def widgets(self, program: str) -> List[str]:
        manifest = json.loads(self.sent[0])["payload"]
        return [w["widgetId"] for p in manifest["programs"] if p["programId"] == program for w in p["widgets"]]

    def runtime(self, widget: str) -> list:
        found = []
        for raw in list(self.sent):
            payload = json.loads(raw).get("payload", {})
            binding = payload.get("binding", {})
            if binding.get("programId") == PANEL and binding.get("widgetId") == widget:
                found.append(payload.get("value"))
        return found


def wait_for(condition: Callable[[], bool], seconds: float = 5.0) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return
        time.sleep(0.02)
    assert condition(), "timed out"


@pytest.fixture(autouse=True)
def config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    # A registered machine, as after the first run: the app never names an id, it takes the one in the saved config.
    from pihub.device_config import DeviceConfig, default_config_path, save_device_config

    save_device_config(DeviceConfig(device_id=DEVICE, device_secret="secret", server="wss://example.invalid"), default_config_path())


@pytest.fixture
def make():
    made = []

    def make(*extra: str):
        transport = Transport()
        panel = gpio_panel.build(gpio_panel.parse_args(["--simulate", *extra]), transport=transport)
        made.append(panel)
        panel.device.start()
        panel.start()
        return panel, transport

    yield make
    for panel in made:
        shut_down(panel)


def shut_down(panel) -> None:
    if panel.led.closed:
        return
    panel.close()
    panel.device.stop()
    panel.button.close()
    panel.led.close()


def command(panel, widget: str, values: dict) -> None:
    binding = Binding(device_id=DEVICE, program_id=PANEL, widget_id=widget)
    panel.device.handle_incoming(CommandMessage(binding=binding, values=values).to_json())


def test_the_dashboard_has_the_button_the_led_and_a_test_press(make):
    panel, transport = make()

    assert set(transport.widgets(PANEL)) == {"button", "led", "test-press"}
    assert transport.runtime("button")[-1] == gpio_panel.RELEASED
    assert not panel.led.is_lit


def test_pressing_the_button_shows_pressed_then_released(make):
    panel, transport = make()
    pin = panel.button.pin

    pin.drive_low()  # a pull-up button: connected to GND is pressed
    wait_for(lambda: transport.runtime("button")[-1] == gpio_panel.PRESSED)
    pin.drive_high()
    wait_for(lambda: transport.runtime("button")[-1] == gpio_panel.RELEASED)


def test_the_test_press_button_does_what_the_real_button_does(make):
    panel, transport = make()

    command(panel, "test-press", {})

    wait_for(lambda: gpio_panel.PRESSED in transport.runtime("button"))
    wait_for(lambda: transport.runtime("button")[-1] == gpio_panel.RELEASED)


def test_the_toggle_switches_the_led(make):
    panel, transport = make()

    command(panel, "led", {"value": True})
    wait_for(lambda: panel.led.is_lit)
    command(panel, "led", {"value": False})
    wait_for(lambda: not panel.led.is_lit)


def test_the_led_is_off_at_start_and_the_toggle_is_not_remembered(make, tmp_path):
    panel, _ = make()
    command(panel, "led", {"value": True})
    wait_for(lambda: panel.led.is_lit)
    shut_down(panel)

    again, transport = make()  # a new start, as after a power cut: the saved Toggle must not turn the LED back on

    assert not again.led.is_lit
    assert again.light.value is False


def test_a_button_already_held_at_start_shows_pressed(make, monkeypatch):
    real_start = gpio_panel.GpioPanel.start

    def held_then_start(self):
        self.button.pin.drive_low()
        real_start(self)

    monkeypatch.setattr(gpio_panel.GpioPanel, "start", held_then_start)
    _, transport = make()

    wait_for(lambda: transport.runtime("button")[-1] == gpio_panel.PRESSED)


def test_the_same_pin_twice_is_refused():
    with pytest.raises(SystemExit) as exit:
        gpio_panel.build(gpio_panel.parse_args(["--simulate", "--button-pin", "5", "--led-pin", "5"]))
    assert "same pin" in str(exit.value)


def test_on_a_pi_with_no_working_pin_driver_the_app_says_what_to_install(monkeypatch):
    from gpiozero.pins.mock import MockFactory

    from pihub.gpio import PIN_DRIVER_HELP

    prepared = []
    monkeypatch.setattr("pihub.gpio.prepare_pins", lambda *a: prepared.append(True) or False)

    def no_driver(*args, **kwargs):
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(gpiozero.Device, "pin_factory", MockFactory())
    monkeypatch.setattr(gpiozero, "Button", no_driver)
    with pytest.raises(SystemExit) as exit:
        gpio_panel.build(gpio_panel.parse_args([]), transport=Transport())
    assert prepared == [True] and PIN_DRIVER_HELP in str(exit.value) and "Invalid argument" in str(exit.value)


def test_the_first_run_registers_by_link_code_and_takes_no_token(make, monkeypatch):
    """The newest method: the Device is built to register if needed (a link and a code, the platform assigns the id), the old
    `--register TOKEN` flag is gone, and `--server` reaches the Device."""
    real_device = gpio_panel.Device
    seen = {}
    monkeypatch.setattr(gpio_panel, "Device", lambda **kwargs: seen.update(kwargs) or real_device(**kwargs))

    make("--server", "http://dev.example")

    assert "register_if_needed" not in seen  # the default is automatic: it links itself at a terminal, and never waits without one
    assert seen["server"] == "http://dev.example"
    with pytest.raises(SystemExit):
        gpio_panel.parse_args(["--register", "TOKEN"])


def test_a_service_stop_switches_the_led_off_like_ctrl_c(monkeypatch):
    """systemd stops a program with SIGTERM, not Ctrl+C: the cleanup that puts the LED out must run for that too."""
    calls: List[str] = []

    class Recorder:
        def __init__(self, name: str) -> None:
            self.name = name

        def __getattr__(self, method: str):
            return lambda *args, **kwargs: calls.append(f"{self.name}.{method}")

    panel = Recorder("panel")
    panel.device, panel.button, panel.led = Recorder("device"), Recorder("button"), Recorder("led")
    monkeypatch.setattr(gpio_panel, "build", lambda args: panel)
    monkeypatch.setattr(gpio_panel.time, "sleep", lambda seconds: os.kill(os.getpid(), signal.SIGTERM))
    previous = signal.getsignal(signal.SIGTERM)
    try:
        gpio_panel.main(["--simulate"])
    finally:
        signal.signal(signal.SIGTERM, previous)

    assert calls.index("panel.close") < calls.index("device.stop") < calls.index("led.close")
