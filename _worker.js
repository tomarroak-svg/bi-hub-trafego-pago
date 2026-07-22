// =====================================================================
// _worker.js — Pages Function do BI HUB TRÁFEGO PAGO (Advanced mode)
//
// Roteia CADA requisição pelo e-mail autenticado no Cloudflare Access:
//   equipe (TEAM_EMAILS)      → index.html (painel completo)
//   parceiro (PARTNER_EMAILS) → p/<slug>/index.html (SÓ os dados do site dele;
//                               os dados dos outros sites nem existem no arquivo)
//   qualquer outro caso       → 403
//
// O caminho pedido na URL é IGNORADO de propósito: não importa o que a pessoa
// digite (/, /p/mondessin/, etc.), ela sempre recebe o arquivo do PERFIL dela.
// Isso elimina acesso direto às variantes de outros parceiros.
//
// Deploy: este arquivo vai na RAIZ do diretório publicado pelo
// `wrangler pages deploy dist` (o refresh.yml já copia). O Pages detecta o
// nome `_worker.js` e passa a executar tudo por ele.
//
// MANUTENÇÃO (2 lugares ao adicionar/remover alguém):
//   1. Cloudflare Zero Trust → Access → Applications → política do app
//      (quem consegue LOGAR).
//   2. Os mapas abaixo (o que a pessoa VÊ depois de logada).
//   Regra de ouro: e-mail no Access mas fora dos mapas = 403 (nunca vaza).
// =====================================================================

// ---- EQUIPE (vê o painel completo) ----
const TEAM_EMAILS = new Set([
  'alicercarpes@gmail.com',
  'babitoncic@gmail.com',
  'hivantoncic@gmail.com',
  'rpitangui@gmail.com',
  'tomarroak@gmail.com',
  'trafegodeposter@gmail.com',
].map(e => e.toLowerCase()));

// ---- PARCEIROS: e-mail → slug da variante (p/<slug>/index.html) ----
// Slugs válidos (definidos no build_cloud.py, PARTNER_SLUGS):
//   'mondessin' · 'coor' · 'bruna-baldone' · 'estudio-baru'
const PARTNER_EMAILS = {
  'marina.m.amaral@gmail.com':      'mondessin',
  'rogerioarrudapinto@gmail.com':   'coor',
  'brunabaldone@gmail.com':         'bruna-baldone',
  'contato@estudiobaru.com.br':     'estudio-baru',
  'hubdecor.sp@gmail.com':          'estudio-baru',  // espelho p/ teste interno — mesma visão da Estúdio Baru
};

// Opcional: liberar um DOMÍNIO inteiro pra um parceiro (ex.: toda @mondessin.com.br)
const PARTNER_DOMAINS = {
  // 'mondessin.com.br': 'mondessin',
};

// ---- Verificação de assinatura do JWT do Access (hardening) ----
// Preencher ACCESS_TEAM_DOMAIN liga a verificação criptográfica do token
// (recomendado). Ex.: 'suaequipe.cloudflareaccess.com' — está em
// Zero Trust → Settings → Custom Pages ("Team domain").
// ACCESS_AUD (opcional) = Application Audience (AUD) Tag do app, em
// Zero Trust → Access → Applications → (app) → Overview.
// Vazios: o worker só decodifica o JWT (que o Access injeta e sobrescreve
// em toda requisição autenticada) — funciona, mas sem a prova criptográfica.
const ACCESS_TEAM_DOMAIN = '';
const ACCESS_AUD = '';

// =====================================================================

function _b64u(s) {
  s = s.replace(/-/g, '+').replace(/_/g, '/');
  while (s.length % 4) s += '=';
  const bin = atob(s);
  const u = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
  return u;
}
const _json = (u8) => JSON.parse(new TextDecoder().decode(u8));

let _keys = null, _keysAt = 0;
async function _accessKeys() {
  if (_keys && Date.now() - _keysAt < 3600e3) return _keys;
  const r = await fetch(`https://${ACCESS_TEAM_DOMAIN}/cdn-cgi/access/certs`);
  if (!r.ok) throw new Error('certs_' + r.status);
  const j = await r.json();
  const out = {};
  for (const k of (j.keys || [])) {
    out[k.kid] = await crypto.subtle.importKey(
      'jwk', k, { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' }, false, ['verify']);
  }
  _keys = out; _keysAt = Date.now();
  return _keys;
}

async function emailFromAccess(request) {
  const jwt = request.headers.get('Cf-Access-Jwt-Assertion');
  if (!jwt) return null;
  const parts = jwt.split('.');
  if (parts.length !== 3) return null;
  let header, payload;
  try { header = _json(_b64u(parts[0])); payload = _json(_b64u(parts[1])); }
  catch (_) { return null; }
  const now = Math.floor(Date.now() / 1000);
  if (payload.exp && now > payload.exp) return null;
  if (ACCESS_TEAM_DOMAIN) {
    if (payload.iss !== `https://${ACCESS_TEAM_DOMAIN}`) return null;
    if (ACCESS_AUD) {
      const aud = Array.isArray(payload.aud) ? payload.aud : [payload.aud];
      if (!aud.includes(ACCESS_AUD)) return null;
    }
    try {
      const keys = await _accessKeys();
      const key = keys[header.kid];
      if (!key) return null;
      const ok = await crypto.subtle.verify(
        'RSASSA-PKCS1-v1_5', key,
        _b64u(parts[2]), new TextEncoder().encode(parts[0] + '.' + parts[1]));
      if (!ok) return null;
    } catch (_) { return null; }
  }
  const email = String(payload.email || '').toLowerCase().trim();
  return email || null;
}

function deny(msg) {
  const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  return new Response(
    `<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Acesso restrito — BI Tráfego Pago</title>
<style>body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0e1116;color:#e8eaed;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
.card{max-width:460px;padding:36px;background:#161b23;border:1px solid #232a35;border-radius:14px;text-align:center}
h1{font-size:19px;margin:0 0 10px}p{font-size:14px;line-height:1.55;color:#9aa4b2;margin:0}</style></head>
<body><div class="card"><h1>Acesso restrito</h1><p>${esc(msg)}</p></div></body></html>`,
    { status: 403, headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' } });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const email = await emailFromAccess(request);
    if (!email) {
      return deny('Sessão de login não encontrada. Acesse o painel pelo endereço oficial e faça o login pelo Cloudflare Access.');
    }
    if (TEAM_EMAILS.has(email)) {
      return env.ASSETS.fetch(new Request(url.origin + '/' + url.search, request));
    }
    const slug = PARTNER_EMAILS[email] || PARTNER_DOMAINS[email.split('@')[1]] || null;
    if (slug) {
      return env.ASSETS.fetch(new Request(url.origin + '/p/' + slug + '/' + url.search, request));
    }
    return deny(`O e-mail ${email} está autenticado, mas ainda não tem um painel associado. Fale com a Hub Decoração para liberar o acesso.`);
  }
};
