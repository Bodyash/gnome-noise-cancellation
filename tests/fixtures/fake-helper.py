"""Private protocol fake. Never imports PipeWire or opens audio devices."""
import json
import signal
import sys
import time


scenario = sys.argv[1]
if scenario == "ignore-stop":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)


def reply(message):
    print(json.dumps(message), flush=True)


prepared = None
published = False
ticks = 0
for line in sys.stdin:
    message = json.loads(line)
    command = message["command"]
    if command == "prepare":
        identity = json.loads(message["key"])
        prepared = {"protocol": 1, "session": message["session"], "event": "prepared",
                    "sourceId": message["sourceId"], "sourceSerial": identity[1],
                    "cookie": int(identity[0]), "captureLinkId": 99,
                    "processedFrames": 1, "rate": 48000, "channels": 1,
                    "processing": True, "published": False, "configuration": message["processing"]}
        if scenario == 'wrong-config':
            prepared['configuration']['backend'] = 'speex'
        if scenario == 'missing-config':
            del prepared['configuration']
        if scenario == "wrong-source":
            prepared["sourceId"] += 1
        if scenario == "wrong-session":
            prepared["session"] = "stale"
        if scenario == "wrong-rate":
            prepared["rate"] = 44100
        if scenario == "unprocessed":
            prepared["processedFrames"] = 0
        if scenario == "early-active":
            prepared["event"] = "active"
            prepared["published"] = True
        if scenario == "malformed":
            print("{", flush=True)
        elif scenario == "oversized":
            print("x" * 5000, flush=True)
        elif scenario != "stall":
            reply(prepared)
    elif command == "publish":
        published = True
        reply({**prepared, "event": "active", "published": True, "outputId": 100})
        if scenario in ['idle-cycle', 'invalid-activity']:
            reply({**prepared, 'event': 'activity', 'published': True, 'outputId': 100,
                   'state': 'idle', 'processing': False, 'resumeBaseline': 0,
                   **({'outputId': 999} if scenario == 'invalid-activity' else {})})
        if scenario == "fault":
            reply({"protocol": 1, "session": message["session"], "event": "fault",
                   "message": "Physical microphone disappeared"})
        if scenario == "crash":
            raise SystemExit(3)
    elif command == "ping":
        if published: ticks += 1
        if published and scenario == 'idle-cycle' and ticks in [2, 3]:
            reply({**prepared, 'event': 'activity', 'published': True, 'outputId': 100,
                   'state': 'resuming' if ticks == 2 else 'processing',
                   'processing': ticks == 3, 'resumeBaseline': 1,
                   'processedFrames': 1 if ticks == 2 else 2})
        reply({"protocol": 1, "session": message["session"], "event": "pong"})
    elif command == 'stop' and scenario == 'handoff-stop':
        if message.get('handoff') is not True or not published: raise SystemExit(4)
        raise SystemExit(0)
if scenario == "ignore-stop":
    time.sleep(10)
