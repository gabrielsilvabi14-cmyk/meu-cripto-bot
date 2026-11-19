'use strict';
const $=id=>document.getElementById(id);
const usd=v=>typeof v==='number'&&Number.isFinite(v)?'US$ '+v.toLocaleString('pt-BR',{minimumFractionDigits:2,maximumFractionDigits:2}):'—';
const price=v=>typeof v==='number'&&Number.isFinite(v)&&v>0?'US$ '+v.toPrecision(6):'—';
const pct=v=>typeof v==='number'&&Number.isFinite(v)?(v>=0?'+':'')+v.toFixed(2)+'%':'—';
function node(tag,value,cls){const n=document.createElement(tag);n.textContent=String(value??'');if(cls)n.className=cls;return n}
function splitToken(s){const i=String(s||'').indexOf(':');return i<0?['','']:[s.slice(0,i),s.slice(i+1)]}
function gmgnUrl(chain,address){
 if(chain==='eth'&&/^0x[a-fA-F0-9]{40}$/.test(address))return 'https://gmgn.ai/eth/token/'+address;
 if(chain==='sol'&&/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(address))return 'https://gmgn.ai/sol/token/'+address;
 return null;
}
function contractLink(container,chain,address){
 const url=gmgnUrl(chain,address);
 if(!url){container.append(node('small',address||'Contrato não disponível','contract'));return}
 const a=node('a',address,'contract contract-link');a.href=url;a.target='_blank';a.rel='noopener noreferrer';a.title='Abrir contrato na GMGN';container.append(a);
}
function openButton(container,chain,address){
 const url=gmgnUrl(chain,address);if(!url)return;
 const a=node('a','Abrir na GMGN ↗','gmgn-link');a.href=url;a.target='_blank';a.rel='noopener noreferrer';container.append(a);
}
function setValue(id,value){$(id).textContent=value}
function renderToken(container,t){
 const row=node('div','','token-row');row.append(node('strong',(t.symbol||t.name||'Token')+' · '+String(t.chain||'').toUpperCase()));
 contractLink(row,t.chain,t.address);openButton(row,t.chain,t.address);container.append(row);
}
function renderPosition(container,p,meta){
 const [chain,address]=splitToken(p.token),t=meta.get(p.token)||{};
 const card=node('div','','position');
 card.append(node('strong',(p.symbol||t.symbol||p.name||'Token')+' · '+chain.toUpperCase()));
 contractLink(card,chain,address);
 const fields=[['Preço de entrada',price(p.entry_price)],['Preço atual',price(p.mark_price)],['Variação do preço',pct(p.change_pct)],['Resultado estimado',usd(p.unrealized_pnl_usd)],['Valor da posição',usd(p.estimated_value_usd)],['Stop de referência',price(p.stop_price)]];
 const grid=node('div','','position-grid');
 for(const [k,v] of fields){const item=document.createElement('div');item.append(node('small',k),node('b',v, k==='Resultado estimado'&&typeof p.unrealized_pnl_usd==='number'?(p.unrealized_pnl_usd>=0?'positive':'negative'):''));grid.append(item)}
 card.append(grid);openButton(card,chain,address);container.append(card);
}
function renderTrade(container,t,meta){
 const [chain,address]=splitToken(t.token),known=meta.get(t.token)||{};
 const row=node('div','','token-row');
 row.append(node('strong',(t.action==='PAPER_BUY'?'🟢 Compra fictícia':'🔴 Venda fictícia')+' · '+(known.symbol||'Token')+' · '+chain.toUpperCase()));
 row.append(node('small',usd(t.notional_usd??t.net_usd)+(typeof t.realized_pnl_usd==='number'?' · Resultado realizado: '+usd(t.realized_pnl_usd):'')));
 contractLink(row,chain,address);openButton(row,chain,address);container.append(row);
}
async function refresh(){
 try{
  const r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);
  const d=await r.json(),m=d.market||{},tokens=m.tokens||[],positions=d.positions||[],trades=d.trades||[];
  setValue('connection',m.connected?'GMGN conectada':(m.phase||'Aguardando'));
  setValue('lastcheck',m.last_check?new Date(m.last_check).toLocaleString('pt-BR'):'Aguardando');
  setValue('cycles',m.cycles||0);
  setValue('errors',Object.entries(m.errors||{}).map(([k,v])=>k.toUpperCase()+': '+v).join(' | ')||'Sem erros registrados');
  setValue('cash',usd(d.cash_usd));setValue('equity',usd(d.estimated_equity_usd));setValue('initial-capital',usd(d.initial_paper_capital_usd));
  const pnl=typeof d.estimated_equity_usd==='number'?d.estimated_equity_usd-Number(d.initial_paper_capital_usd||100):null;
  setValue('pnl',usd(pnl));$('pnl').className='value '+(pnl===null?'':pnl>=0?'positive':'negative');
  setValue('position-count',positions.length);setValue('events',trades.length+' eventos');
  const closed=trades.filter(t=>t.action==='PAPER_SELL');setValue('closed-count',closed.length+' fechamentos');
  const meta=new Map(tokens.map(t=>[t.chain+':'+t.address,t]));
  const pos=$('positions');pos.replaceChildren();positions.forEach(p=>renderPosition(pos,p,meta));
  if(!positions.length)pos.textContent='Nenhuma posição aberta no momento.';
  const closedBox=$('closed-trades');closedBox.replaceChildren();
  for(const t of closed.slice(-30).reverse()){
    const [chain,address]=splitToken(t.token);const known=meta.get(t.token)||{};const item=node('div','','token-row');
    item.append(node('strong',(known.symbol||'Token')+' · '+chain.toUpperCase()+' · '+(t.fraction===1?'Venda total':'Venda parcial')));
    const result=Number(t.realized_pnl_usd);const gain=Number.isFinite(result)&&result>=0;
    item.append(node('div',(gain?'Lucro: ':'Prejuízo: ')+usd(result)+' ('+(typeof t.realized_pnl_pct==='number'?pct(t.realized_pnl_pct):'percentual indisponível')+')','trade-result '+(gain?'positive':'negative')));
    item.append(node('small','Recebido: '+usd(t.net_usd)+' · Custo da parte vendida: '+usd(t.cost_basis_usd),'trade-meta'));
    if(t.timestamp)item.append(node('small',new Date(t.timestamp).toLocaleString('pt-BR'),'trade-meta'));
    contractLink(item,chain,address);openButton(item,chain,address);closedBox.append(item);
  }
  if(!closed.length)closedBox.textContent='Nenhuma operação fechada até agora.';
  const history=$('trades');history.replaceChildren();trades.slice(-25).reverse().forEach(t=>renderTrade(history,t,meta));
  if(!trades.length)history.textContent='Nenhuma operação simulada.';
  const list=$('tokens');list.replaceChildren();tokens.forEach(t=>renderToken(list,t));
  if(!tokens.length)list.textContent='Nenhum token recebido da GMGN.';
 }catch(e){setValue('connection','Falha ao consultar o painel');setValue('errors','Não foi possível atualizar os dados. Tente recarregar a página.')}
}
refresh();setInterval(refresh,30000);

$('capital-form').addEventListener('submit',async e=>{
 e.preventDefault();
 const input=$('capital-input'),feedback=$('capital-feedback'),button=$('capital-submit');
 const amount=Number(input.value);
 if(!Number.isFinite(amount)||amount<10||amount>100000){feedback.textContent='Digite um valor entre US$ 10 e US$ 100.000.';return}
 if(!window.confirm('Reiniciar a simulação com '+usd(amount)+'? Todas as posições e operações fictícias anteriores serão apagadas.'))return;
 button.disabled=true;feedback.textContent='Reiniciando carteira fictícia...';
 try{
  const r=await fetch('/api/paper/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({capital_usd:amount,confirm_reset:true})});
  const data=await r.json();
  if(!r.ok)throw Error(data.error||'Erro ao reiniciar');
  feedback.textContent='Carteira fictícia reiniciada com '+usd(amount)+'.';
  await refresh();
 }catch(err){feedback.textContent='Não foi possível reiniciar: '+err.message}
 finally{button.disabled=false}
});
