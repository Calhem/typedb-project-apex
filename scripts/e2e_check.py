"""End-to-end demonstration check against a running Apex (docker compose up -d --build).

    uv run python -m scripts.e2e_check                      # data, answer key and every view in a browser
    uv run python -m scripts.e2e_check --screenshots DIR    # also save the README screenshots (1440 px)

1. TypeDB answers, the "apex" database holds exactly the records in data/seed (1,058), and the Impact answer
   key holds (61, 120 and 136 days for the CMA clearance).
2. Every main view and every guided-demonstration step opens in headless Chrome (Google Chrome or Chromium,
   found automatically or via $CHROME) at 1440 and 1280 px, with no console error, no unnamed button or link,
   no duplicate id and no sideways page scroll.

Standard library plus the apex package only; nothing is installed or sent anywhere: the browser is driven over
its local DevTools port and only visits 127.0.0.1.
"""

import argparse
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from apex import queries
from apex.db import READ, connect
from apex.seed import expected_counts, read_seed
from apex.settings import load_settings

APP = os.environ.get("APEX_URL", "http://127.0.0.1:8091")
PORT = 9339
VIEWS = ["/", "/?cp=DL-HELIX-WAIVER", "/workstreams", "/workstreams/WS-REG", "/parties", "/parties/PER-DONNELLY",
         "/parties/ORG-CMA", "/explorer", "/explorer?view=group", "/explorer?view=paths&from=PER-AINSWORTH&to=ORG-CMA",
         "/impact?item=CP-CMA&days=61", "/analyst", "/analyst?i=who-acts", "/analyst?i=regulatory-delay", "/guide",
         "/?advantage=schema-constraints"]
README_SHOTS = [("/", "deal-overview.png", (1440, 1880)), ("/explorer", "explorer-network.png", (1440, 900)),
                ("/explorer?view=group", "explorer-group-structure.png", (1440, 900)),
                ("/impact?item=CP-CMA&days=61", "impact-61-days.png", (1440, 900))]
READY = "!document.querySelector('[data-slot=skeleton]') && document.querySelector('main h1')"
AUDIT = r"""(() => {
  const name = (el) => (el.getAttribute('aria-label') || el.getAttribute('title') || el.innerText || el.getAttribute('aria-labelledby') || '').trim();
  const nameless = [...document.querySelectorAll('button, a[href], [role=button]')].filter((el) => !name(el)).length;
  const ids = [...document.querySelectorAll('[id]')].map((e) => e.id);
  return { h1: document.querySelectorAll('h1').length, nameless, duplicate_ids: ids.length - new Set(ids).size,
           sideways: document.documentElement.scrollWidth > window.innerWidth };
})()"""


def check_data():
    settings = load_settings()
    expected = expected_counts(read_seed())
    with connect(settings) as driver, driver.transaction(settings.apex_database, READ) as tx:
        found = queries.counts_by_type(tx, sorted(expected))
    assert found == expected, {k: (v, found.get(k)) for k, v in expected.items() if found.get(k) != v}
    health = json.load(urllib.request.urlopen(f"{APP}/api/health", timeout=10))
    assert health["status"] == "ok", health
    impact = json.load(urllib.request.urlopen(f"{APP}/api/impact?item=CP-CMA", timeout=30))["data"]
    breaks = sorted(d["breach_from_days"] for d in impact["deadlines"])
    assert breaks == [61, 120, 136], breaks
    print(f"data: {sum(found.values())} records in '{settings.apex_database}', matching data/seed; answer key {breaks}")


# ---- A small DevTools client (WebSocket over the standard library) --------------------------------

