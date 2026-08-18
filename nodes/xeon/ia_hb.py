#!/usr/bin/env python3
"""Heartbeat da IA (.126) -> VPS /api/ia/heartbeat: saúde + métricas da máquina/GPU (p/ o card 'Xeon-IA')."""
import os, json, subprocess, urllib.request, socket, time
env = {}
for ln in open('/home/corexia/corexia-ia/.env'):
    if '=' in ln and not ln.strip().startswith('#'):
        k, v = ln.strip().split('=', 1); env[k] = v.strip().strip('"').strip("'")
secret = env.get('WEBHOOK_SECRET', '')
base = env.get('WEBHOOK_URL', 'https://grupocorexia.com.br/webhookAlertas').rsplit('/', 1)[0]
def active(s):
    return subprocess.run(['systemctl', 'is-active', s], capture_output=True, text=True).stdout.strip()
def cpu_snap():
    with open('/proc/stat') as f:
        p = [int(x) for x in f.readline().split()[1:9]]
    return sum(p), p[3] + p[4]
def net_snap():
    d = {}
    try:
        with open('/proc/net/dev') as f:
            for ln in f.readlines()[2:]:
                name, _, rest = ln.partition(':')
                name = name.strip()
                if name == 'lo' or name.startswith(('veth', 'docker', 'br-', 'virbr', 'tap')):
                    continue
                p = rest.split()
                d[name] = (int(p[0]), int(p[8]))
    except Exception:
        pass
    return d
t1, i1 = cpu_snap(); _n1 = net_snap(); time.sleep(1); t2, i2 = cpu_snap(); _n2 = net_snap()
dt = t2 - t1; di = i2 - i1; cpu = round(100*(dt-di)/dt) if dt > 0 else 0
m = {}
for ln in open('/proc/meminfo'):
    k, v = ln.split(':', 1); m[k.strip()] = int(v.strip().split()[0])
mt, ma = m.get('MemTotal', 0), m.get('MemAvailable', 0)
s = os.statvfs('/'); tot = s.f_blocks*s.f_frsize; usd = tot - s.f_bavail*s.f_frsize
def gpus():
    try:
        out = subprocess.run(['nvidia-smi', '--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu',
                              '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=15).stdout.strip()
        r = []
        for ln in out.splitlines():
            p = [x.strip() for x in ln.split(',')]
            r.append({'idx': int(p[0]), 'nome': p[1], 'uso_pct': int(p[2]), 'decoder_pct': 0,
                      'vram_usada_mb': int(p[3]), 'vram_total_mb': int(p[4]), 'temp_c': int(p[5])})
        return r
    except Exception:
        return []
g = gpus()
xeon = {'cpu_pct': cpu, 'load': float(open('/proc/loadavg').read().split()[0]), 'nucleos': os.cpu_count(),
        'mem': {'total_gb': round(mt/1048576, 1), 'usado_gb': round((mt-ma)/1048576, 1), 'pct': round(100*(mt-ma)/mt, 1) if mt else 0},
        'disco': {'total_tb': round(tot/1e12, 2), 'usado_tb': round(usd/1e12, 2), 'pct': round(100*usd/tot, 1) if tot else 0},
        'gpus': g}
_ifs = []; _rxt = 0.0; _txt = 0.0
for _nm, _v2 in _n2.items():
    if _nm in _n1:
        _rx = max(0, _v2[0] - _n1[_nm][0]) * 8 / 1e6
        _tx = max(0, _v2[1] - _n1[_nm][1]) * 8 / 1e6
        _rxt += _rx; _txt += _tx
        _ifs.append({'iface': _nm, 'rx_mbps': round(_rx, 2), 'tx_mbps': round(_tx, 2)})
xeon['net'] = {'rx_mbps': round(_rxt, 2), 'tx_mbps': round(_txt, 2), 'ifaces': _ifs}
checks = []; problemas = []; overall = 'ok'
for i in (0, 1):
    st = active('vigia_nvdec@%d' % i); ok = st == 'active'
    checks.append({'check': 'Detector IA GPU%d' % i, 'status': 'ok' if ok else 'critico', 'detalhe': st})
    if not ok:
        overall = 'critico'; problemas.append({'titulo': 'Detector de IA %d inativo' % i, 'detalhe': st, 'sev': 'critico'})
if not g:
    overall = 'critico'; problemas.append({'titulo': 'GPU nao detectada', 'detalhe': 'nvidia-smi sem saida', 'sev': 'critico'})
for gg in g:
    checks.append({'check': 'GPU%d %s' % (gg['idx'], gg['nome']),
                   'status': 'ok' if gg['temp_c'] < 90 else 'alto',
                   'detalhe': '%dC | util %d%% | %d/%d MiB' % (gg['temp_c'], gg['uso_pct'], gg['vram_usada_mb'], gg['vram_total_mb'])})
    if gg['temp_c'] >= 90:
        overall = 'critico'; problemas.append({'titulo': 'GPU %d quente' % gg['idx'], 'detalhe': '%dC' % gg['temp_c'], 'sev': 'alto'})
payload = {'secret': secret, 'overall': overall, 'checks': checks, 'problemas': problemas, 'xeon': xeon, 'host': socket.gethostname()}
req = urllib.request.Request(base + '/api/ia/heartbeat', data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
try:
    r = urllib.request.urlopen(req, timeout=15); print('ia hb', r.status, '| gpus', len(g), '| overall', overall, '| cpu', cpu, '%')
except Exception as e:
    print('ia hb FALHOU', str(e)[:90])
