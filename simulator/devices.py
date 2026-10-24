#!/usr/bin/env python3
"""Симулятор пристроїв: Варіант 1 «Енергоощадна розумна квартира»."""
import argparse
import json
import math
import os
import random
import signal
import threading
import time

import paho.mqtt.client as mqtt

OBJ = "flat"
HOST = os.getenv("MQTT_HOST", "localhost")
PORT = int(os.getenv("MQTT_PORT", "8883"))
CA = os.getenv("MQTT_CA", "certs/ca.crt")
PERIOD = int(float(os.getenv("TELEMETRY_PERIOD", "5")))  # період телеметрії, с


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


#  модель квартири
class World:
    def __init__(self, scenario, return_after, overload_seconds):
        self.lock = threading.RLock()
        self.scenario = scenario
        self.return_after = return_after
        self.overload_seconds = overload_seconds
        self.t0 = time.time()
        self.temp = 20.5
        self.outdoor = 6.0
        self.humidity = 45.0
        self.lux = 100.0
        self.presence = {"livingroom": False, "bedroom": False}
        self.lights = {z: {"state": "OFF", "level": 100} for z in self.presence}
        self.heater = {"state": "ON", "setpoint": 21.0}
        self.heating = False
        self.extra_load = 0.0
        self.next_change = 0.0
        if scenario == "overload":
            self.temp = 16.0
            self.heater = {"state": "ON", "setpoint": 25.0}

    def elapsed(self):
        return time.time() - self.t0

    def _update_presence(self, t):
        sc = self.scenario
        if sc == "lights":      # темно; рух у вітальні 5..25 с, далі нікого
            self.presence = {"livingroom": 5 <= t < 25, "bedroom": False}
        elif sc == "eco":       # нікого вдома; через return_after с хтось заходить
            self.presence = {"livingroom": t >= self.return_after, "bedroom": False}
        elif sc == "overload":  # людина вдома
            self.presence = {"livingroom": True, "bedroom": False}
        elif t >= self.next_change:  # normal: випадкове переміщення
            place = random.choice(["livingroom", "bedroom", "none"])
            self.presence = {z: (z == place) for z in self.presence}
            self.next_change = t + random.uniform(60, 240)

    def _lux_target(self):
        if self.scenario == "lights":
            return 20.0
        lt = time.localtime()
        h = lt.tm_hour + lt.tm_min / 60
        if 7 <= h <= 19:
            return 30 + 450 * math.sin(math.pi * (h - 7) / 12)
        return 5.0

    def step(self, dt=1.0):
        with self.lock:
            t = self.elapsed()
            self._update_presence(t)

            if self.scenario == "overload":
                self.extra_load = 1400.0 if 5 <= t < 5 + self.overload_seconds else 0.0

            # термостат з гістерезисом (edge-логіка в самому пристрої)
            h = self.heater
            if h["state"] == "ON":
                if self.temp < h["setpoint"] - 0.3:
                    self.heating = True
                elif self.temp > h["setpoint"] + 0.3:
                    self.heating = False
            else:
                self.heating = False

            # температура: тепловтрати до вулиці + нагрів
            self.outdoor += random.gauss(0, 0.01) + (6.0 - self.outdoor) * 0.0005
            gain = 0.02 if self.heating else 0.0
            self.temp += (0.0004 * (self.outdoor - self.temp) + gain) * dt + random.gauss(0, 0.002)

            # вологість і освітленість: плавно до цілі + шум
            people = sum(self.presence.values())
            target_h = 45 + 5 * people - 0.5 * (self.temp - 20)
            self.humidity += (target_h - self.humidity) * 0.05 + random.gauss(0, 0.15)
            target_l = self._lux_target()
            self.lux += (target_l - self.lux) * 0.2 + random.gauss(0, target_l * 0.01 + 0.3)
            self.lux = max(0.0, self.lux)

    def power(self):
        with self.lock:
            p = 120.0 + random.gauss(0, 8)  # базове навантаження
            for l in self.lights.values():
                if l["state"] == "ON":
                    p += 40.0 * l["level"] / 100
            if self.heating:
                p += 1500.0
            return p + self.extra_load + random.gauss(0, 15 if self.extra_load else 0)


# базовий MQTT-пристрій
class Device:
    def __init__(self, user, zone, name):
        self.user = user
        self.base = f"{OBJ}/{zone}/{name}"
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"sim-{user}")
        self.client.username_pw_set(user, os.getenv(f"MQTT_PASS_{user.upper()}"))
        self.client.tls_set(ca_certs=CA)  # перевірка сертифіката власним CA
        self.client.will_set(f"{self.base}/status", "offline", qos=1, retain=True)  # Last Will
        self.client.on_connect = self._on_connect
        self.client.on_message = self.on_message
        self.client.reconnect_delay_set(1, 10)

    def start(self):
        self.client.connect_async(HOST, PORT, keepalive=15)
        self.client.loop_start()

    def stop(self):
        try:
            # чисте відключення НЕ запускає Last Will, тому offline публікуємо самі
            self.client.publish(f"{self.base}/status", "offline", qos=1, retain=True).wait_for_publish(2)
        except Exception:
            pass
        self.client.disconnect()
        self.client.loop_stop()

    def _on_connect(self, client, userdata, flags, rc, props=None):
        if rc.is_failure:
            log(f"[{self.user}] помилка підключення: {rc}")
            return
        log(f"[{self.user}] підключено")
        client.publish(f"{self.base}/status", "online", qos=1, retain=True)
        self.on_ready()

    def on_ready(self):
        pass

    def on_message(self, client, userdata, msg):
        pass


