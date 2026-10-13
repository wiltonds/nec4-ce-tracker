# NEC4 CE Tracker — MVP

Protótipo em Streamlit para controlar atrasos e eventos compensatórios (compensation events) em contratos **NEC4 ECC**. O MVP:

1. **Dashboard**: mostra alertas de eventos ainda não notificados, a contagem regressiva das 8 semanas (cl. 61.3), o cronograma planejado vs. hoje e a checagem da lógica do cronograma.
2. **Timeline**: mostra a linha do tempo das evidências e a janela de 8 semanas que cada fato novo abre.
3. **EW scan**: faz a varredura retroativa do registro de early warnings e lista os avisos sobre cliente ou terceiros sem referência a evento compensatório.
4. **Ingest documents**: recebe PDF, Word, e-mail ou TXT, apaga e-mails e telefones e extrai data, referência, remetente (por função), local, cláusulas e se o documento é um possível evento compensatório.
5. **Draft notification**: gera a minuta da notificação (cl. 61.3) com a cronologia e as atividades bloqueadas, e permite baixar em .docx.
6. **NEC4 rules**: traz a base de regras e prazos do ECC4, parafraseada.

Os dados vêm do projeto A9 (caso **Ledpettie Bridge**) e estão **pseudonimizados**: só aparecem funções, sem nomes de pessoas, e-mails ou telefones.
- `data/documents.csv`: linha do tempo montada a partir da correspondência do CEMAR (GC-347 a GC-1456) e do registro de EW.
- `data/programme.csv` e `data/programme_findings.csv`: atividades e checagens geradas do cronograma P6 rev 017 (data date 22/05/2026).
- `data/ew_scan.csv`: varredura do registro de EW de 01/10/2026.

Mesmo sem nomes, esses dados continuam **confidenciais** (cl. 29 do contrato). Não publique o repositório.

## Atualizar os dados a partir dos arquivos brutos

```bash
python scripts/build_data.py --xer caminho/programa.xer --ewr "caminho/Early Warning Register.xlsx"
```

Os arquivos brutos (.xer, registros, PDFs) ficam **fora** do repositório.

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
