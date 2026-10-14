# NEC4 CE Tracker — MVP

Protótipo em Streamlit para o planejamento de atrasos em contratos **NEC4 ECC**. Ele segue o fluxo do planejador:
**fato → tracker classificado → assunto que vira atraso → fragnet → impacto no cronograma → narrativa do fechamento mensal**.

1. **Tracker**: registro de cada fato (e-mail, CEMAR, relatório, ata), classificado por **pacote** (5-8 grandes pacotes do gerente), **pacote de trabalho**, **local (CH)** e **assunto**. É possível filtrar, editar a classificação e ligar o assunto às atividades do P6, com sugestões automáticas por palavra-chave e estaca.
2. **Fragnet & narrative**: para o assunto escolhido, gera o fragnet (atividades com início, fim e lógica FS), liga às atividades do P6, calcula quais atividades e marcos o atraso empurra e redige a narrativa do programa mensal (cl. 32). Exporta Excel no layout de importação do P6 e Word.
3. **Ingest documents**: lê PDF, Word ou e-mail e propõe pacote, assunto, local e atividades impactadas, depois inclui no tracker.
4. **CE dashboard**, **Timeline** e **Draft notification**: controle dos prazos de 8 semanas (cl. 61.3) e minuta de notificação de evento compensatório.
5. **EW scan**: early warnings sobre cliente ou terceiros sem CE correspondente (cruzado com o registro de CEs do P6).
6. **NEC4 rules**: prazos e gatilhos do ECC4.

Os pacotes ficam em `data/packages.csv` (id, nome, palavras-chave) e podem ser trocados pela lista oficial do gerente.

Os dados vêm do projeto A9 (caso **Ledpettie Bridge**) e estão **pseudonimizados**: só aparecem funções, sem nomes de pessoas, e-mails ou telefones.
- `data/documents.csv`: linha do tempo montada a partir da correspondência do CEMAR (GC-347 a GC-1456) e do registro de EW.
- `data/programme.csv` e `data/programme_findings.csv`: atividades e checagens geradas do cronograma P6 rev 017 (data date 22/05/2026).
- `data/ew_scan.csv`: varredura do registro de EW de 01/10/2026.
- `data/tracker.csv`: 849 fatos (EWs + notas datadas das reuniões de EW + correspondência do CEMAR) em 219 assuntos.
- `data/activities.csv` e `data/relationships.csv`: as 3.385 atividades e 6.998 vínculos do P6 rev 017, usados para sugerir atividades e calcular o impacto.
- `data/ce_record.csv`: CE-001 a CE-037, registrados no próprio P6.

Mesmo sem nomes, esses dados continuam **confidenciais** (cl. 29 do contrato). Não publique o repositório.

## Atualizar os dados a partir dos arquivos brutos

```bash
python scripts/build_data.py --xer caminho/programa.xer --ewr "caminho/Early Warning Register.xlsx" --names nomes_privados.txt
```

Os arquivos brutos (.xer, registros, PDFs) e a lista de nomes a apagar (`--names`, um por linha) ficam **fora** do repositório.

## Como rodar (Cursor ou terminal)

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # opcional: coloque a ANTHROPIC_API_KEY para ligar a IA
streamlit run app.py
```

Sem chave de API, o app funciona em **modo offline**, com extração por palavras-chave. Com a chave, a extração e o refinamento da minuta usam o Claude.

Para testar a ingestão, suba `samples/utility_delay_letter.txt`. É uma carta fictícia de concessionária atrasando a remoção de fibra óptica no KM 1–4.

## Estrutura

```
app.py                 # interface Streamlit
core/nec4_rules.py     # prazos e gatilhos NEC4 (8 semanas, 1 semana, etc.)
core/extractor.py      # leitura de PDF/DOCX/EML, redação de dados pessoais, extração IA/regras
core/notice.py         # minuta da notificação cl. 61.3 + export .docx
core/xer.py            # leitor de cronograma Primavera P6 (.xer)
scripts/build_data.py  # gera os CSVs a partir do .xer e do registro de EW
data/documents.csv     # registro de documentos (evidências)
data/programme.csv     # atividades do P6 ligadas a cada evento
data/programme_findings.csv  # checagens de lógica do cronograma
data/ew_scan.csv       # varredura retroativa do registro de EW
samples/               # documentos fictícios para testar
```

## Proteção de dados (UK GDPR / DPA 2018) e confidencialidade (cl. 29)

- Use apenas dados **anonimizados ou pseudonimizados** fora do ambiente da empresa.
- O app apaga e-mails e telefones antes de processar. A IA recebe instrução de usar funções, nunca nomes.
- Com a API da Anthropic, os dados enviados não são usados para treinar modelos. Mesmo assim, é preciso fechar um contrato de processamento de dados (DPA) com a empresa e fazer uma avaliação de impacto (DPIA) antes de usar dados reais.
- Não versione `.env` nem documentos reais no Git.

## Próximos passos (pós-MVP)

- Importar também MS Project (.xml) e comparar revisões do cronograma (017 vs. versões seguintes).
- Cruzar a varredura de EW com o registro de CEs do CEMAR.
- Ingestão automática: caixa de e-mail do projeto (Microsoft Graph), SharePoint e CEMAR.
- Alertas por e-mail ou Teams nos marcos de 4 e 6 semanas.
- Busca semântica no acervo de PDFs para a varredura retroativa.
- Persistência em banco de dados (hoje os dados vivem na sessão).

## Publicar no Streamlit Community Cloud (privado)

1. Suba o projeto para um repositório **privado** no GitHub.
2. Em share.streamlit.io, entre com o GitHub, clique em **Create app** e escolha o repositório, a branch `main` e o arquivo `app.py`.
3. Em **Advanced settings**, cole em *Secrets* o conteúdo de `.streamlit/secrets.example.toml`, já com a senha trocada.
4. Depois do deploy, use **Share** para convidar por e-mail quem pode ver o app.

Apps vindos de repositório privado já nascem privados. Na conta gratuita, só pode haver **um** app privado por vez.
