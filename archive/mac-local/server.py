#!/usr/bin/env python3
"""RetroTailWind local server.

Serves the app, stores each team as a JSON file in ./data, and pushes live
updates to everyone in the room with Server-Sent Events. Python standard
library only, so nothing needs installing.

    python3 server.py            # then open http://localhost:4321
    PORT=8080 python3 server.py  # another port
"""
import hashlib
import json
import os
import queue
import re
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
APP = os.path.join(ROOT, "retrotailwind-preview.html")
PORT = int(os.environ.get("PORT", "4321"))
os.makedirs(DATA, exist_ok=True)

# Limits the server enforces. The facilitator's config (from the Tune panel) overrides them.
LIMITS = {"VOTES_PER_PERSON": 5, "MAX_VOTES_PER_CARD": 3, "MAX_WAYPOINTS": 3}

FAC_ONLY = {
    "phase", "names", "silent", "config", "cards.replace", "merge", "split", "why", "reef",
    "wp.add", "wp.update", "wp.remove", "review", "crew", "outcome", "sessions.seed",
    "session.next", "history",
}
ID = r"[A-Za-z0-9_-]{16,128}"

lock = threading.RLock()
teams = {}      # team id -> team dict
fac_index = {}  # sha256(facilitator key) -> team id
subs = {}       # team id -> set of queues (one per open browser)


class Reject(Exception):
    pass


def now_ms():
    return int(time.time() * 1000)


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def new_session():
    return {"id": secrets.token_urlsafe(9), "ship": "", "star": "", "lastStar": None, "phase": 0,
            "cards": [], "waypoints": [], "review": [], "crew": [], "hullStart": 100,
            "carriedDistance": None, "silentUntil": 0, "history": [], "outcome": None,
            "startedAt": now_ms()}


def load_all():
    for name in os.listdir(DATA):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(DATA, name)) as fh:
                t = json.load(fh)
            teams[t["id"]] = t
            fac_index[t["fkHash"]] = t["id"]
        except (OSError, ValueError, KeyError) as e:
            print(f"Skipping {name}: {e}")


def save(t):
    path = os.path.join(DATA, t["id"] + ".json")
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(t, fh)
    os.replace(tmp, path)


# ---- what other people are allowed to see -------------------------------------------

def silent(s):
    return (s.get("silentUntil") or 0) > now_ms()


def public_card(c, s):
    # During silent writing nobody, not even the server's other clients, learns who wrote what
    if silent(s) and not c.get("anonymous"):
        return {**c, "author": None, "authorName": None}
    return c


def public_team(t):
    s = t["current"]
    out = {k: v for k, v in t.items() if k != "fkHash"}
    out["current"] = {**s, "cards": [public_card(c, s) for c in s["cards"]]}
    return out


def public_action(t, a):
    if a["type"] == "card.add":
        return {**a, "card": public_card(a["card"], t["current"])}
    return a


# ---- the reducer: same actions as the browser's apply() --------------------------------

def find(s, cid):
    return next((c for c in s["cards"] if c["id"] == cid), None)


