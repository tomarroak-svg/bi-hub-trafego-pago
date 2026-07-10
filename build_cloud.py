"""
build_cloud.py — Build do BI HUB TRÁFEGO PAGO para hospedagem em nuvem.
Derivado do BI Hub Decoração (jul/2026), mantendo APENAS as páginas:
  P1 (Performance Sites Próprios) · P5 (Detalhe por Site) · P6 (CRO).
Puxa os dados pela API REST do Windsor.ai, reconstrói as séries e injeta no
template -> gera index.html. Roda em GitHub Actions; requer o secret WINDSOR_API_KEY.
Regras preservadas do BI original: Meta +12,15% a partir de 01/01/2026; receita
só status PAGO (Faturamento Sisarte); GA4 com account_id como CAMPO (não
parâmetro) e sem teto de linhas; carimbo em Horário de Brasília.
IMPORTANTE: as páginas P2/P3/P4 (Receita & Produção, Gestão de Caixa,
Indicadores Estratégicos) NÃO existem neste BI — nenhum dado financeiro delas
é puxado nem injetado no HTML publicado (o público deste painel é outro).
"""
import os, sys, json, time, urllib.parse, urllib.request, unicodedata
from datetime import date, datetime, timedelta, timezone
from collections import defaultdict

def _nfc(s):
    """Normaliza string para NFC (caractere composto). Defende contra mismatches
    entre o source Python (que pode estar em NFD) e a resposta do Windsor (NFC)."""
    return unicodedata.normalize('NFC', s) if isinstance(s, str) else s

def _nfc_rows(rows):
    """Normaliza as CHAVES de cada dict da lista para NFC. Conteúdo (valores) intocado."""
    out=[]
    for r in rows:
        out.append({_nfc(k): v for k, v in r.items()})
    return out

def K(s):
    """Use K('conciliação') em qualquer .get() de campos com acento — garante NFC."""
    return _nfc(s)

API_KEY = os.environ.get("WINDSOR_API_KEY", "").strip()
if not API_KEY:
    sys.exit("ERRO: defina a variável de ambiente WINDSOR_API_KEY (secret do GitHub).")

BASE   = os.path.dirname(os.path.abspath(__file__))
BRT    = timezone(timedelta(hours=-3))
NOW    = datetime.now(BRT).strftime('%d/%m/%Y %H:%M')
START  = date(2024,1,1)
END    = datetime.now(BRT).date()
ENDS   = END.isoformat()

# ---- mapeamentos (iguais à versão validada) ----
# Sites PRÓPRIOS (aparecem em P1, P5 e P6) + empresas PARCEIRAS (aparecem SÓ em P5/P6,
# nunca na P1 — decisão do Rafael: parceiros não podem consolidar com sites próprios).
SITE_FB={'383182840215364':'Moderna','990772362573645':'Moderna','175769079':'DePoster','867793234181618':'DePoster','1413252628867222':'Empório','958957112622663':'Empório',
         # parceiros (Meta Ads)
         '324593358846160':'Mondessin','1154532475018030':'Coor','329992743452082':'Bruna Baldone','1008952206460036':_nfc('Estúdio Baru')}
# Google Ads: Windsor devolve o customer id com hífens (XXX-XXX-XXXX). Cadastro os dois
# formatos (com e sem hífen) por segurança, pois não dá pra validar o formato exato sem a chave.
SITE_GADS={'423-641-1454':'Moderna','402-184-9198':'DePoster','135-806-4700':'DePoster','543-984-2956':'Empório',
           # parceiros com Google Ads (Bruna Baldone e Estúdio Baru NÃO têm conta Google Ads)
           '505-083-9339':'Mondessin','5050839339':'Mondessin','564-082-3041':'Coor','5640823041':'Coor'}
SITE_GA4={'363632980':'Moderna','286274006':'DePoster','358759049':'Empório',
          # parceiros (GA4)
          '352420460':'Mondessin','396666896':'Coor','430724112':'Bruna Baldone','302059339':_nfc('Estúdio Baru')}
PREFIX={'MDR':'Moderna','DEP':'DePoster','EMP':'Empório'}
META_FROM=date(2026,1,1); META=1.1215
OWN={'MDR','EMP','DEP'}

# ---- empresas PARCEIRAS (P5/P6 apenas) ----
PARTNER_SITES=['Mondessin','Coor','Bruna Baldone',_nfc('Estúdio Baru')]
# Empresas SEM conta Google Ads → front-end mostra aviso na tabela de Campanhas Google
# e o canal Google entra como zero nos indicadores do topo (custoGoogle já fica 0 por não
# estar em SITE_GADS).
NO_GOOGLE_ADS=['Bruna Baldone',_nfc('Estúdio Baru')]
# Faturamento Sisarte: parceiros são identificados pela COLUNA 'parceiro' (não pelo prefixo
# do número do pedido, como os sites próprios). Chaves normalizadas p/ NFC minúsculo.
PARTNER_FAT={_nfc(k):v for k,v in {
    'marina mondessin':'Mondessin',
    'coor':'Coor',
    'bruna baldone | arte em quadros':'Bruna Baldone',
    'estúdio baru':_nfc('Estúdio Baru'),
}.items()}
# BASE BI PRODUÇÃO E CUSTOS FÁBRICA (col AF PARCEIRO): código completo do parceiro.
PARTNER_PROD={'MON':'Mondessin','COOR':'Coor','BBALD':'Bruna Baldone','BARU':_nfc('Estúdio Baru')}

# contas googlesheets (Windsor account_id = id_da_planilha + sufixo)
SHEET_FATURAMENTO="1N8o99FbyhWn70mqEDad_S_AI_Lzjynv-0rzxoc0eWOI-1289411442"

def brnum(v):
    if v in (None,''): return 0.0
    s=str(v).strip().replace(' ','')
    if ',' in s: s=s.replace('.','').replace(',','.')
    elif s.count('.')>1:
        i=s.rfind('.'); s=s[:i].replace('.','')+'.'+s[i+1:]
    try: return float(s)
    except: return 0.0

def pdate(v):
    if not v: return None
    s=str(v).split(' ')[0].strip()
    for fmt in ('%d/%m/%Y','%d/%m/%y'):
        try: return datetime.strptime(s,fmt).date()
        except: pass
    return None

import re as _re

