# aria2 Verbindungsrobustheit

## Problem (behoben)

Der Fehler `Network error talking to aria2: Cannot write to closing transport` trat auf wenn:

1. `get_all()` drei parallele Anfragen via `asyncio.gather()` absetzte
2. Die ersten Sessions bereits schlossen während die letzte noch schrieb
3. aiohttp versuchte auf eine schließende Session zu schreiben

## Lösung

**Jede HTTP-Anfrage bekommt einen eigenen Connector mit `force_close=True`:**

```python
connector = aiohttp.TCPConnector(force_close=True)
async with aiohttp.ClientSession(timeout=self.timeout, connector=connector) as session:
    ...
```

Das stellt sicher dass jede Session eine eigene Verbindung öffnet und sauber schließt.

## Fehlerklassen

| Klasse | Bedeutung | Logging |
|--------|-----------|---------|
| `Aria2ConnectionError` | Netzwerkfehler (nicht erreichbar, Transport schließt) | WARNING |
| `Aria2RPCError` | Fehler in der RPC-Logik (ungültige Parameter etc.) | ERROR |

`Aria2ConnectionError` ist Subklasse von `Aria2RPCError` für Abwärtskompatibilität.

## Verhalten bei Verbindungsausfall

- `get_all()` gibt `[]` zurück statt zu werfen → Scheduler läuft weiter
- `ensure_download()` retried bei `Aria2ConnectionError` mit Backoff (1s, 4s, 9s, ...)
- `_best_effort()`-Aufrufe (pause, remove) loggen auf DEBUG-Level bei Fehlern
