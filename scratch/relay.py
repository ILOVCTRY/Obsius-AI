import socket, threading, sys
def pipe(a,b):
    try:
        while True:
            d=a.recv(65536)
            if not d: break
            b.sendall(d)
    except Exception: pass
    finally:
        for s in (a,b):
            try: s.shutdown(socket.SHUT_RDWR)
            except Exception: pass
def handle(c):
    try:
        u=socket.create_connection(("127.0.0.1",7890), timeout=10)
    except Exception:
        c.close(); return
    threading.Thread(target=pipe,args=(c,u),daemon=True).start()
    threading.Thread(target=pipe,args=(u,c),daemon=True).start()
s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
s.bind(("0.0.0.0",7891)); s.listen(64)
print("relay 0.0.0.0:7891 -> 127.0.0.1:7890", flush=True)
while True:
    c,_=s.accept()
    threading.Thread(target=handle,args=(c,),daemon=True).start()