def windsor(connector, fields, account_id=None, date_from=None, date_to=None, max_rows=None, retries=3):
    """Chama a API REST do Windsor e devolve lista de dicts (envelope {'data':[...]})."""
    params={"api_key":API_KEY, "fields":",".join(fields), "_renderer":"json"}
    if account_id: params["account_id"]=account_id
    if date_from:  params["date_from"]=date_from
    if date_to:    params["date_to"]=date_to
    if max_rows:   params["_max_rows"]=str(max_rows)
    url=f"https://connectors.windsor.ai/{connector}?"+urllib.parse.urlencode(params)
    last=None
    for att in range(retries):
        try:
            req=urllib.request.Request(url, headers={"User-Agent":"Windsor/1.0 (BI-Hub-Trafego-Pago)"})
            with urllib.request.urlopen(req, timeout=300) as r:
                payload=json.loads(r.read().decode("utf-8"))
            if isinstance(payload, dict) and "error" in payload:
                raise RuntimeError(f"{connector}: {payload['error']}")
            rows = payload.get("data") if isinstance(payload, dict) else payload
            return rows or []
        except Exception as e:
            last=e; time.sleep(3*(att+1))
    raise RuntimeError(f"Falha ao puxar {connector} (account {account_id}): {last}")

print(f"[{NOW} BRT] Puxando dados do Windsor.ai (até {ENDS})…")
fb   = windsor("facebook",        ["date","spend","account_id"], date_from=START.isoformat(), date_to=ENDS)
gads = windsor("google_ads",      ["date","cost","account_id"],  date_from=START.isoformat(), date_to=ENDS)
ga4  = windsor("googleanalytics4",["date","sessions","account_id"], date_from=START.isoformat(), date_to=ENDS)
fat  = windsor("googlesheets", ["número_do_pedido","data","valor_total_do_pedido","status","parceiro"], account_id=SHEET_FATURAMENTO)
print(f"  linhas: fb={len(fb)} gads={len(gads)} ga4={len(ga4)} fat={len(fat)}")

# Normaliza nomes de campo para NFC (corrige bug onde 'conciliação' / 'descrição' etc.
# vinham com mismatch entre source code Python e resposta do Windsor REST).
fat  = _nfc_rows(fat)


# ===================== PÁGINA 1 =====================
cost=defaultdict(lambda:defaultdict(float)); sess=defaultdict(lambda:defaultdict(int))
rev=defaultdict(lambda:defaultdict(float)); cnt=defaultdict(lambda:defaultdict(int)); orders={}
for r in fb:
    aid=str(r.get('account_id'))
    if aid in SITE_FB:
        d=r.get('date');
        if not d: continue
        sp=brnum(r.get('spend'))
        try:
            if date.fromisoformat(d)>=META_FROM: sp*=META
        except: pass
        cost[SITE_FB[aid]][d]+=sp
for r in gads:
    s=SITE_GADS.get(str(r.get('account_id')))
    if s and r.get('date'): cost[s][r['date']]+=brnum(r.get('cost'))
for r in ga4:
    s=SITE_GA4.get(str(r.get('account_id')))
    if s and r.get('date'):
        try: sess[s][r['date']]+=int(float(r.get('sessions') or 0))
        except: pass
for r in fat:
    no=r.get('número_do_pedido'); dt=r.get('data')
    if not no or not dt: continue
    st=r.get('status')
    if st is not None and str(st).strip()!='' and str(st).strip().upper()!='PAGO': continue
    s=PREFIX.get(str(no).split('.')[0])
    if not s:
        # empresas PARCEIRAS: identificadas pela coluna 'parceiro' (não têm prefixo MDR/DEP/EMP)
        s=PARTNER_FAT.get(_nfc(str(r.get('parceiro') or '')).strip().lower())
    if not s: continue
    d=pdate(dt)
    if not d: continue
    orders[no]=(s,d,brnum(r.get('valor_total_do_pedido')))
for no,(s,d,val) in orders.items():
    ds=d.isoformat(); rev[s][ds]+=val; cnt[s][ds]+=1

dates=[]; d=START
while d<=END: dates.append(d.isoformat()); d+=timedelta(days=1)
sites={}
for s in ['Moderna','DePoster','Empório']:
    sites[s]=dict(custo=[round(cost[s].get(x,0),2) for x in dates],
                  receita=[round(rev[s].get(x,0),2) for x in dates],
                  vendas=[cnt[s].get(x,0) for x in dates],
                  sessoes=[sess[s].get(x,0) for x in dates])
p1=dict(meta=dict(updated_at=NOW,start=START.isoformat(),end=ENDS,metaTax=META,metaTaxFrom=META_FROM.isoformat(),tiktok_incluido=False),
        dates=dates, sites=sites)

# ---- índice de datas (usado por P5/P6) ----
idx={x:i for i,x in enumerate(dates)}; N=len(dates)



# ===================== PÁGINA 5 — Detalhe por Site Próprio =====================
# Página com SELETOR DE EMPRESA no topo: tudo é exibido de uma empresa por vez.
# Estrutura de dados:
#   - sites[site]: séries DIÁRIAS (2024→hoje) p/ os 12 KPIs + tabela diária. O front-end
#     reagrega pelo período selecionado (mesmo motor da Página 1).
#   - campanhas / anúncios / instagram: preenchidos em blocos próprios mais abaixo
#     (cada métrica sensível a período fica em arrays diários esparsos p/ reagregar no front).
# Custo Meta leva +12,15% a partir de 01/01/2026 (igual P1); Google sem ajuste.
SITE_IG = {
    '17841450821474791': 'Moderna',
    '17841402125563401': 'DePoster',
    '17841418118323735': 'Empório',
    # parceiros (Instagram)
    '17841440336824405': 'Mondessin',
    '17841406023923194': 'Coor',
    '17841452639482858': 'Bruna Baldone',
    '17841450666776212': _nfc('Estúdio Baru'),
}
P5_SITES = ['Moderna', 'DePoster', 'Empório'] + PARTNER_SITES

# --- séries diárias por site: custo Meta e Google SEPARADOS (p/ tabela diária e KPIs) ---
p5_custoMeta   = defaultdict(lambda: defaultdict(float))
p5_custoGoogle = defaultdict(lambda: defaultdict(float))
for r in fb:
    s = SITE_FB.get(str(r.get('account_id')))
    if not s: continue
    d = r.get('date')
    if not d: continue
    sp = brnum(r.get('spend'))
    try:
        if date.fromisoformat(d) >= META_FROM: sp *= META
    except Exception: pass
    p5_custoMeta[s][d] += sp
for r in gads:
    s = SITE_GADS.get(str(r.get('account_id')))
    if s and r.get('date'):
        p5_custoGoogle[s][r['date']] += brnum(r.get('cost'))

