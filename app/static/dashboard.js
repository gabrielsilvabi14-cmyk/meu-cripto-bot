'use strict';
const $=id=>document.getElementById(id);
const usd=v=>typeof v==='number'&&Number.isFinite(v)?'US$ '+v.toLocaleString('pt-BR',{minimumFractionDigits:2,maximumFractionDigits:2}):'—';
const mc=v=>typeof v==='number'&&Number.isFinite(v)&&v>0?'US$ '+v.toLocaleString('pt-BR',{maximumFractionDigits:0}):'Indisponível';
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
 const fields=[['MC na entrada',mc(p.entry_market_cap)],['MC atual',mc(p.current_market_cap)],['Variação do preço',pct(p.change_pct)],['Resultado estimado',usd(p.unrealized_pnl_usd)],['Valor da posição',usd(p.estimated_value_usd)],['Stop de referência',p.entry_market_cap?mc(p.entry_market_cap*(1-(p.stop_pct??12)/100))+' (estimado)':'—'],['Stop-loss',pct(-(p.stop_pct??12))],['Meta de lucro',pct(p.target_pct??50)],['Cotação',p.mark_price==null?'⚠️ Indisponível':p.mark_is_stale?'⚠️ Desatualizada':'Atualizada']];
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
  const learning=d.learning||{};
  setValue('learning-mode',learning.mode==='adaptive'?'Filtro adaptativo ativo':'Aprendendo');
  setValue('learning-count',String(learning.trades_analyzed??0)+' / '+String(learning.minimum_trades_for_review??40));
  setValue('learning-threshold','acima de '+String(learning.minimum_score_exclusive??60));
  setValue('learning-explanation',learning.explanation||'Coletando resultados de operações fechadas.');
  const low=learning.low_score_61_69||{},high=learning.high_score_70_plus||{};
  setValue('learning-low',String(low.count??0)+' operações · '+(low.average_return_pct==null?'—':pct(low.average_return_pct))+' médio');
  setValue('learning-high',String(high.count??0)+' operações · '+(high.average_return_pct==null?'—':pct(high.average_return_pct))+' médio');
  const stats=d.signal_stats||{};
  setValue('signal-received',stats.received||0);
  setValue('signal-bought',stats.bought||0);
  setValue('signal-skipped',stats.skipped||0);
  setValue('signal-error',stats.last_error?'Erro: '+stats.last_error:(d.telegram_radar_running?'Radar ativo · aguardando próximo sinal':'Radar Telegram não está ativo'));
  const decisions=$('signal-decisions');decisions.replaceChildren();
  for(const decision of (d.paper_decisions||[]).slice(-12).reverse()){
   const line=node('div','','token-row');
   line.append(node('strong',(decision.result==='comprado'?'🟢 Compra fictícia':'🟠 Não comprado')+' · '+decision.token));
   line.append(node('small',decision.reason||'Sem motivo informado'));
   decisions.append(line);
  }
  if(!decisions.children.length)decisions.textContent='Nenhum sinal processado nesta execução.';
  setValue('connection',m.connected?'GMGN conectada':(m.phase||'Aguardando'));
  setValue('lastcheck',m.last_check?new Date(m.last_check).toLocaleString('pt-BR'):'Aguardando');
  setValue('cycles',m.cycles||0);
  setValue('errors',Object.entries(m.errors||{}).map(([k,v])=>k.toUpperCase()+': '+v).join(' | ')||'Sem erros registrados');
  const settings=d.paper_settings||{};
  if(!document.activeElement?.closest?.('#settings-form')){
   if(typeof settings.operation_usd==='number')$('operation-usd').value=settings.operation_usd;
   if(typeof settings.stop_pct==='number')$('stop-pct').value=settings.stop_pct;
   if(typeof settings.target_pct==='number')$('target-pct').value=settings.target_pct;
  }
  const initial=Number(d.initial_paper_capital_usd);
  const missing=positions.filter(p=>p.estimated_value_usd==null||p.mark_is_stale);
  const equityComplete=missing.length===0&&typeof d.estimated_equity_usd==='number';
  const equity=equityComplete?d.estimated_equity_usd:null;
  setValue('cash',usd(d.cash_usd));setValue('equity',usd(equity));setValue('initial-capital',usd(initial));setValue('status-initial-capital',usd(initial));
  setValue('equity-note',missing.length?('⚠️ '+missing.length+' posição(ões) sem cotação atualizada; patrimônio não confiável'):'Inclui saldo e posições abertas');
  const pnl=equity!==null&&Number.isFinite(initial)?equity-initial:null;
  setValue('pnl',usd(pnl));$('pnl').className='value '+(pnl===null?'':pnl>=0?'positive':'negative');
  setValue('pnl-percent',pnl===null?'Indisponível até atualizar todas as cotações':pct(initial>0?pnl/initial*100:null)+' sobre capital inicial de '+usd(initial));
  setValue('position-count',positions.length);setValue('events',trades.length+' eventos');
  const closed=trades.filter(t=>t.action==='PAPER_SELL');setValue('closed-count',closed.length+' fechamentos');
  const gains=closed.reduce((sum,t)=>sum+Math.max(0,Number(t.realized_pnl_usd)||0),0);
  const losses=closed.reduce((sum,t)=>sum+Math.min(0,Number(t.realized_pnl_usd)||0),0);
  const net=gains+losses;
  setValue('closed-gains',usd(gains));setValue('closed-losses',usd(losses));setValue('closed-net',usd(net));
  $('closed-net').className=net>=0?'positive':'negative';
  setValue('closed-summary-note','Resumo das '+closed.length+' vendas disponíveis no histórico exibido; não inclui posições abertas.');
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

$('settings-form').addEventListener('submit',async e=>{
 e.preventDefault();
 const feedback=$('settings-feedback'),button=$('settings-submit');
 const values={operation_usd:Number($('operation-usd').value),stop_pct:Number($('stop-pct').value),target_pct:Number($('target-pct').value)};
 if(!Number.isFinite(values.operation_usd)||values.operation_usd<1||values.operation_usd>10000||!Number.isFinite(values.stop_pct)||values.stop_pct<0.1||values.stop_pct>95||!Number.isFinite(values.target_pct)||values.target_pct<1||values.target_pct>1000){
  feedback.textContent='Confira os limites dos três campos.';return;
 }
 button.disabled=true;feedback.textContent='Salvando parâmetros...';
 try{
  const r=await fetch('/api/paper/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values)});
  const data=await r.json();if(!r.ok)throw Error(data.error||'Falha ao salvar');
  feedback.textContent='Estratégia salva. Próximas operações usarão os novos valores.';
 }catch(err){feedback.textContent='Erro: '+err.message}
 finally{button.disabled=false}
});
