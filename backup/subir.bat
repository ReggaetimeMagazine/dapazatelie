@echo off
chcp 65001 >nul
cd /d "C:\Users\coyot\Desktop\dapaz modas"

echo.
echo ================================
echo  DA PAZ ATELIE - GitHub Pages
echo ================================
echo.

IF NOT EXIST ".git" (
    echo Inicializando git...
    git init
    git branch -M main
)

echo Configurando remote...
git remote remove origin 2>nul
git remote add origin https://github.com/ReggaetimeMagazine/dapazatelie.git

if not exist "data\produtos.json" (
    echo.
    echo AVISO: data\produtos.json nao encontrado!
    echo Exporte pelo Admin e coloque em data\produtos.json
    echo.
    pause
    exit /b 1
)

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMddHHmm"') do set VER=%%i
echo Versao: %VER%

powershell -NoProfile -Command "$c=[IO.File]::ReadAllText('index.html');$c=$c -replace 'v=\d{12}','v=%VER%';[IO.File]::WriteAllText('index.html',$c)"

echo Adicionando arquivos...
git add .

git diff --cached --quiet
if not errorlevel 1 (
    echo Nenhuma mudanca. Nada para subir.
    pause
    exit /b 0
)

echo Fazendo commit...
git commit -m "update %VER%"

echo Enviando pro GitHub...
git push -u origin main
if errorlevel 1 (
    echo.
    echo ERRO no push. Verifique conexao ou credenciais.
    pause
    exit /b 1
)

echo.
echo ================================
echo  PRONTO! Versao %VER% no ar.
echo  https://reggaetimemagazine.github.io/dapazatelie/
echo  Aguarde 60s e abra o link.
echo ================================
echo.
pause