p5_sites = {}
for s in P5_SITES:
    p5_sites[s] = dict(
        custoMeta   = [round(p5_custoMeta[s].get(x, 0), 2)   for x in dates],
        custoGoogle = [round(p5_custoGoogle[s].get(x, 0), 2) for x in dates],
        receita     = [round(rev[s].get(x, 0), 2)            for x in dates],   # mesma fonte da P1 (Faturamento PAGO)
        vendas      = [cnt[s].get(x, 0)                      for x in dates],   # transações P1
        sessoes     = [sess[s].get(x, 0)                     for x in dates],   # GA4 P1
    )

# ---- pulls da P5 (campanha/anúncio/Instagram). Defensivos: falha vira [] e não quebra o build ----
def windsor_safe(*a, **k):
    try:
        return windsor(*a, **k)
    except Exception as e:
        print(f"  [P5][AVISO] pull falhou ({a[0] if a else '?'}): {e}")
        return []

def detect_field(connector, account_id, base_fields, candidates, date_from, date_to):
    """Tenta cada candidato individualmente; devolve o 1º slug que retorna dado não-vazio."""
    for c in candidates:
        try:
            rows = windsor(connector, base_fields + [c], account_id=account_id,
                           date_from=date_from, date_to=date_to, max_rows=5, retries=1)
            if any((c in r and r.get(c) not in (None, '')) for r in rows):
                return c
        except Exception:
            pass
    return None

# Janelas CURTAS para não explodir o volume (campanha/anúncio são pesados em nível detalhado).
P5_CAMP_FROM = (END - timedelta(days=120)).isoformat()   # campanhas: últimos ~4 meses
P5_AD_FROM   = (END - timedelta(days=60)).isoformat()     # anúncios: últimos ~2 meses (mais leve)
P5_IG_FROM   = (END - timedelta(days=540)).isoformat()    # seguidores/alcance IG
P5_POST_FROM = (END - timedelta(days=365)).isoformat()    # posts IG do último ano

# Autodetecção dos 2 slugs irregulares: "Page View" do Meta (p/ CPS) e valor da demografia IG.
PV = "actions_landing_page_view"   # confirmado (CPS Meta = custo ÷ Page Views)
# Demografia: dimensão (nome do bucket) + valor (qtd de seguidores). Slugs confirmados no
# data-preview do Windsor. ATENÇÃO: o IG aceita só UMA dimensão por chamada -> uma por loop.
DEMO_FIELDS = [("age",    "audience_age_name",    "audience_age_size"),
               ("gender", "audience_gender_name", "audience_gender_size"),
               ("city",   "city",                 "audience_city_size")]
print(f"  [P5] pageViewField={PV} (fixo)")

# ---------- CAMPANHAS META ----------
_fb_camp_fields = ["date", "account_id", "campaign", "campaign_status", "effective_status",
                   "spend", "impressions", "clicks", "reach", "actions_purchase",
                   "action_values_purchase", "actions_add_to_cart", "actions_initiate_checkout",
                   "link_clicks"] + ([PV] if PV else [])
fb_camp = windsor_safe("facebook", _fb_camp_fields, date_from=P5_CAMP_FROM, date_to=ENDS, retries=2)
# colunas Meta (ordem fixa p/ o front): spend, imp, clicks, reach, purch, rev, cart, chk, pv
camp_meta = {s: {} for s in P5_SITES}
for r in fb_camp:
    s = SITE_FB.get(str(r.get('account_id')))
    if not s: continue
    name = (r.get('campaign') or '').strip()
    if not name: continue
    d = r.get('date'); i = idx.get(d)
    if i is None: continue
    sp = brnum(r.get('spend'))
    try:
        if date.fromisoformat(d) >= META_FROM: sp *= META
    except Exception: pass
    rec = camp_meta[s].get(name)
    if rec is None:
        rec = camp_meta[s][name] = {'status': '', 'agg': defaultdict(lambda: [0.0]*9)}
    st = (str(r.get('effective_status') or r.get('campaign_status') or '')).strip()
    if st: rec['status'] = st
    a = rec['agg'][i]
    a[0]+=sp; a[1]+=brnum(r.get('impressions')); a[2]+=brnum(r.get('clicks')); a[3]+=brnum(r.get('reach'))
    a[4]+=brnum(r.get('actions_purchase')); a[5]+=brnum(r.get('action_values_purchase'))
    a[6]+=brnum(r.get('actions_add_to_cart')); a[7]+=brnum(r.get('actions_initiate_checkout'))
    a[8]+=brnum(r.get(PV)) if PV else 0.0

# ---------- CAMPANHAS GOOGLE ----------
g_camp = windsor_safe("google_ads",
    ["date", "account_id", "campaign", "campaign_status", "cost", "impressions",
     "clicks", "conversions", "conversions_value"], date_from=P5_CAMP_FROM, date_to=ENDS, retries=2)
# colunas Google: cost, imp, clicks, conv, convVal, gsess
camp_google = {s: {} for s in P5_SITES}
for r in g_camp:
    s = SITE_GADS.get(str(r.get('account_id')))
    if not s: continue
    name = (r.get('campaign') or '').strip()
    if not name: continue
    i = idx.get(r.get('date'))
    if i is None: continue
    rec = camp_google[s].get(name)
    if rec is None:
        rec = camp_google[s][name] = {'status': '', 'agg': defaultdict(lambda: [0.0]*6)}
    st = (str(r.get('campaign_status') or '')).strip()
    if st: rec['status'] = st
    a = rec['agg'][i]
    a[0]+=brnum(r.get('cost')); a[1]+=brnum(r.get('impressions')); a[2]+=brnum(r.get('clicks'))
    a[3]+=brnum(r.get('conversions')); a[4]+=brnum(r.get('conversions_value'))

# sessões GA4 por campanha (p/ CPS das campanhas Google) — casa pelo nome da campanha
ga4_camp = windsor_safe("googleanalytics4", ["date", "account_id", "campaign", "sessions"],
                        date_from=P5_CAMP_FROM, date_to=ENDS, retries=2)
for r in ga4_camp:
    s = SITE_GA4.get(str(r.get('account_id')))
    if not s: continue
    name = (r.get('campaign') or '').strip()
    i = idx.get(r.get('date'))
    if i is None or not name: continue
    rec = camp_google[s].get(name)
    if rec is not None:
        rec['agg'][i][5] += brnum(r.get('sessions'))

def _serialize_camps(site_map):
    out = []
    for name, rec in site_map.items():
        rows = [[i] + [round(x, 2) for x in rec['agg'][i]] for i in sorted(rec['agg'].keys())]
        out.append({'name': name, 'status': rec['status'], 'rows': rows})
    return out
campaigns = {s: {'Meta': _serialize_camps(camp_meta[s]),
                 'Google': _serialize_camps(camp_google[s])} for s in P5_SITES}

