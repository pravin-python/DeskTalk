# FreeChat — LAN Real-Time Chat

Office ke LAN pe real-time chat. Pure Python, **sirf standard library** — koi `pip install` nahi.
Windows, macOS aur Ubuntu — teeno pe same code chalta hai.

```
┌─────────────┐        TCP 9009         ┌─────────────┐
│  Aapka PC   │ ◄─────────────────────► │  server.py  │  ← koi ek machine pe
│ 172.31.1.143│                         │ (hub)       │
└─────────────┘                         └─────────────┘
┌─────────────┐                               ▲
│ Dost ka PC  │ ──────────────────────────────┘
└─────────────┘        UDP 9010 = auto-discovery
```

---

## Setup (3 step)

### 1. Ek machine pe server chalao

Aapke PC pe (IP `172.31.1.143`):

```bash
python server.py
```

Output me aapka address dikhega — wahi dost ko dena hai:

```
  FreeChat server chal raha hai: 'DESKTOP-XYZ room'
  TCP port: 9009
  Dost ko ye address do:
      172.31.1.143:9009
```

> Server sirf **ek** machine pe chalega. Baaki sab client hain.
> Aap khud bhi client chala sakte ho usi machine pe.

### 2. Sab log client chalao

GUI (recommended):

```bash
python chat_gui.py
```

Window me **LAN scan** dabao — server apne aap mil jaayega — phir **Connect**.
Ya seedha IP de do:

```bash
python chat_gui.py --host 172.31.1.143 --name pravin
```

Terminal pasand hai to:

```bash
python chat_cli.py --host 172.31.1.143 --name pravin
```

```bash
python chat_cli.py --scan
```

### 3. Firewall allow karo (server wali machine pe)

Ye sabse common dikkat hai — connect nahi hota to 90% baar firewall hi hota hai.

**Windows** (PowerShell, Administrator ke roop me):

```powershell
New-NetFirewallRule -DisplayName "FreeChat TCP" -Direction Inbound -Protocol TCP -LocalPort 9009 -Action Allow
```

```powershell
New-NetFirewallRule -DisplayName "FreeChat UDP discovery" -Direction Inbound -Protocol UDP -LocalPort 9010 -Action Allow
```

**Ubuntu**:

```bash
sudo ufw allow 9009/tcp && sudo ufw allow 9010/udp
```

**macOS**: pehli baar Python ko incoming connection ka prompt aayega — **Allow** dabao.

---

## Chat ke andar commands

| Command | Kaam |
|---|---|
| `/dm <naam> <message>` | Private message kisi ek ko |
| `/who` | Kaun online hai |
| `/quit` | Exit |

GUI me right side ki user list me kisi naam pe **double-click** karo — `/dm` apne aap bhar jaayega.

---

## Files

| File | Kya hai |
|---|---|
| `server.py` | asyncio TCP hub + UDP discovery responder |
| `chat_gui.py` | Tkinter GUI client |
| `chat_cli.py` | Terminal client |
| `freechat/protocol.py` | Message format (newline-delimited JSON) |
| `freechat/client.py` | Client core — dono clients isi ko use karte hain |
| `freechat/discovery.py` | LAN broadcast scan |

---

## Requirements

- Python 3.8+ (aapke paas 3.10.11 hai ✓)
- Ubuntu pe GUI ke liye: `sudo apt install python3-tk`
- Sab machines **ek hi LAN/subnet** pe honi chahiye

---

## Options

```bash
python server.py --port 9009 --name "Dev team room"
python server.py --no-discovery          # UDP broadcast band
python server.py --host 172.31.1.143     # sirf ek interface pe bind
```

---

## Troubleshooting

**"Connect nahi hua"**
1. Server wali machine pe `server.py` chal raha hai? Terminal check karo.
2. Dusre PC se ping karo: `ping 172.31.1.143`
3. Firewall rule add kiya? (upar step 3)
4. Dono machines same subnet pe hain? (`172.31.1.x`)

**LAN scan me kuch nahi milta**
Bahut saare office networks UDP broadcast block karte hain. Ye normal hai —
seedha IP type kar do, TCP connection phir bhi chalega.

**"Naam already use me hai"**
Koi aur usi naam se juda hua hai. `--name` badal do.

---

## Scope note

Ye LAN ke liye bana hai, isliye **encryption aur authentication nahi hai** —
traffic plaintext JSON hai aur koi bhi koi bhi naam le sakta hai. Office ke trusted
network ke liye theek hai; internet pe expose mat karna (router pe port forward mat karo).
Agar aage chahiye to TLS (`ssl` module) aur ek shared password add kiya ja sakta hai.
