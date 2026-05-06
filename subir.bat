@echo off
cd /d "C:\Users\coyot\Desktop\dapaz modas"

echo ===================================
echo SUBINDO PRO GITHUB
echo ===================================

IF NOT EXIST ".git" (
    echo Inicializando repositorio...
    git init
    git remote add origin https://github.com/leodonkyabrasil/dapazatelie.git
    git branch -M main
)

echo Adicionando arquivos...
git add .

echo Commit...
git commit -m "update"

echo Enviando...
git push origin main

pause