# ---------- ANÚNCIOS META ----------
# Anúncios: por conta, em DOIS níveis. Os campos "exóticos" (vídeo 25%, engajamento de página)
# parecem derrubar a query de anúncio (HTTP 500); então tenta completo e, se falhar, refaz só
# com o essencial (que inclui o criativo/thumbnail). Assim o painel popula mesmo degradado.
_ad_full = ["date", "account_id", "campaign", "ad_id", "ad_name", "thumbnail_url", "spend",
            "impressions", "clicks", "reach", "actions_purchase", "action_values_purchase",
            "link_clicks", "video_p25_watched_actions_video_view", "actions_page_engagement"]
_ad_core = ["date", "account_id", "campaign", "ad_id", "ad_name", "thumbnail_url", "spend",
            "impressions", "clicks", "reach", "actions_purchase", "action_values_purchase"]
fb_ad = []
_ad_fallbacks = 0
for _acc in SITE_FB:
    rows = windsor_safe("facebook", _ad_full, account_id=_acc, date_from=P5_AD_FROM, date_to=ENDS, retries=1)
    if not rows:
        rows = windsor_safe("facebook", _ad_core, account_id=_acc, date_from=P5_AD_FROM, date_to=ENDS, retries=2)
        if rows: _ad_fallbacks += 1
    fb_ad += rows
print(f"  [P5][DBG] anúncios: {len(fb_ad)} linhas (fallback p/ campos essenciais em {_ad_fallbacks} conta(s))")
# colunas anúncio: spend, imp, clicks, reach, purch, rev, linkclicks, video25, pageEng
ads_meta = {s: {} for s in P5_SITES}
for r in fb_ad:
    s = SITE_FB.get(str(r.get('account_id')))
    if not s: continue
    aid = str(r.get('ad_id') or '').strip()
    if not aid: continue
    d = r.get('date'); i = idx.get(d)
    if i is None: continue
    sp = brnum(r.get('spend'))
    try:
        if date.fromisoformat(d) >= META_FROM: sp *= META
    except Exception: pass
    rec = ads_meta[s].get(aid)
    if rec is None:
        rec = ads_meta[s][aid] = {'ad_name': '', 'campaign': '', 'thumb': '', 'agg': defaultdict(lambda: [0.0]*9)}
    nm = str(r.get('ad_name') or '').strip()
    if nm: rec['ad_name'] = nm
    cp = str(r.get('campaign') or '').strip()
    if cp: rec['campaign'] = cp
    th = str(r.get('thumbnail_url') or '').strip()
    if th: rec['thumb'] = th
    a = rec['agg'][i]
    a[0]+=sp; a[1]+=brnum(r.get('impressions')); a[2]+=brnum(r.get('clicks')); a[3]+=brnum(r.get('reach'))
    a[4]+=brnum(r.get('actions_purchase')); a[5]+=brnum(r.get('action_values_purchase'))
    a[6]+=brnum(r.get('link_clicks')); a[7]+=brnum(r.get('video_p25_watched_actions_video_view'))
    a[8]+=brnum(r.get('actions_page_engagement'))

ads = {}
for s in P5_SITES:
    lst = []
    for aid, rec in ads_meta[s].items():
        rows = [[i] + [round(x, 2) for x in rec['agg'][i]] for i in sorted(rec['agg'].keys())]
        lst.append({'id': aid, 'name': rec['ad_name'], 'campaign': rec['campaign'],
                    'thumb': rec['thumb'], 'rows': rows})
    ads[s] = lst

# ---------- INSTAGRAM (chamadas ÚNICAS p/ todas as contas, filtradas por account_id) ----------
ig_foll = {s: {} for s in P5_SITES}; ig_reach = {s: {} for s in P5_SITES}; ig_new = {s: {} for s in P5_SITES}
# Pull 1: alcance + total de seguidores (este combo FUNCIONA — não adicionar follower_count aqui,
# senão o IG retorna vazio por causa da regra "uma dimensão por chamada").
for r in windsor_safe("instagram", ["date", "account_id", "followers_count", "reach"],
                      date_from=P5_IG_FROM, date_to=ENDS, retries=2):
    s = SITE_IG.get(str(r.get('account_id')))
    if not s: continue
    i = idx.get(r.get('date'))
    if i is None: continue
    fc = brnum(r.get('followers_count'))
    if fc: ig_foll[s][i] = fc
    ig_reach[s][i] = ig_reach[s].get(i, 0) + brnum(r.get('reach'))
# Pull 2 (SEPARADO): novos seguidores LÍQUIDOS por dia ("New followers" = follower_count),
# p/ reconstruir o total de fim de cada mês (o Windsor só dá o total ATUAL via followers_count).
for r in windsor_safe("instagram", ["date", "account_id", "follower_count"],
                      date_from=P5_IG_FROM, date_to=ENDS, retries=2):
    s = SITE_IG.get(str(r.get('account_id')))
    if not s: continue
    i = idx.get(r.get('date'))
    if i is None: continue
    ig_new[s][i] = ig_new[s].get(i, 0) + brnum(r.get('follower_count'))

# demografia: POR CONTA, UMA dimensão por chamada (limite do IG), vitalícia (sem janela)
ig_demo = {s: {'age': {}, 'gender': {}, 'city': {}} for s in P5_SITES}
for acc, s in SITE_IG.items():
    for dim_key, name_f, val_f in DEMO_FIELDS:
        for r in windsor_safe("instagram", [name_f, val_f], account_id=acc,
                              date_from=None, date_to=None, max_rows=400, retries=2):
            k = str(r.get(name_f) or '').strip()
            v = brnum(r.get(val_f))
            if not k or v <= 0: continue
            ig_demo[s][dim_key][k] = ig_demo[s][dim_key].get(k, 0) + v

