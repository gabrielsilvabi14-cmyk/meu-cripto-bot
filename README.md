# GMGN Trader 1.0

Plataforma pessoal de paper trading para memecoins Solana (Pump.fun) e Ethereum.

## Estado
Fase 1: site privado inicial, servidor HTTP e arquitetura. **Sem integração GMGN ativa, sem Telegram, sem operações reais ou fictícias automáticas.**

## Executar localmente
1. Python 3.11+
2. `pip install -r requirements.txt`
3. Defina `TRADER_PASSWORD` (senha forte) e `SESSION_SECRET` (segredo aleatório com 32+ caracteres) como variáveis de ambiente.
4. `uvicorn app.main:app --host 0.0.0.0 --port 10000`
5. Acesse `http://localhost:10000`.

Para publicar, use HTTPS obrigatório, defina as variáveis apenas no provedor e configure um banco persistente antes de habilitar operações simuladas. O repositório é público: **nunca grave credenciais em arquivos versionados**.

## Configurações iniciais
- Redes: SOL Pump.fun e ETH
- Capital simulado de referência: US$100
- Risco planejado por operação: 2% do patrimônio, não garantido
- Stop-loss de referência: 12%, sujeito a taxas e execução
- Execução real: proibida nesta versão

## Próximos módulos
`app/integrations/gmgn.py` (consulta read-only), `app/agents/` (sinais e auditoria), `app/trading/` (paper engine e risco), `app/storage/` (PostgreSQL), `app/web/` (dashboard).

Nenhum dado financeiro apresentado no painel inicial é resultado de operações.
