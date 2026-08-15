import requests, time
IP = "181.191.109.137"
res = {}
for p in [8000, 80, 443]:
    try:
        r = requests.get("https://check-host.net/check-tcp?host=%s:%d&max_nodes=5" % (IP, p),
                         headers={"Accept": "application/json"}, timeout=25).json()
        res[p] = {"rid": r.get("request_id"), "link": r.get("permanent_link")}
        print("porta %-4d -> relatorio (enviar ao TI): %s" % (p, r.get("permanent_link")))
    except Exception as e:
        res[p] = {"rid": None, "link": None}
        print("porta %-4d -> erro ao iniciar teste: %s" % (p, str(e)[:80]))
    time.sleep(1)
print("\naguardando os nos externos testarem (~16s)...")
time.sleep(16)
def fmt(val):
    if val is None: return "sem resposta"
    if isinstance(val, list):
        out = []
        for it in val:
            if isinstance(it, dict):
                if "address" in it and "time" in it: out.append("CONECTOU (%.0f ms)" % (it["time"]*1000))
                elif "error" in it: out.append("FALHOU: " + str(it["error"]))
                else: out.append(str(it))
            else: out.append(str(it))
        return " | ".join(out)
    return str(val)
for p in [8000, 80, 443]:
    rid = res[p]["rid"]
    print("\n=== PORTA %d  (%s:%d) ===" % (p, IP, p))
    if not rid:
        continue
    try:
        rr = requests.get("https://check-host.net/check-result/%s" % rid, headers={"Accept": "application/json"}, timeout=25).json()
        for node, val in rr.items():
            print("   %-22s %s" % (node, fmt(val)))
    except Exception as e:
        print("   erro ao ler resultado:", str(e)[:80])
