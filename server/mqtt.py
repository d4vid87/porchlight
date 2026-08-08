"""Publish one MQTT message, so Home Assistant (or anything else on the broker)
hears about camera events.

ponytail: MQTT 3.1.1 QoS 0 over a plain socket, connect-publish-disconnect per
message -- alerts are rare and there is no session worth keeping. Swap in
paho-mqtt if anyone needs TLS, QoS 1 or a persistent connection.
"""

import socket
import struct


def _remaining(n):
    """MQTT's variable-length integer."""
    out = b""
    while True:
        b = n % 128
        n //= 128
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _string(s):
    b = s.encode()
    return struct.pack("!H", len(b)) + b


def publish(broker, topic, payload, user=None, password=None, retain=False,
            client_id="porchlight"):
    """True when the broker took the message. broker is "host" or "host:port"."""
    host, _, port = broker.partition(":")
    if not host:
        return False
    flags, creds = 0x02, b""                      # clean session
    if user:
        flags |= 0x80
        creds += _string(user)
        if password:
            flags |= 0x40
            creds += _string(password)
    var = _string("MQTT") + bytes([4, flags]) + struct.pack("!H", 30)
    connect = var + _string(client_id) + creds
    pub = _string(topic) + payload.encode()
    try:
        with socket.create_connection((host, int(port or 1883)), timeout=10) as s:
            s.settimeout(10)
            s.sendall(b"\x10" + _remaining(len(connect)) + connect)
            ack = s.recv(4)
            if len(ack) < 4 or ack[0] != 0x20 or ack[3] != 0:
                return False
            s.sendall(bytes([0x30 | (0x01 if retain else 0)])
                      + _remaining(len(pub)) + pub)
            s.sendall(b"\xe0\x00")                # DISCONNECT
    except Exception:
        return False
    return True


if __name__ == "__main__":       # tiny self-check: packet shapes, no broker needed
    assert _remaining(0) == b"\x00" and _remaining(127) == b"\x7f"
    assert _remaining(128) == b"\x80\x01" and _remaining(321) == b"\xc1\x02"
    assert _string("ab") == b"\x00\x02ab"
    assert publish("", "x", "y") is False
    print("ok")