# posts: POR CONTA, com os campos de MÍDIA (media_*) — os de conta (reach/likes) vinham 0.
# Métricas são por mídia (lifetime). Janela de ~150 dias seleciona os posts recentes.
ig_posts = {s: {} for s in P5_SITES}
_post_from = (END - timedelta(days=380)).isoformat()   # ~12 meses, p/ Interações/Engajamento mensais cobrirem o ano
for acc, s in SITE_IG.items():
    for r in windsor_safe("instagram",
            ["media_id", "media_product_type", "timestamp", "media_reach",
             "media_like_count", "media_comments_count", "media_saved",
             "media_url", "media_thumbnail_url"],
            account_id=acc, date_from=_post_from, date_to=ENDS, max_rows=2000, retries=2):
        mid = str(r.get('media_id') or '').strip()
        if not mid: continue
        # Reels: media_url é o vídeo (a tag <img> falha) → usar media_thumbnail_url. Imagens/carrossel: media_url.
        thumb = (str(r.get('media_thumbnail_url') or '').strip() or str(r.get('media_url') or '').strip())
        vals = dict(reach=brnum(r.get('media_reach')), likes=brnum(r.get('media_like_count')),
                    comments=brnum(r.get('media_comments_count')), saves=brnum(r.get('media_saved')),
                    shares=0.0, ti=0.0)
        cur = ig_posts[s].get(mid)
        if cur is None:
            ig_posts[s][mid] = dict(fmt=str(r.get('media_product_type') or '').strip(),
                ts=str(r.get('timestamp') or '').strip()[:10], thumb=thumb, **vals)
        else:
            for k in ('reach', 'likes', 'comments', 'saves'): cur[k] = max(cur[k], vals[k])
            if not cur['thumb']: cur['thumb'] = thumb
            if not cur['ts']:    cur['ts']    = str(r.get('timestamp') or '').strip()[:10]
            if not cur['fmt']:   cur['fmt']   = str(r.get('media_product_type') or '').strip()
# diagnóstico: confirma se métricas dos posts vêm preenchidas (sem expor URL)
_mp = list(ig_posts.get('Moderna', {}).values())
if _mp:
    _e = max(_mp, key=lambda p: p['ts'])
    print(f"  [P5][DBG] post Moderna mais recente: ts={_e['ts']} fmt={_e['fmt']} reach={_e['reach']} likes={_e['likes']} ti={_e['ti']} (de {len(_mp)} posts)")
print(f"  [P5][DBG] demografia Moderna: gender={len(ig_demo['Moderna']['gender'])} age={len(ig_demo['Moderna']['age'])} city={len(ig_demo['Moderna']['city'])}")

instagram = {}
for s in P5_SITES:
    _keys = sorted(set(list(ig_reach[s].keys()) + list(ig_foll[s].keys()) + list(ig_new[s].keys())))
    daily = [[i, round(ig_reach[s].get(i, 0), 0), round(ig_foll[s].get(i, 0), 0), round(ig_new[s].get(i, 0), 0)]
             for i in _keys]
    demo = {k: sorted(([kk, round(vv, 0)] for kk, vv in ig_demo[s][k].items() if vv > 0),
                      key=lambda x: -x[1])[:12] for k in ('age', 'gender', 'city')}
    posts = sorted(ig_posts[s].values(), key=lambda p: p['ts'], reverse=True)
    instagram[s] = dict(daily=daily, demo=demo, posts=posts)
print("  [P5][DBG] ig.daily linhas: " + ", ".join(f"{s}={len(instagram[s]['daily'])}" for s in P5_SITES) +
      " | ig_new Moderna dias=" + str(len(ig_new['Moderna'])))

p5 = dict(
    meta = dict(updated_at=NOW, start=START.isoformat(), end=ENDS,
                metaTax=META, metaTaxFrom=META_FROM.isoformat(),
                sites=P5_SITES, noGoogleAds=NO_GOOGLE_ADS, campFrom=P5_CAMP_FROM, adFrom=P5_AD_FROM,
                pageViewField=PV, demoFields=[d[0] for d in DEMO_FIELDS],
                campCols=['spend','imp','clicks','reach','purch','rev','cart','chk','pv'],
                gcampCols=['cost','imp','clicks','conv','convVal','gsess'],
                adCols=['spend','imp','clicks','reach','purch','rev','linkclicks','video25','pageEng']),
    dates = dates,
    sites = p5_sites,
    campaigns = campaigns,
    ads = ads,
    instagram = instagram,
)

# ===================== PÁGINA 6 — CRO (comportamento do cliente no site, via GA4) =====================
# Seletor de empresa + seletor de datas GLOBAL (mesma base diária do resto do BI).
# Janela LIMITADA aos últimos 18 meses (GA4 mais leve). Seção 1 é diária (alinhada a `dates`/`idx`);
# Seções 2-4 são mensais (bucket 'YYYY-MM') → o cliente agrega no período escolhido.
P6_SITES = ['Moderna', 'DePoster', 'Empório'] + PARTNER_SITES
_p6m = END.month - 18; _p6y = END.year
while _p6m <= 0: _p6m += 12; _p6y -= 1
P6_FROM = max(date(_p6y, _p6m, 1), START).isoformat()   # primeiro dia, 18 meses atrás

_GA4_CACHE = {}
def ga4_dated(acc, dims, mets, label, max_rows=None):
    """Breakdown GA4 com 'date' (18 meses). IMPORTANTE: o Windsor IGNORA account_id como
    parâmetro no GA4 (devolve dados combinados das 3 propriedades). Então puxamos UMA vez por
    (dims,mets) com 'account_id' como CAMPO (padrão P1/P5), cacheamos, e filtramos por conta no
    Python. Sem teto de linhas (max_rows=None) → não trunca as datas recentes das dimensões pesadas."""
    key = (tuple(dims), tuple(mets))
    if key not in _GA4_CACHE:
        try:
            _GA4_CACHE[key] = windsor("googleanalytics4", ['date'] + dims + mets + ['account_id'],
                                      date_from=P6_FROM, date_to=ENDS, max_rows=max_rows, retries=2)
        except Exception as e:
            print(f"  [P6][AVISO] GA4 {label} falhou: {e}")
            _GA4_CACHE[key] = []
    a = str(acc)
    return [r for r in _GA4_CACHE[key] if str(r.get('account_id')) == a]

def _daily_dim(rows, dimkey, topn):
    """{dimValue: {idxStr: sess}} esparso; se topn, mantém top-N por total e agrupa resto em __outros__."""
    bydate = defaultdict(lambda: defaultdict(float)); total = defaultdict(float)
    for r in rows:
        i = idx.get((str(r.get('date') or ''))[:10])
        if i is None: continue
        sv = brnum(r.get('sessions'))
        if sv <= 0: continue
        k = (str(r.get(dimkey)) if r.get(dimkey) not in (None, '') else '(não definido)').strip() or '(não definido)'
        bydate[k][i] += sv; total[k] += sv
    keep = [k for k, _ in sorted(total.items(), key=lambda x: -x[1])[:topn]] if topn else list(total.keys())
    keepset = set(keep); out = {}
    for k in keep:
        out[k] = {str(i): int(round(v)) for i, v in bydate[k].items() if v > 0}
    if topn:
        outros = defaultdict(float)
        for k, dd in bydate.items():
            if k in keepset: continue
            for i, v in dd.items(): outros[i] += v
        if outros:
            out['__outros__'] = {str(i): int(round(v)) for i, v in outros.items() if v > 0}
    return out

