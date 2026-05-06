# Da Paz Ateliê — Estrutura do Site

## Pasta raiz: C:\Users\coyot\Desktop\dapaz modas\

```
dapaz modas\
│
├── adm.html          ← SITE PÚBLICO (renomeie para index.html ao subir)
├── subir.bat         ← Clique duas vezes para subir tudo pro GitHub
│
├── adm\
│   └── index.html    ← PAINEL ADMIN (abra localmente no navegador)
│
├── data\
│   └── produtos.json ← JSON com toda a loja (gerado pelo Admin)
│
└── img\
    └── fotos.js      ← Fotos embutidas (não edite manualmente)
```

## Fluxo de atualização

1. Abra `adm\index.html` no navegador
2. Edite produtos, seções, artigos, conceito
3. Vá em **Exportar JSON** → clique **Baixar produtos.json**
4. Mova o arquivo baixado para a pasta `data\`
5. Clique duas vezes em `subir.bat`
6. Aguarde ~60 segundos e acesse:
   https://leodonkyabrasil.github.io/dapazatelie/

## Adicionar fotos de produtos

No campo "Foto" do admin, cole:
- URL de uma imagem (Instagram, Imgur, Google Drive público, etc.)
- `_xadrez` → usa a foto da bolsa xadrez embutida
- `_listras` → usa a foto da bolsa de listras
- `_loja1` / `_loja2` → fotos da loja

## WhatsApp: 11986668246
## Instagram: @dapaz.costuras
## GitHub: https://github.com/leodonkyabrasil/dapazatelie
