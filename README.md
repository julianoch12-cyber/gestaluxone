# gestaluxone

Sistema de Escrutínio e Contagem de Dados em Tempo Real com:

- recepção e validação de dados por múltiplos pontos;
- processamento automático (classificação por bucket);
- painel em tempo real via SSE;
- segmentação por regiões;
- acessos por nível de permissão;
- histórico e rastreabilidade de operações.

## Executar

```bash
python3 app.py
```

Aceda a `http://localhost:8000`.

## Perfis de teste

- `admin` / `admin123`
- `supervisor` / `super123`
- `operador` / `oper123`
- `viewer` / `view123`

## API principal

- `POST /api/login`
- `POST /api/data` (role `operator`+)
- `GET /api/dashboard` (role `viewer`+)
- `GET /api/history` (role `supervisor`+)
- `GET /api/events` (stream SSE para updates do dashboard)