def reduce(t, a):
    s = t["current"]
    typ = a.get("type")
    lim = {**LIMITS, **{k: v for k, v in (t.get("config") or {}).items() if k in LIMITS}}

    if typ == "card.add":
        if s["phase"] != 1:
            raise Reject("Cards are closed")
        c = a.get("card") or {}
        if c.get("type") not in ("wind", "rock", "barnacle") or not str(c.get("text", "")).strip():
            raise Reject("Bad card")
        if find(s, c.get("id")):
            return False
        anon = bool(c.get("anonymous"))
        card = {"id": str(c["id"])[:40], "type": c["type"], "text": str(c["text"])[:200],
                "author": None if anon else c.get("author"),
                "authorName": None if anon else str(c.get("authorName") or "")[:30],
                "anonymous": anon, "votes": {}, "scraped": False, "created": now_ms()}
        s["cards"].append(card)
        a["card"] = card
    elif typ == "vote":
        if s["phase"] != 2:
            raise Reject("Voting is closed")
        c = find(s, a.get("cardId"))
        if not c or c.get("mergedInto"):
            raise Reject("No such card")
        pid = str(a.get("pid"))
        d = 1 if a.get("delta", 0) > 0 else -1
        cur = c["votes"].get(pid, 0)
        used = sum(x["votes"].get(pid, 0) for x in s["cards"])
        if d > 0 and (cur >= lim["MAX_VOTES_PER_CARD"] or used >= lim["VOTES_PER_PERSON"]):
            raise Reject("No votes left")
        if d < 0 and cur <= 0:
            raise Reject("No vote to remove")
        if cur + d:
            c["votes"][pid] = cur + d
        else:
            c["votes"].pop(pid, None)
        a["delta"] = d
    elif typ == "merge":
        p = find(s, a.get("primary"))
        if not p:
            raise Reject("No such card")
        for cid in a.get("ids", []):
            c = find(s, cid)
            if not c or c is p:
                continue
            c["mergedInto"] = p["id"]
            for x in s["cards"]:
                if x.get("mergedInto") == c["id"]:
                    x["mergedInto"] = p["id"]
            for pid, n in c["votes"].items():
                p["votes"][pid] = p["votes"].get(pid, 0) + n
            c["votes"] = {}
    elif typ == "split":
        for x in s["cards"]:
            if x.get("mergedInto") == a.get("primary"):
                x.pop("mergedInto", None)
    elif typ == "why":
        c = find(s, a.get("cardId"))
        if c:
            c["why"] = str(a.get("text", ""))[:500]
    elif typ == "reef":
        c = find(s, a.get("cardId"))
        if c:
            c["reef"] = bool(a.get("on"))
    elif typ == "wp.add":
        wp = a.get("wp") or {}
        if any(w["id"] == wp.get("id") for w in s["waypoints"]):
            return False
        if len(s["waypoints"]) >= lim["MAX_WAYPOINTS"]:
            raise Reject("Too many waypoints")
        s["waypoints"].append(wp)
        c = find(s, wp.get("cardId"))
        if c:
            c["scraped"] = True
    elif typ == "wp.update":
        w = next((w for w in s["waypoints"] if w["id"] == a.get("id")), None)
        if w:
            w.update({k: v for k, v in (a.get("patch") or {}).items() if k in ("text", "owner", "due", "star")})
    elif typ == "wp.remove":
        w = next((w for w in s["waypoints"] if w["id"] == a.get("id")), None)
        if w:
            s["waypoints"].remove(w)
            c = find(s, w.get("cardId"))
            if c:
                c["scraped"] = False
    elif typ == "review":
        w = next((w for w in s["review"] if w["id"] == a.get("id")), None)
        if w:
            w["status"] = a.get("status") if a.get("status") in ("done", "not-done") else None
    elif typ == "crew":
        for pid in a.get("ids", []):
            if pid not in s["crew"]:
                s["crew"].append(pid)
    elif typ == "phase":
        s["phase"] = max(0, min(4, int(a.get("phase", 0))))
    elif typ == "names":
        if a.get("ship") is not None:
            s["ship"] = str(a["ship"])[:60]
        if a.get("star") is not None:
            s["star"] = str(a["star"])[:90]
    elif typ == "silent":
        s["silentUntil"] = int(a.get("until") or 0)
    elif typ == "config":
        if isinstance(a.get("config"), dict):
            t["config"] = a["config"]
    elif typ == "cards.replace":
        cards = a.get("cards") or []
        ids = {c.get("id") for c in cards}
        s["cards"] = cards
        s["waypoints"] = [w for w in s["waypoints"] if not w.get("cardId") or w["cardId"] in ids]
    elif typ == "outcome":
        if a.get("outcome") in ("reached", "closer", "not-yet"):
            s["outcome"] = a["outcome"]
    elif typ == "sessions.seed":
        t["sessions"] = a.get("sessions") or []
    elif typ == "session.next":
        t["sessions"] = a.get("sessions") or []
        t["current"] = a.get("session") or new_session()
    elif typ == "history":
        s.setdefault("history", []).append({"t": now_ms(), "v": float(a.get("v", 0))})
        del s["history"][:-3000]
    else:
        raise Reject("Unknown action")
    return True