def _daily_ga(rows):
    """{faixa: {idxStr: [fem, masc]}}"""
    order = ['18-24', '25-34', '35-44', '45-54', '55-64', '65+']; out = {}
    for r in rows:
        i = idx.get((str(r.get('date') or ''))[:10])
        if i is None: continue
        a = (str(r.get('age') or '')).strip(); g = (str(r.get('gender') or '')).strip().lower()
        sv = brnum(r.get('sessions'))
        if sv <= 0 or a not in order or g not in ('male', 'female'): continue
        cell = out.setdefault(a, {}).setdefault(str(i), [0.0, 0.0])
        cell[0 if g == 'female' else 1] += sv
    for a in out:
        for k in out[a]:
            out[a][k] = [int(round(out[a][k][0])), int(round(out[a][k][1]))]
    return out

def _daily_pages(rows, topn=60):
    """{página: {idxStr: [sess, bounce*sess]}}  (rejeição reconstruída ponderada no cliente)"""
    bydate = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0])); total = defaultdict(float)
    for r in rows:
        i = idx.get((str(r.get('date') or ''))[:10])
        if i is None: continue
        sv = brnum(r.get('sessions'))
        if sv <= 0: continue
        p = (str(r.get('page_path') or '/')).strip() or '/'
        br = brnum(r.get('bounce_rate'))
        cell = bydate[p][i]; cell[0] += sv; cell[1] += br * sv; total[p] += sv
    keep = [p for p, _ in sorted(total.items(), key=lambda x: -x[1])[:topn]]; out = {}
    for p in keep:
        out[p] = {str(i): [int(round(v[0])), round(v[1], 2)] for i, v in bydate[p].items() if v[0] > 0}
    return out

# ---- Seções 2-4: agregação MENSAL ('YYYY-MM'); o cliente soma os meses do período ----
def _monthly(rows, keydim, mets, topn, rankidx):
    """{chave: {'YYYY-MM': [soma de cada métrica]}}; mantém top-N por soma de mets[rankidx]."""
    nm = len(mets)
    bym = defaultdict(lambda: defaultdict(lambda: [0.0] * nm)); total = defaultdict(float)
    for r in rows:
        mk = (str(r.get('date') or ''))[:7]
        if len(mk) != 7: continue
        k = (str(r.get(keydim)) if r.get(keydim) not in (None, '') else '(não definido)').strip() or '(não definido)'
        vals = [brnum(r.get(m)) for m in mets]
        if not any(vals): continue
        c = bym[k][mk]
        for j in range(nm): c[j] += vals[j]
        total[k] += vals[rankidx]
    keep = [k for k, _ in sorted(total.items(), key=lambda x: -x[1])[:topn]] if topn else list(total.keys())
    return {k: {mk: [round(x, 2) for x in v] for mk, v in bym[k].items()} for k in keep}

def _monthly_items(rows):
    """Produtos: devolve (porNome, porSku) com [comprados, vistos, receita] por mês.
    Rótulo SEMPRE texto legível (item_name); a tabela SKU concatena a variante (material/tamanho)."""
    def build(keyfn, topn):
        bym = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0.0])); total = defaultdict(float)
        for r in rows:
            mk = (str(r.get('date') or ''))[:7]
            if len(mk) != 7: continue
            k = keyfn(r)
            if not k: continue
            pu = brnum(r.get('items_purchased')); vi = brnum(r.get('items_viewed')); rev = brnum(r.get('item_revenue'))
            if pu <= 0 and vi <= 0 and rev <= 0: continue
            c = bym[k][mk]; c[0] += pu; c[1] += vi; c[2] += rev; total[k] += rev
        keep = [k for k, _ in sorted(total.items(), key=lambda x: -x[1])[:topn]]
        return {k: {mk: [round(v[0], 2), round(v[1], 2), round(v[2], 2)] for mk, v in bym[k].items()} for k in keep}
    def _name(r):
        return (str(r.get('item_name') or '')).strip()
    def _sku(r):
        n = (str(r.get('item_name') or '')).strip()
        if not n: return ''
        v = (str(r.get('item_variant') or '')).strip()
        return n + (' · ' + v if v and v.lower() not in ('(not set)', '(sem valor)', 'none') else '')
    return build(_name, 80), build(_sku, 120)

def _monthly_time(rows):
    """De [date,hour]+[sessions,transactions,purchase_revenue] deriva hora/dia-da-semana/dia-do-mês + heatmap."""
    hour = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0.0]))
    dow  = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0.0]))
    dom  = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0.0]))
    heat = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))  # dowIdx -> hh -> mês -> sessões
    for r in rows:
        ds = (str(r.get('date') or ''))[:10]
        if len(ds) != 10: continue
        try: y, m, dd = int(ds[:4]), int(ds[5:7]), int(ds[8:10])
        except Exception: continue
        mk = ds[:7]; hh = str(int(brnum(r.get('hour')))).zfill(2)
        se = brnum(r.get('sessions')); tr = brnum(r.get('transactions')); rv = brnum(r.get('purchase_revenue'))
        ch = hour[hh][mk]; ch[0] += se; ch[1] += tr; ch[2] += rv
        try: wd = date(y, m, dd).weekday()
        except Exception: continue
        cd = dow[str(wd)][mk]; cd[0] += se; cd[1] += tr; cd[2] += rv
        cm = dom[str(dd).zfill(2)][mk]; cm[0] += se; cm[1] += tr; cm[2] += rv
        heat[str(wd)][hh][mk] += se
    td = lambda b: {k: {mk: [round(x, 2) for x in v] for mk, v in dd.items()} for k, dd in b.items()}
    ht = {wd: {hh: {mk: round(v, 2) for mk, v in mm.items()} for hh, mm in hh2.items()} for wd, hh2 in heat.items()}
    return td(hour), td(dow), td(dom), ht

def _parse_month(v):
    """Extrai 'YYYY-MM' de DATA PEDIDO (aceita ISO, dd/mm/aaaa, dd-mm-aaaa, dd/mm/aa)."""
    if v in (None, ''): return None
    s = str(v).strip()
    m = _re.match(r'^(\d{4})-(\d{2})', s)
    if m: return f"{m.group(1)}-{m.group(2)}"
    m = _re.match(r'^(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})', s)
    if m:
        y = m.group(3); y = ('20' + y) if len(y) == 2 else y
        try: return f"{int(y):04d}-{int(m.group(2)):02d}"
        except Exception: return None
    return None

