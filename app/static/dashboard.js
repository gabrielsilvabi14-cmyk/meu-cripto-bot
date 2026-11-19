const $=id=>document.getElementById(id);
const usd=v=>typeof v==='number'&&Number.isFinite(v)?'US$ '+v.toLocaleString('pt-BR',{minimumFractionDigits:2,maximumFractionDigits:2}):'Sem cotação';
const price=v=>typeof v==='number'&&v>0?'US$ '+v.toPrecision(6):'—';
const pct=v=>typeof v==='number'?(v>=0?'+':'')+v.toFixed(2)+'%':'—';
function text(tag,value,cls){const n=document.createElement(tag);n.textContent=value;if(cls)n.className=cls;return n}
function gmgnLink(chain,address){if(!['sol','eth'].includes(chain)||!address||!/^[a-zA-Z0-9]{30,128}$/.test(address))return null;return 'https://gmgn.ai/'+chain+'/token/'+encodeURIComponent(address)}
function addLink(row,chain,address){const url=gmgnLink(chain,address);if(!url)return;const a=text('a','Abrir contrato na GMGN ↗','gmgn-link');a.href=url;a.target='_blank';a.rel='noopener noreferrer';row.append(a)}
function tokenId(s){const i=String(s||'').indexOf(':');return i<0?['','']:[s.slice(0,i),s.slice(i+1)]}
function addToken(container,t){const row=document.createElement('div');row.className='token-row';row.append(text('strong',(t.symbol||t.name||'Token')+' · '+String(t.chain||'').toUpperCase()));row.append(text('small',t.address||'','contract'));addLink(row,t.chain,t.address);container.append(row)}
async function refresh(){try{
const r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);
const d=await r.json(),m=d.market||{},tokens=m.tokens||[];
$('connection').textContent=m.connected?'Conectado':(m.phase||'Aguardando');
$('lastcheck').textContent=m.last_check?new Date(m.last_check).toLocaleString('pt-BR'):'Ainda não consultado';
$('cycles').textContent=m.cycles||0;
$('errors').textContent=Object.entries(m.errors||{}).map(([k,v])=>k.toUpperCase()+': '+v).join(' | ')||'Sem erros registrados';
$('events').textContent=(d.trades||[]).length;
$('cash').textContent=usd(d.cash_usd);$('equity').textContent=usd(d.estimated_equity_usd);
const tokensEl=$('tokens');tokensEl.replaceChildren();tokens.forEach(t=>addToken(tokensEl,t));if(!tokens.length)tokensEl.textContent='Nenhum token recebido até agora.';
const meta=new Map(tokens.map(t=>[t.chain+':'+t.address,t]));
const pos=$('positions');pos.replaceChildren();
for(const p of d.positions||[]){const [chain,address]=tokenId(p.token);const t=meta.get(p.token)||{};const card=document.createElement('div');card.className='position';
card.append(text('strong',(p.symbol||t.symbol||'Token')+' · '+chain.toUpperCase()));
card.append(text('small',address,'contract'));
const fields=[['Entrada',price(p.entry_price)],['Preço atual',price(p.mark_price)],['Variação',pct(p.change_pct)],['Lucro/prejuízo estimado',usd(p.unrealized_pnl_usd)],['Valor da posição',usd(p.estimated_value_usd)],['Stop de referência',price(p.stop_price)]];
const grid=document.createElement('div');grid.className='position-grid';for(const [k,v] of fields){const item=document.createElement('div');item.append(text('small',k),text('b',v));grid.append(item)}card.append(grid);addLink(card,chain,address);pos.append(card)}
if(!pos.children.length)pos.textContent='Nenhuma posição aberta.';
const trades=$('trades');trades.replaceChildren();for(const t of (d.trades||[]).slice(-20).reverse()){const [chain,address]=tokenId(t.token);const metaToken=meta.get(t.token)||{};const row=document.createElement('div');row.className='token-row';row.append(text('strong',(t.action==='PAPER_BUY'?'Compra fictícia':'Venda fictícia')+' · '+(metaToken.symbol||'Token')+' · '+chain.toUpperCase()));row.append(text('small',usd(t.notional_usd??t.net_usd)+(typeof t.realized_pnl_usd==='number'?' · Resultado: '+usd(t.realized_pnl_usd):'')));row.append(text('small',address,'contract'));addLink(row,chain,address);trades.append(row)}
if(!trades.children.length)trades.textContent='Nenhuma operação simulada.';
}catch(e){$('connection').textContent='Falha ao atualizar painel'}}
refresh();setInterval(refresh,30000);
