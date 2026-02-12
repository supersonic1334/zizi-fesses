"""
RemoteDesk Pro - Serveur Relais V2
====================================
Un seul port. Serveurs ET panels se connectent ici.
Le relais fait passer toutes les données entre les deux.
"""

import socket, threading, json, time, os, struct, uuid

PORT   = int(os.environ.get('PORT', 8765))
SECRET = os.environ.get('RD_SECRET', 'rd2024')

serveurs = {}
panels   = {}
lock     = threading.Lock()

def log(m): print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)

def send_msg(sock, data: bytes):
    sock.sendall(struct.pack('>I', len(data)) + data)

def send_json(sock, obj):
    send_msg(sock, json.dumps(obj).encode())

def recv_exact(sock, n):
    buf = b''
    while len(buf) < n:
        c = sock.recv(min(n - len(buf), 65536))
        if not c: raise ConnectionError("socket fermé")
        buf += c
    return buf

def recv_msg(sock):
    size = struct.unpack('>I', recv_exact(sock, 4))[0]
    if size > 60_000_000: raise ValueError(f"trop grand: {size}")
    return recv_exact(sock, size)

def handle(sock, addr):
    ip = addr[0]
    try:
        sock.settimeout(20)
        raw = recv_msg(sock)
        msg = json.loads(raw.decode())
        if msg.get('secret') != SECRET:
            send_json(sock, {'type': 'error', 'msg': 'secret invalide'})
            return
        role = msg.get('role')
        if role == 'server':
            handle_server(sock, ip, msg)
        elif role == 'panel':
            handle_panel(sock, ip, msg)
    except Exception as e:
        log(f"Erreur {ip}: {e}")
    finally:
        try: sock.close()
        except: pass

def handle_server(sock, ip, msg):
    session_id = str(uuid.uuid4())[:8]
    nom_pc     = msg.get('nom_pc', f'PC-{ip}')
    sock.settimeout(None)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    with lock:
        serveurs[session_id] = {'sock': sock, 'nom_pc': nom_pc, 'ip': ip,
                                 'session_id': session_id, 'panel_sock': None}
    log(f"[+] Serveur: {nom_pc} ({ip}) session={session_id}")
    _notif_panels({'type': 'new_server', 'session_id': session_id, 'nom_pc': nom_pc, 'ip': ip})
    send_json(sock, {'type': 'registered', 'session_id': session_id})
    try:
        while True:
            sock.settimeout(30)
            try:
                data = recv_msg(sock)
                try:
                    ctrl = json.loads(data.decode())
                    if ctrl.get('type') == 'pong': continue
                except: pass
                with lock:
                    panel_sock = serveurs.get(session_id, {}).get('panel_sock')
                if panel_sock:
                    try: send_msg(panel_sock, data)
                    except: pass
            except socket.timeout:
                try: send_json(sock, {'type': 'ping'})
                except: break
    except Exception:
        pass
    finally:
        with lock: serveurs.pop(session_id, None)
        log(f"[-] Serveur déco: {nom_pc}")
        _notif_panels({'type': 'server_gone', 'session_id': session_id})

def handle_panel(sock, ip, msg):
    panel_id = str(uuid.uuid4())[:8]
    sock.settimeout(None)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    with lock:
        panels[panel_id] = {'sock': sock, 'target_session': None}
    log(f"[+] Panel: {ip} id={panel_id}")
    send_json(sock, {'type': 'panel_ok', 'panel_id': panel_id})
    with lock:
        srvs = list(serveurs.values())
    for s in srvs:
        try: send_json(sock, {'type': 'new_server', 'session_id': s['session_id'],
                              'nom_pc': s['nom_pc'], 'ip': s['ip']})
        except: pass
    try:
        while True:
            sock.settimeout(60)
            try:
                data = recv_msg(sock)
            except socket.timeout:
                try: send_json(sock, {'type': 'ping'})
                except: break
                continue
            try:
                ctrl = json.loads(data.decode())
                if ctrl.get('type') == 'connect':
                    _connecter_panel_au_serveur(panel_id, ctrl.get('session_id'), sock)
                continue
            except: pass
            with lock:
                target = panels.get(panel_id, {}).get('target_session')
                srv_sock = serveurs.get(target, {}).get('sock') if target else None
            if srv_sock:
                try: send_msg(srv_sock, data)
                except: pass
    except Exception:
        pass
    finally:
        with lock:
            p = panels.pop(panel_id, {})
            t = p.get('target_session')
            if t and t in serveurs: serveurs[t]['panel_sock'] = None
        log(f"[-] Panel déco: {ip}")

def _connecter_panel_au_serveur(panel_id, session_id, panel_sock):
    with lock:
        if session_id not in serveurs:
            try: send_json(panel_sock, {'type': 'error', 'msg': 'Serveur non dispo'})
            except: pass
            return
        serveurs[session_id]['panel_sock'] = panel_sock
        panels[panel_id]['target_session'] = session_id
        nom = serveurs[session_id]['nom_pc']
        srv_sock = serveurs[session_id]['sock']
    log(f"[→] Tunnel: panel {panel_id} ↔ {nom}")
    try: send_json(panel_sock, {'type': 'tunnel_ok', 'session_id': session_id, 'nom_pc': nom})
    except: pass
    try: send_json(srv_sock, {'type': 'panel_connected'})
    except: pass

def _notif_panels(msg):
    with lock: ps = list(panels.values())
    for p in ps:
        try: send_json(p['sock'], msg)
        except: pass

if __name__ == '__main__':
    log(f"RemoteDesk Relais — port {PORT}")
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('0.0.0.0', PORT))
    srv.listen(100)
    log("En attente de connexions...")
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=handle, args=(conn, addr), daemon=True).start()