class Tab:
    def __init__(self, url):
        host, rest = url[len("ws://"):].split("/", 1)
        h, p = host.split(":")
        self.s = socket.create_connection((h, int(p)))
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET /{rest} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.s.recv(4096)
        self.extra, self.n, self.events = buf.split(b"\r\n\r\n", 1)[1], 0, []

    def _read(self, k):
        while len(self.extra) < k:
            self.extra += self.s.recv(1 << 20)
        out, self.extra = self.extra[:k], self.extra[k:]
        return out

    def call(self, method, **params):
        self.n += 1
        data = json.dumps({"id": self.n, "method": method, "params": params}).encode()
        n = len(data)
        head = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536
                                 else bytes([0x80 | 127]) + struct.pack(">Q", n))
        mask = os.urandom(4)
        self.s.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
        while True:
            payload = b""
            while True:
                b1, b2 = self._read(2)
                k = b2 & 0x7F
                k = struct.unpack(">H", self._read(2))[0] if k == 126 else struct.unpack(">Q", self._read(8))[0] if k == 127 else k
                payload += self._read(k)
                if b1 & 0x80:
                    break
            msg = json.loads(payload)
            if "method" in msg:
                self.events.append(msg)
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def js(self, expr):
        return self.call("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True).get("result", {}).get("value")

    def open(self, path, ready=READY, timeout=20):
        self.call("Page.navigate", url=APP + path)
        end = time.time() + timeout
        time.sleep(0.4)
        while not self.js(f"!!({ready})"):
            if time.time() > end:
                raise TimeoutError(path)
            time.sleep(0.2)
        time.sleep(1.0)

    def errors(self):
        bad = [m for m in self.events if m["method"] == "Runtime.exceptionThrown"
               or (m["method"] == "Log.entryAdded" and m["params"]["entry"]["level"] == "error")
               or (m["method"] == "Runtime.consoleAPICalled" and m["params"]["type"] == "error")]
        self.events.clear()
        return bad


def find_chrome():
    for c in [os.environ.get("CHROME"), "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              shutil.which("google-chrome"), shutil.which("chromium"), shutil.which("chromium-browser")]:
        if c and Path(c).exists():
            return c
    return None


def check_browser(shots):
    chrome = find_chrome()
    if not chrome:
        print("browser: skipped (no Chrome or Chromium found; set $CHROME)")
        return True
    profile = tempfile.mkdtemp(prefix="apex-e2e-")
    proc = subprocess.Popen([chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--remote-debugging-port={PORT}",
                             "--no-first-run", f"--user-data-dir={profile}", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ok = True
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version")
                break
            except OSError:
                time.sleep(0.2)
        guide = [s["to"] for s in json.load(urllib.request.urlopen(f"{APP}/api/guide"))["data"]["steps"]]
        for width, height in ((1440, 900), (1280, 800)):
            tab = Tab(json.load(urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{PORT}/json/new?about:blank", method="PUT")))["webSocketDebuggerUrl"])
            tab.call("Page.enable"); tab.call("Runtime.enable"); tab.call("Log.enable")
            tab.call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=False)
            for path in dict.fromkeys(VIEWS + guide):
                tab.open(path)
                audit, errors = tab.js(AUDIT), tab.errors()
                fine = not errors and audit["h1"] == 1 and not audit["nameless"] and not audit["duplicate_ids"] and not audit["sideways"]
                ok &= fine
                print(f"browser {width}px {'ok  ' if fine else 'FAIL'} {path}" + ("" if fine else f"  {audit} {len(errors)} console errors"))
            if shots and width == 1440:
                Path(shots).mkdir(parents=True, exist_ok=True)
                for path, name, (w, h) in README_SHOTS:
                    tab.call("Emulation.setDeviceMetricsOverride", width=w, height=h, deviceScaleFactor=1, mobile=False)
                    tab.open(path)
                    data = tab.call("Page.captureScreenshot", format="png")["data"]
                    (Path(shots) / name).write_bytes(base64.b64decode(data))
                    print(f"screenshot {Path(shots) / name}")
                tab.call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=False)
            tab.s.close()
    finally:
        proc.terminate()
        shutil.rmtree(profile, ignore_errors=True)
    return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--screenshots", metavar="DIR", help="also save the README screenshots here")
    args = parser.parse_args()
    check_data()
    ok = check_browser(args.screenshots)
    print("end-to-end check: " + ("PASSED" if ok else "FAILED"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