def _prod_total():
    """Planilha BASE BI PRODUÇÃO por MÊS (segue o seletor de datas). Defensivo."""
    SHEET = "1p4ms0yuZ6KYj9WbJbnhwBGSrIPCAwB_jjkoJTYFWQk0-0"
    try:
        rows = windsor("googlesheets",
            ["pedido", "parceiro", "data_pedido", "nome_produto", "sku", "material", "tamanho", "cor",
             "estado_entrega", "cidade_entrega"], account_id=SHEET, max_rows=300000, retries=2)
    except Exception as e:
        print(f"  [P6][AVISO] planilha produção indisponível (conectou no Windsor?): {e}"); return {}
    if not rows:
        print("  [P6][AVISO] planilha produção vazia"); return {}
    # Sites próprios (MDR/DEP/EMP, 3 chars) + parceiros (códigos de tamanho variável: MON, COOR,
    # BBALD, BARU). Casa o código COMPLETO primeiro; cai p/ prefixo de 3 chars só p/ os próprios.
    pmap = {'MDR': 'Moderna', 'DEP': 'DePoster', 'EMP': 'Empório'}
    pmap.update(PARTNER_PROD)
    def _prod_site(v):
        u = _nfc(str(v or '')).strip().upper()
        if not u: return None
        if u in pmap: return pmap[u]           # código completo (MON, COOR, BBALD, BARU, MDR...)
        if u[:3] in pmap: return pmap[u[:3]]   # próprios legados que venham com sufixo
        for code, name in pmap.items():        # parceiro com sufixo no valor (ex.: 'BBALD - ...')
            if u.startswith(code): return name
        return None
    # field -> (coluna, topn por total)
    fields = (('material', 'material', None), ('tamanho', 'tamanho', None), ('cor', 'cor', None),
              ('estado', 'estado_entrega', 40), ('cidade', 'cidade_entrega', 80), ('produto', 'nome_produto', 120))
    acc = {s: {f[0]: defaultdict(lambda: defaultdict(int)) for f in fields} for s in P6_SITES}  # field->valor->mês->qtd
    n = dated = 0
    for r in rows:
        if 'MANUAL' in (str(r.get('pedido') or '')).upper(): continue   # exclui *.MANUAL
        site = _prod_site(r.get('parceiro'))
        if not site: continue
        mk = _parse_month(r.get('data_pedido'))
        if not mk: continue   # sem data válida → fora do período
        n += 1; dated += 1; d = acc[site]
        for field, col, _ in fields:
            v = (str(r.get(col)) if r.get(col) not in (None, '') else '').strip()
            if v: d[field][v][mk] += 1
    print(f"  [P6][DBG] produção: {n} itens com data válida (slug data_pedido)")
    out = {}
    for s in P6_SITES:
        out[s] = {}
        for field, _, topn in fields:
            tot = {val: sum(mm.values()) for val, mm in acc[s][field].items()}
            keep = sorted(tot, key=lambda k: -tot[k])[:topn] if topn else list(tot.keys())
            out[s][field] = {val: {mk: [cnt] for mk, cnt in acc[s][field][val].items()} for val in keep}
    return out

prod_data = _prod_total()
p6_sites = {}
for _ga4_acc, _site in SITE_GA4.items():
    rec = {}
    # --- Seção 1: análise das sessões (diário) ---
    rec['campaign'] = _daily_dim(ga4_dated(_ga4_acc, ['campaign'], ['sessions'], 'campaign'), 'campaign', 25)
    rec['url']      = _daily_dim(ga4_dated(_ga4_acc, ['landing_page'], ['sessions'], 'url'), 'landing_page', 25)
    rec['interest'] = _daily_dim(ga4_dated(_ga4_acc, ['branding_interest'], ['sessions'], 'interest'), 'branding_interest', 25)
    rec['channel']  = _daily_dim(ga4_dated(_ga4_acc, ['session_default_channel_group'], ['sessions'], 'channel'), 'session_default_channel_group', None)
    rec['state']    = _daily_dim(ga4_dated(_ga4_acc, ['region'], ['sessions'], 'state'), 'region', None)
    rec['city']     = _daily_dim(ga4_dated(_ga4_acc, ['city'], ['sessions'], 'city'), 'city', 40)
    rec['genderAge']= _daily_ga(ga4_dated(_ga4_acc, ['gender', 'age'], ['sessions'], 'gender_age'))
    rec['pages']    = _daily_pages(ga4_dated(_ga4_acc, ['page_path'], ['sessions', 'bounce_rate'], 'pages'), 60)
    # --- Seção 2: produtos (mensal, item-level) ---
    _items = ga4_dated(_ga4_acc, ['item_name', 'item_variant'], ['items_purchased', 'items_viewed', 'item_revenue'], 'items')
    rec['prodName'], rec['prodSku'] = _monthly_items(_items)
    # --- Seção 3: vendas (mensal) ---
    rec['kw']     = _monthly(ga4_dated(_ga4_acc, ['session_google_ads_keyword'], ['sessions', 'transactions', 'purchase_revenue'], 'kw'),
                             'session_google_ads_keyword', ['sessions', 'transactions', 'purchase_revenue'], 60, 0)
    rec['search'] = _monthly(ga4_dated(_ga4_acc, ['search_term'], ['sessions', 'transactions'], 'search'),
                             'search_term', ['sessions', 'transactions'], 60, 0)
    rec['coupon'] = _monthly(ga4_dated(_ga4_acc, ['order_coupon'], ['transactions', 'purchase_revenue'], 'coupon'),
                             'order_coupon', ['transactions', 'purchase_revenue'], 40, 1)
    rec['hour'], rec['dow'], rec['dom'], rec['heat'] = _monthly_time(
        ga4_dated(_ga4_acc, ['hour'], ['sessions', 'transactions', 'purchase_revenue'], 'time'))
    rec['prod'] = prod_data.get(_site, {})
    # --- Seção 4: conversão por canal (mensal) ---
    rec['chanConv'] = _monthly(ga4_dated(_ga4_acc, ['session_default_channel_group'], ['sessions', 'transactions', 'purchase_revenue'], 'chanconv'),
                               'session_default_channel_group', ['sessions', 'transactions', 'purchase_revenue'], None, 0)
    p6_sites[_site] = rec

p6 = dict(
    meta = dict(updated_at=NOW, sites=P6_SITES, start=P6_FROM, end=ENDS, dow=['Segunda','Terça','Quarta','Quinta','Sexta','Sábado','Domingo']),
    dates = dates,
    sites = p6_sites,
)
print("[P6][DBG] dims/empresa: " + " | ".join(
    f"{s}: camp={len(p6_sites[s]['campaign'])} cidade={len(p6_sites[s]['city'])} "
    f"prodNome={len(p6_sites[s]['prodName'])} prodSku={len(p6_sites[s]['prodSku'])} "
    f"kw={len(p6_sites[s]['kw'])} busca={len(p6_sites[s]['search'])} cupom={len(p6_sites[s]['coupon'])} "
    f"hora={len(p6_sites[s]['hour'])} prod={'sim' if p6_sites[s]['prod'] else 'NÃO'}" for s in P6_SITES))

