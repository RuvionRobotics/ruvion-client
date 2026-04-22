# Datagram Reception Debugging Steps

Das `diagnose.py` Script zeigt jetzt Debug-Logs von aioquic und präzise Timings. Führe folgende Tests nacheinander aus:

## Test 1: Packet Capture (parallel zu diagnose.py)

In einem Terminal:
```bash
# Terminal A: tcpdump starten (erfasst UPD traffic auf port 4433)
sudo tcpdump -i any -n 'udp port 4433' -w /tmp/ruvion.pcap -v

# Terminal B: diagnose starten
cd /path/to/ruvion-client
python examples/diagnose.py
```

Nach diagnose-Durchlauf:
```bash
# Terminal A: tcpdump mit Ctrl+C stoppen
# Dann anschauen:
tcpdump -r /tmp/ruvion.pcap -v
```

**Was zu erwarten ist:**
- Wenn nur 2 Datagrams bei TCP ankommt: Problem liegt auf der Leitung (Netzwerk/Server)
- Wenn Server viele UDP-Pakete sendet, aber Python nur 2 sieht: Kernel/Socket-Problem

## Test 2: Socket Buffer Check

```bash
# Größe des UDP Receive-Buffers:
python -c "
import socket
s = socket.socket()
try:
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4194304)  # 4MB
    print('Set rcvbuf to 4MB')
except Exception as e:
    print(f'Could not set: {e}')
"
```

Dann diagnose nochmal laufen lassen — hilft wenn Buffer zu klein ist.

## Test 3: IPv4 oder Global IPv6 versuchen

Falls nur link-local verfügbar ist, könnte das das Problem sein. Wenn der Controller eine andere IP hat, probier die:

```python
# In diagnose.py, vor discover_once():
controllers = await discover_once(timeout=DISCOVER_TIMEOUT)
c = controllers[0]
print(f"All addresses: {c.addresses}")
print(f"Hostname: {c.hostname}")

# Falls mehrere Adressen da sind, manuell eine versuchen:
conn = await connect_controller(c, cert_dir=CERT_DIR)
```

Wenn link-local das Problem ist, sollte eine andere IP funktionieren.

## Test 4: Server Logs anschauen

Wenn Zugriff auf den Controller hast:
```bash
ssh controller_ip
# Schau nach:
journalctl -u ruvion-controller -f
# oder
tail -f /var/log/ruvion/controller.log
```

Suche nach:
- "datagram too large"
- "MTU"
- "ACK timeout"
- Anzahl gesendeter Datagrams

## Test 5: Einfacher UDP Echo Test (ohne QUIC)

Teste ob die Netzwerk-Route allgemein funktioniert:

```bash
# Terminal A (auf Server, falls Zugriff):
nc -u -l 0.0.0.0 9999

# Terminal B (auf Client):
for i in {1..100}; do
  echo "test $i" | nc -u <server_ip> 9999
  sleep 0.01
done
```

Wenn die meisten Pakete ankommen, liegt Problem spezifisch bei QUIC/datagrams.

## Test 6: QUIC Congestion Window / Retransmission

Die aioquic DEBUG logs (jetzt standardmäßig aktiviert) zeigen:
```
quic [DEBUG] ...PTO timeout
quic [DEBUG] ...congestion window changed
quic [DEBUG] ...retransmit
```

Wenn viele PTO/retransmit: Server wartet auf ACKs (ist nicht angekommen).

---

## Vermutet nach erstem Lauf

Gegeben dass:
- Handshake schnell geht (1.1s mit parallel attempts)
- Aber nur 2 Datagrams in 10s

**Wahrscheinliche Diagnose:**
1. **Server sendet, Client empfängt nicht**: Netzwerk-Problem (tcpdump klärt das)
2. **Server sendet gar nicht**: Server wartet auf ACKs von Client (ausfallende Reverse-Route)
3. **MTU-Problem**: Server schickt nur initial 1-2 Pakete, sieht ACKs, merkt dass Datagrams zu groß sind, stoppt

Starte mit **Test 1 (tcpdump)** — das teilt das Feld am schnellsten.
