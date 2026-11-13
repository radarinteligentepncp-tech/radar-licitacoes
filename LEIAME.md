# Radar de Licitações - PNCP

Aplicação Streamlit para busca de editais no PNCP, com login, senha e
**cadastro sujeito à autorização do administrador**.

## Arquivos

| Arquivo | Para que serve | Vai para o GitHub? |
|---|---|---|
| `app.py` | Aplicação principal (Radar) | Sim |
| `auth.py` | Login, solicitação de acesso e painel de gestão de usuários | Sim |
| `limitador_pncp.py` | Controla o ritmo das consultas ao PNCP (evita o erro 429) | Sim |
| `requirements.txt` | Bibliotecas necessárias | Sim |
| `.gitignore` | Impede o envio de senhas e chaves ao GitHub | Sim |
| `montar_secrets.py` | Gera a configuração secreta (senha do admin, planilha) | Sim |
| `LEIAME.md` | Este guia | Sim |
| `secrets_gerado.toml`, `.streamlit/secrets.toml`, chave `.json` | Senhas e chaves | **NUNCA** |

## Como funciona o acesso

1. O interessado abre o sistema, vai na aba **Solicitar acesso** e preenche nome,
   e-mail, órgão/empresa, usuário e senha. O cadastro fica **pendente**.
2. Enquanto estiver pendente, ele não consegue entrar (recebe o aviso
   "aguarda autorização do administrador").
3. Você entra com o usuário de administrador. A barra lateral avisa quantas
   solicitações estão pendentes. Na área **👥 Usuários** você **aprova** ou **recusa**.
4. No mesmo painel você pode **bloquear**, **reativar**, **redefinir senha**,
   **tornar administrador** ou **excluir** usuários. Um bloqueio derruba a sessão
   do usuário em até 10 minutos.

O administrador principal fica na configuração secreta, não na planilha. Assim
você nunca perde o acesso, mesmo se a planilha for apagada.

---

## Passo 1 - Testar no seu computador (Prompt de Comando - cmd)

Rode um comando por vez, dentro da pasta do projeto:

```
cd "C:\Users\Luiz Otero\Documents\Radar Licitações\radar-licitacoes"
python -m pip install -r requirements.txt
python montar_secrets.py
mkdir .streamlit
move /Y secrets_gerado.toml .streamlit\secrets.toml
streamlit run app.py
```

No `montar_secrets.py`: informe usuário e senha do administrador; para a planilha, cole o ID
(ou a URL inteira) e o caminho da pasta onde está o arquivo `.json`. Deixando a planilha em
branco, os cadastros ficam no arquivo local `.dados/usuarios.csv` (só para teste).

## Passo 2 - Criar a planilha de usuários (Google Sheets)

Na nuvem os arquivos são apagados a cada reinício, por isso os cadastros ficam
numa planilha do Google.

1. Crie uma planilha em branco no Google Planilhas (ex.: "Radar - Usuários").
   Copie o **ID** dela: é o trecho da URL entre `/d/` e `/edit`.
2. Acesse <https://console.cloud.google.com/> e crie um projeto (ex.: "radar-licitacoes").
3. Em **APIs e serviços > Biblioteca**, ative a **Google Sheets API**.
4. Em **APIs e serviços > Credenciais > Criar credenciais > Conta de serviço**,
   crie uma conta (ex.: "radar"). Não precisa conceder papéis.
5. Abra a conta de serviço criada > aba **Chaves** > **Adicionar chave > Criar
   nova chave > JSON**. Um arquivo `.json` será baixado. Guarde-o com cuidado.
6. Volte à planilha, clique em **Compartilhar** e adicione o e-mail da conta de
   serviço (termina em `iam.gserviceaccount.com`) como **Editor**.
7. Rode de novo `python montar_secrets.py`, agora informando o ID da planilha e o
   caminho do arquivo `.json`.

A aba `usuarios` e o cabeçalho são criados automaticamente no primeiro acesso.

## Passo 3 - Enviar o código para o GitHub

1. Crie uma conta em <https://github.com> (se ainda não tiver).
2. Crie um repositório **privado** (ex.: `radar-licitacoes`).
3. Em **Add file > Upload files**, envie `app.py`, `auth.py`, `requirements.txt`,
   `limitador_pncp.py`, `.gitignore`, `montar_secrets.py` e `LEIAME.md`.
   **Não envie** o `.json`, o `secrets_gerado.toml` nem a pasta `.streamlit`.

## Passo 4 - Publicar no Streamlit Community Cloud (gratuito)

1. Acesse <https://share.streamlit.io> e entre com sua conta do GitHub.
2. Clique em **Create app > Deploy a public app from GitHub** e escolha o
   repositório, a branch `main` e o arquivo `app.py`.
3. Em **Advanced settings > Secrets**, cole todo o conteúdo de
   `secrets_gerado.toml`. Salve e clique em **Deploy**.
4. Depois de publicado, **apague** o `secrets_gerado.toml` do seu computador.
5. Envie o link `https://<nome>.streamlit.app` para quem for usar. Cada pessoa
   solicita o acesso, e você autoriza.

O app pode ser público no Streamlit: quem não tiver login aprovado só vê a
tela de entrada.

Para alterar a configuração depois: no painel do Streamlit Cloud, em
**⋮ > Settings > Secrets**.

## Observações

- O plano gratuito do Streamlit pode colocar o app para "dormir" após um período
  sem acesso. Basta abrir o link e clicar para reativá-lo (leva alguns segundos).
- Também dá para aprovar ou bloquear direto na planilha, trocando a coluna
  `status` para `aprovado`, `recusado` ou `bloqueado`.
- Senhas são guardadas apenas como hash (PBKDF2-SHA256). Nem você nem o Google
  conseguem lê-las. Se alguém esquecer a senha, use **Redefinir senha** no painel.