# ===================== INJEÇÃO NO TEMPLATE =====================
TPL=open(os.path.join(BASE,'bi-hub-template.html'),encoding='utf-8').read()
assert TPL.count('/*__DAILY__*/null')==1, "template sem placeholder P1 (__DAILY__)"

def _inject(d1, d5, d6, view=None):
    """Monta um HTML a partir do template. d1/d5/d6 = JSON string (ou 'null').
    view = dict {'partner': <site>} pra variante restrita de parceiro (injeta __VIEW__)."""
    h=TPL.replace('/*__DAILY__*/null','/*__DAILY__*/'+d1,1)
    # P5 e P6 — injeção CONDICIONAL (padrão da casa): permite subir pipeline antes do HTML.
    if h.count('/*__DAILY5__*/null')==1:
        h=h.replace('/*__DAILY5__*/null','/*__DAILY5__*/'+d5,1)
    if h.count('/*__DAILY6__*/null')==1:
        h=h.replace('/*__DAILY6__*/null','/*__DAILY6__*/'+d6,1)
    if view is not None and h.count('/*__VIEW__*/null')==1:
        h=h.replace('/*__VIEW__*/null','/*__VIEW__*/'+json.dumps(view,ensure_ascii=False,separators=(',',':')),1)
    return h

d1=json.dumps(p1, ensure_ascii=False, separators=(',',':'))
d5=json.dumps(p5, ensure_ascii=False, separators=(',',':'))
d6=json.dumps(p6, ensure_ascii=False, separators=(',',':'))
open(os.path.join(BASE,'index.html'),'w',encoding='utf-8').write(_inject(d1,d5,d6))

# ===================== VARIANTES POR PARCEIRO (acesso restrito) =====================
# Cada parceiro ganha um HTML próprio em p/<slug>/index.html contendo APENAS os dados
# do site dele (P5/P6 filtrados). P1 NÃO é injetado (fica null) — mesmo princípio de
# segurança do spin-off: dado que o parceiro não pode ver, não existe no arquivo.
# Quem decide qual arquivo servir é o _worker.js (Pages Function) pelo e-mail do
# Cloudflare Access. Slugs casam com o mapa PARTNER_EMAILS do _worker.js.
PARTNER_SLUGS={'Mondessin':'mondessin','Coor':'coor','Bruna Baldone':'bruna-baldone',_nfc('Estúdio Baru'):'estudio-baru'}
assert set(PARTNER_SLUGS)==set(PARTNER_SITES), "PARTNER_SLUGS fora de sincronia com PARTNER_SITES"
for _psite,_slug in PARTNER_SLUGS.items():
    _m5=dict(p5['meta']); _m5['sites']=[_psite]; _m5['noGoogleAds']=[x for x in NO_GOOGLE_ADS if x==_psite]
    _p5v=dict(meta=_m5, dates=dates,
              sites={_psite:p5['sites'][_psite]}, campaigns={_psite:p5['campaigns'][_psite]},
              ads={_psite:p5['ads'][_psite]}, instagram={_psite:p5['instagram'][_psite]})
    _m6=dict(p6['meta']); _m6['sites']=[_psite]
    _p6v=dict(meta=_m6, dates=dates, sites={_psite:p6['sites'][_psite]})
    _d5v=json.dumps(_p5v,ensure_ascii=False,separators=(',',':'))
    _d6v=json.dumps(_p6v,ensure_ascii=False,separators=(',',':'))
    # QA de vazamento: nenhum outro site pode aparecer como chave nos JSONs da variante.
    for _other in (set(P5_SITES)-{_psite}):
        _k=json.dumps(_other,ensure_ascii=False)+':{'
        assert _k not in _d5v and _k not in _d6v, f"VAZAMENTO: dados de {_other} na variante {_slug}"
    _dir=os.path.join(BASE,'p',_slug); os.makedirs(_dir,exist_ok=True)
    _hv=_inject('null', _d5v, _d6v, view=dict(partner=_psite))
    open(os.path.join(_dir,'index.html'),'w',encoding='utf-8').write(_hv)
    print(f"  [PARCEIRO] p/{_slug}/index.html gerado ({len(_hv)} chars) — só {_psite}")

# ===================== QA (aborta se algo absurdo) =====================
print(f"[QA] pedidos P1={len(orders)} (ref >5000) | dias na base={len(dates)}")
print("[QA] P5 base (últimos 30d): " + " | ".join(
    f"{s}: Meta~{sum(p5_sites[s]['custoMeta'][-30:]):.0f} Google~{sum(p5_sites[s]['custoGoogle'][-30:]):.0f} "
    f"receita~{sum(p5_sites[s]['receita'][-30:]):.0f} sess~{sum(p5_sites[s]['sessoes'][-30:])}"
    for s in P5_SITES))
# QA PARCEIROS (não aborta — só valida o mapeamento; parceiros podem ter pouco/nenhum dado ainda)
_pord=defaultdict(int); _prec=defaultdict(float)
for _no,(_s,_d,_v) in orders.items():
    if _s in PARTNER_SITES: _pord[_s]+=1; _prec[_s]+=_v
print("[QA] Parceiros (histórico completo): " + " | ".join(
    f"{s}: pedidos={_pord.get(s,0)} receita~{_prec.get(s,0):.0f} "
    f"Meta~{sum(p5_sites[s]['custoMeta']):.0f} Google~{sum(p5_sites[s]['custoGoogle']):.0f} "
    f"sess~{sum(p5_sites[s]['sessoes'])} gAds={'não' if s in NO_GOOGLE_ADS else 'sim'} "
    f"prod={'sim' if p6_sites[s]['prod'] else 'NÃO'}" for s in PARTNER_SITES))
if len(orders)<5000:
    sys.exit("ERRO QA: pedidos P1 fora do esperado — provável truncamento ou mudança de schema. NÃO publicar.")
_p6_camps=sum(len(p6_sites[s]['campaign']) for s in P6_SITES)
if _p6_camps==0:
    sys.exit("ERRO QA: P6 (CRO) sem nenhuma campanha GA4 — provável falha no pull. NÃO publicar.")
print(f"OK build em nuvem: index.html gerado ({os.path.getsize(os.path.join(BASE,'index.html'))} bytes) @ {NOW} BRT")