# сенсори
class Sensor(Device):
    def __init__(self, user, zone, name, unit, getter, digits=1):
        super().__init__(user, zone, name)
        self.unit, self.getter, self.digits = unit, getter, digits
        self.last = None

    def value(self):
        v = self.getter()
        return int(v) if self.digits is None else round(v, self.digits)

    def publish(self):
        payload = {"value": self.value(), "unit": self.unit, "ts": int(time.time())}
        # QoS 0: телеметрія часта, втрата одного заміру некритична
        self.client.publish(f"{self.base}/telemetry", json.dumps(payload), qos=0)

    def publish_if_changed(self):  # датчик руху: подія одразу, не чекаючи періоду
        v = self.value()
        if v != self.last:
            self.last = v
            self.publish()


#  актуатори
class Light(Device):
    def __init__(self, world, zone):
        super().__init__(f"light_{zone}", zone, "light")
        self.world, self.zone = world, zone

    def on_ready(self):
        self.client.subscribe(f"{self.base}/set", qos=1)  # QoS 1: команда має дійти
        self.publish_state()

    def on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload)
            if not isinstance(data, dict):
                raise ValueError("очікується JSON-об'єкт")
            with self.world.lock:
                l = self.world.lights[self.zone]
                if "brightness" in data:
                    b = max(0, min(100, int(float(data["brightness"]))))
                    if b > 0:
                        l["level"], l["state"] = b, "ON"
                    else:
                        l["state"] = "OFF"
                if data.get("state") in ("ON", "OFF"):
                    l["state"] = data["state"]
            log(f"[{self.user}] команда {data}")
            self.publish_state()
        except (ValueError, TypeError) as e:
            log(f"[{self.user}] некоректна команда {msg.payload!r}: {e}")

    def publish_state(self):
        with self.world.lock:
            l = self.world.lights[self.zone]
            payload = {"state": l["state"], "brightness": l["level"] if l["state"] == "ON" else 0,
                       "ts": int(time.time())}
        self.client.publish(f"{self.base}/state", json.dumps(payload), qos=1, retain=True)


class Heater(Device):
    def __init__(self, world):
        super().__init__("heater", "hall", "heater")
        self.world = world
        self.last = None

    def on_ready(self):
        self.client.subscribe(f"{self.base}/set", qos=1)
        self.publish_state(force=True)

    def on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload)
            if not isinstance(data, dict):
                raise ValueError("очікується JSON-об'єкт")
            with self.world.lock:
                h = self.world.heater
                if data.get("state") in ("ON", "OFF"):
                    h["state"] = data["state"]
                if "setpoint" in data:
                    h["setpoint"] = max(5.0, min(30.0, float(data["setpoint"])))
            log(f"[{self.user}] команда {data}")
            self.publish_state(force=True)
        except (ValueError, TypeError) as e:
            log(f"[{self.user}] некоректна команда {msg.payload!r}: {e}")

    def publish_state(self, force=False):
        with self.world.lock:
            payload = {"state": self.world.heater["state"], "setpoint": self.world.heater["setpoint"],
                       "heating": "true" if self.world.heating else "false"}
        if force or payload != self.last:
            self.last = dict(payload)  # порівнюємо без ts
            payload["ts"] = int(time.time())
            self.client.publish(f"{self.base}/state", json.dumps(payload), qos=1, retain=True)


#  main
def main():
    ap = argparse.ArgumentParser(description="Симулятор квартири (варіант 1)")
    ap.add_argument("--scenario", choices=["normal", "lights", "eco", "overload"], default="normal")
    ap.add_argument("--return-after", type=float, default=90)
    ap.add_argument("--overload-seconds", type=float, default=150)
    args = ap.parse_args()

    w = World(args.scenario, args.return_after, args.overload_seconds)
    motions = [
        Sensor("motion_livingroom", "livingroom", "motion", "bool", lambda: w.presence["livingroom"], None),
        Sensor("motion_bedroom", "bedroom", "motion", "bool", lambda: w.presence["bedroom"], None),
    ]
    sensors = motions + [
        Sensor("temperature", "hall", "temperature", "C", lambda: w.temp, 1),
        Sensor("humidity", "hall", "humidity", "%", lambda: w.humidity, 1),
        Sensor("lux", "hall", "lux", "lx", lambda: w.lux, 0),
        Sensor("power", "main", "power", "W", w.power, 0),
    ]
    heater = Heater(w)
    devices = sensors + [Light(w, "livingroom"), Light(w, "bedroom"), heater]

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    log(f"Старт симулятора, сценарій: {args.scenario}, брокер {HOST}:{PORT}")
    for d in devices:
        d.start()

    tick = 0
    while not stop.is_set():
        stop.wait(1.0)
        w.step(1.0)
        for m in motions:
            m.publish_if_changed()
        heater.publish_state()
        tick += 1
        if tick % PERIOD == 0:
            for s in sensors:
                s.publish()

    log("Зупинка, публікую offline")
    for d in devices:
        d.stop()


if __name__ == "__main__":
    main()