def broadcast(tid, msg):
    data = json.dumps(msg)
    for q in list(subs.get(tid, ())):
        q.put(data)


def lan_url():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))  # no packet is sent; this just picks the Wi-Fi interface
            return f"http://{sock.getsockname()[0]}:{PORT}"
    except OSError:
        return None


# ---- HTTP ------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "RetroTailWind"

    def log_message(self, fmt, *args):
        pass  # keep the terminal quiet

    def send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2_000_000:
            raise Reject("Too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html", "/retrotailwind-preview.html"):
            with open(APP, "rb") as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/info":
            self.send_json(200, {"lan": lan_url()})
        elif m := re.fullmatch(rf"/api/fac/({ID})", path):
            tid = fac_index.get(sha(m[1]))
            self.send_json(200, {"teamId": tid}) if tid else self.send_json(404, {"error": "Unknown facilitator link"})
        elif m := re.fullmatch(rf"/api/team/({ID})", path):
            with lock:
                t = teams.get(m[1])
                self.send_json(200, public_team(t)) if t else self.send_json(404, {"error": "Unknown team"})
        elif m := re.fullmatch(rf"/api/team/({ID})/events", path):
            self.events(m[1])
        else:
            self.send_json(404, {"error": "Not found"})

    def events(self, tid):
        if tid not in teams:
            return self.send_json(404, {"error": "Unknown team"})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        q = queue.Queue()
        with lock:
            subs.setdefault(tid, set()).add(q)
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    self.wfile.write(f"data: {q.get(timeout=15)}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except OSError:
            pass
        finally:
            with lock:
                subs.get(tid, set()).discard(q)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self.read_json()
        except (Reject, ValueError):
            return self.send_json(400, {"error": "Bad request"})

        if path == "/api/teams":
            tid, key = secrets.token_urlsafe(16), secrets.token_urlsafe(24)
            t = {"id": tid, "name": str(body.get("name") or "Our team")[:50], "created": now_ms(),
                 "fkHash": sha(key), "config": {}, "sessions": [], "current": new_session()}
            with lock:
                teams[tid] = t
                fac_index[t["fkHash"]] = tid
                save(t)
            return self.send_json(200, {"teamId": tid, "facKey": key})

        if m := re.fullmatch(rf"/api/team/({ID})/action", path):
            with lock:
                t = teams.get(m[1])
                if not t:
                    return self.send_json(404, {"error": "Unknown team"})
                is_fac = sha(self.headers.get("X-Fac-Key") or "") == t["fkHash"]
                if body.get("type") in FAC_ONLY and not is_fac:
                    return self.send_json(403, {"error": "Facilitator only"})
                try:
                    changed = reduce(t, body)
                except (Reject, ValueError, TypeError, KeyError) as e:
                    return self.send_json(409, {"error": str(e) or "Rejected"})
                if changed:
                    save(t)
                    broadcast(t["id"], public_action(t, body))
            return self.send_json(200, {"ok": True})

        if m := re.fullmatch(rf"/api/team/({ID})/presence", path):
            if m[1] not in teams or body.get("type") not in ("hello", "bye"):
                return self.send_json(404, {"error": "Unknown team"})
            msg = {"type": body["type"], "pid": body.get("pid"), "reply": bool(body.get("reply")),
                   "person": body.get("person"), "extras": (body.get("extras") or [])[:60]}
            with lock:
                broadcast(m[1], msg)
            return self.send_json(200, {"ok": True})

        self.send_json(404, {"error": "Not found"})


def main():
    load_all()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    print(f"\n  ⛵ RetroTailWind is running\n")
    print(f"  On this Mac:        http://localhost:{PORT}")
    lan = lan_url()
    if lan:
        print(f"  Team on same Wi-Fi: {lan}")
    print(f"  Data folder:        {DATA}\n\n  Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
