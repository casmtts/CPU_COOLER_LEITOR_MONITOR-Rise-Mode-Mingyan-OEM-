# Monitor CPU Cooler

**Autor: casmtts**

Monitor de temperatura para water coolers/coolers com display USB
(`VID 0xAA88` / `PID 0x8666`), feito em Python e empacotado como um
**executável standalone para Windows**.

É uma alternativa **open-source, transparente e independente**: todo o
código-fonte está disponível neste repositório, sem componentes ocultos,
coleta de dados ou serviços externos. O programa roda localmente, lê os
sensores de hardware e conversa diretamente com o display USB — você pode
auditar, modificar, compilar e distribuir livremente.

---

## Sumário

- [O que faz](#o-que-faz)
- [Início rápido (executável)](#início-rápido-executável)
- [Interface (GUI)](#interface-gui)
- [Inicialização automática / UAC](#inicialização-automática--uac)
- [Comandos de linha](#comandos-de-linha)
- [Como funciona](#como-funciona)
- [Drivers e HVCI](#drivers-e-hvci)
- [Arquivos do projeto](#arquivos-do-projeto)
- [Compilando do código-fonte](#compilando-do-código-fonte)
- [Solução de problemas](#solução-de-problemas)
- [Segurança](#segurança)
- [Licença](#licença)

---

## O que faz

- Mostra em tempo real, na janela e no display do cooler:
  - **Temperatura da CPU** (valor principal, no mostrador circular);
  - **Uso de CPU**, **GPU** e **RAM**;
  - **Temperatura da GPU** e da **placa-mãe**;
  - **PUMP** e fans do sistema (`SYSFAN 1`, `SYSFAN 2`, `SYSFAN 3`).
- Envia continuamente o relatório HID de 11 bytes para o visor do cooler.
- Inicia o leitor de sensores **automaticamente**, sem precisar abrir o
  LibreHardwareMonitor nem o software do fabricante.
- Suporta **WinRing0** e **PawnIO**, com troca automática se um falhar.
- Cria, com um clique, uma **tarefa agendada elevada** para iniciar no logon
  **sem pedir UAC toda vez**.
- A janela tem barra de título própria, botões estilizados, temas claro/escuro,
  área rolável com roda do mouse e ícone na bandeja.

> Observação: a temperatura dos pentes de memória (DIMM) não é exibida nesta
> versão. Ela só aparece com PawnIO + LibreHardwareMonitor 0.9.5, e isso
> exigiria um segundo processo de leitura — optou-se por manter o consumo
> mínimo (um único leitor).

---

## Início rápido (executável)

1. Baixe/clone este repositório.
2. Dê dois cliques em **`MonitorCPUCooler.exe`**.
3. Na **primeira execução**, a tela *Configuração inicial* mostra os requisitos:
   - clique em **Instalar / configurar agora** para criar a tarefa elevada
     (UAC aparece **uma única vez**);
   - ou **Pular** para configurar depois pelo botão *Iniciar com Windows*.
4. A janela abre com o spinner de carregamento e, em ~3–4 s, os valores reais
   aparecem (o LibreHardwareMonitor leva alguns segundos para detectar o
   hardware na primeira leitura).

O `.exe` é portátil: copie apenas ele para qualquer PC. Na primeira execução
ele cria ao lado dele os arquivos auxiliares (`read_lhm.ps1`, DLLs, ícones e
scripts de inicialização).

---

## Interface (GUI)

| Elemento | Função |
|----------|--------|
| Mostrador circular | Temperatura da CPU (com arco de progresso) |
| Coluna **CPU** | Uso da CPU, PUMP, SYSFAN 1/2/3 (RPM) |
| Coluna **GPU** | Uso da GPU (%) e temperatura da GPU (°C) |
| Coluna **MEMÓRIA** | Uso da RAM (%) |
| Coluna **PLACA MÃE** | Temperatura da placa (°C) |
| `Mudar °C / °F` | Alterna a unidade exibida (o envio ao cooler é sempre em °C) |
| `Iniciar com Windows` | Cria/verifica a tarefa elevada no logon |
| `Ativar driver` | Reinicia elevado para carregar WinRing0/PawnIO |
| `Ver log / terminal` | Abre a janela com tudo que o programa está executando |
| `Tema: claro/escuro` | Alterna o tema visual completo |
| `Ocultar detalhes` | Esconde/mostra os cartões de métricas |
| `─` / `□` / `✕` | Minimizar / maximizar-restaurar / minimizar para a bandeja |

- O **X** minimiza para a bandeja (o monitor continua enviando ao visor).
  Para encerrar de verdade: ícone da bandeja → **Sair**.
- A roda do mouse rola o painel em qualquer ponto da janela.
- Tooltips explicam o WinRing0 (driver de leitura de hardware, não malware).

---

## Inicialização automática / UAC

O botão **Iniciar com Windows** cria uma tarefa no Agendador de Tarefas:

```
Nome:     \MonitorCPUCooler
Gatilho:  Logon do usuário
RunLevel: HighestAvailable
Comando:  <pasta>\MonitorCPUCooler.exe --driver
```

O UAC aparece **uma vez**, apenas na criação da tarefa. Depois disso o Windows
inicia o programa elevado no logon, sem aviso.

Arquivos de estado criados ao lado do `.exe`:

| Arquivo | Significado |
|---------|-------------|
| `first_run.done` | a tela de configuração inicial já foi exibida |
| `task_configured` | a tarefa elevada já existe (evita pedir UAC de novo) |

Para ver a tela inicial novamente, apague `first_run.done`.

---

## Comandos de linha

Todos os comandos abaixo funcionam tanto com `python monitor.py` quanto com
`MonitorCPUCooler.exe`.

```bat
MonitorCPUCooler.exe                    modo normal (GUI + leitor + HID)
MonitorCPUCooler.exe --interval 0.5     intervalo de envio em segundos (padrão 0,5)
MonitorCPUCooler.exe --unit F           exibir em Fahrenheit (padrão C)
MonitorCPUCooler.exe --no-gui           apenas console
MonitorCPUCooler.exe --once             lê uma vez, envia e sai (o visor apaga)
MonitorCPUCooler.exe --dry-run          não grava no HID (teste)
MonitorCPUCooler.exe --probe            só inspeciona o dispositivo HID
MonitorCPUCooler.exe --test-display 77  envia valor fixo 77 ao visor (teste)
MonitorCPUCooler.exe --driver           força driver/elevação para ler a CPU real
MonitorCPUCooler.exe --elevate          pede administrador
MonitorCPUCooler.exe --source auto|dll|wmi   origem dos sensores (padrão auto)
MonitorCPUCooler.exe --layout direct|original  posição do byte de temperatura
MonitorCPUCooler.exe --install-startup        cria a tarefa elevada
MonitorCPUCooler.exe --uninstall-startup      remove a tarefa
MonitorCPUCooler.exe --startup-status         mostra se a tarefa existe
```

### Exemplos

```bat
:: testar sem escrever no cooler
python monitor.py --dry-run --no-gui

:: descobrir o dispositivo HID
python monitor.py --probe

:: forçar leitura elevada da CPU
python monitor.py --driver

:: verificar inicialização automática
python monitor.py --startup-status
```

---

## Como funciona

```
LibreHardwareMonitorLib.dll (32 bits)
        |  sensores: /amdcpu/..., /gpu-..., /lpc/nct6799d/...
        v
read_lhm.ps1  →  JSON no stdout (uma linha a cada 500 ms)
        |  (thread + fila não bloqueante)
        v
monitor.py  →  monta payload de 11 bytes
        |
        v
HID WriteFile (Report ID 0)  →  display do cooler (VID 0xAA88 / PID 0x8666)
```

### Protocolo HID

Relatório de **11 bytes**, validado em hardware (Rise Mode / Mingyan OEM):

| Byte | Conteúdo |
|------|----------|
| 0 | Report ID (`0x00`) |
| 1 | **valor exibido no visor — temperatura da CPU** |
| 2 | temperatura da GPU (°C) |
| 3 | uso da CPU (%) |
| 4 | uso da RAM (%) |
| 5 | temperatura da placa-mãe (°C) |
| 6 | uso da GPU (%) |
| 7–10 | reservado / RPM |

O firmware do visor tem watchdog: é necessário enviar um pacote a cada
2–3 s. O intervalo padrão de 0,5 s garante atualização contínua.

A comunicação é feita diretamente com o dispositivo HID, sem processos
intermediários, pipes ou serviços adicionais.

---

## Drivers e HVCI

| Driver | DLL | Quando usar |
|--------|-----|-------------|
| **WinRing0** | `LibreHardwareMonitorLib.dll` (0.9.1) | padrão; exige HVCI e bloqueio de drivers vulneráveis desligados |
| **PawnIO** | `LibreHardwareMonitorLib_PawnIO.dll` (0.9.5) | moderno, assinado, funciona com HVCI ligado |

O programa detecta automaticamente qual está disponível e troca se a leitura
falhar por vários ciclos (`[DRIVER] Leitura zerada — trocando para ...`).

`desativar_hvci.bat` desativa a *Integridade da memória* e o bloqueio de
drivers vulneráveis do Windows (requer admin + reinício). Use apenas se
aceitar o risco de permitir o WinRing0.

---

## Arquivos do projeto

| Arquivo | Descrição |
|---------|-----------|
| `monitor.py` | programa principal (GUI, HID, leitor, startup, trays) |
| `read_lhm.ps1` | leitor dos sensores via DLL do LHM (32 bits) |
| `install_task.ps1` / `uninstall_task.ps1` | criam/removem a tarefa elevada |
| `desativar_hvci.bat` | desativa HVCI + bloqueio de drivers vulneráveis |
| `make_icon.py` | gera o ícone (ventoinha + termômetro) |
| `cooler_icon.ico` / `.png` | ícone do programa |
| `LibreHardwareMonitorLib.dll` | LHM 0.9.1 (WinRing0) |
| `LibreHardwareMonitorLib_PawnIO.dll` | LHM 0.9.5 (PawnIO) |
| `RAMSPDToolkit-NDD.dll` | dependência da DLL 0.9.5 (SPD) |
| `HidSharp.dll` | dependência opcional do LHM |
| `MonitorCPUCooler.exe` | executável standalone (PyInstaller onefile) |
| `MonitorCPUCooler.spec` | receita de build do PyInstaller |
| `requirements.txt` | dependências Python (para rodar da fonte) |

---

## Compilando do código-fonte

Requer **Python 3.12 (64 bits)** e **PyInstaller**:

```bat
pip install -r requirements.txt
pip install pyinstaller

pyinstaller --noconfirm --onefile --noconsole ^
  --name MonitorCPUCooler ^
  --icon cooler_icon.ico ^
  --add-data "read_lhm.ps1;." ^
  --add-data "cooler_icon.png;." ^
  --add-data "cooler_icon.ico;." ^
  --add-data "LibreHardwareMonitorLib.dll;." ^
  --add-data "LibreHardwareMonitorLib_PawnIO.dll;." ^
  --add-data "RAMSPDToolkit-NDD.dll;." ^
  --add-data "install_task.ps1;." ^
  --add-data "uninstall_task.ps1;." ^
  --add-data "desativar_hvci.bat;." ^
  --hidden-import=wmi --hidden-import=win32com.client ^
  --hidden-import=pythoncom --hidden-import=pywintypes monitor.py
```

O resultado fica em `dist\MonitorCPUCooler.exe`.

Para rodar sem compilar:

```bat
python -m pip install -r requirements.txt
python monitor.py
```

---

## Solução de problemas

| Sintoma | Causa / solução |
|---------|-----------------|
| `WinError 32` ao abrir | outro programa está usando o visor em modo exclusivo. Sendo elevado, o monitor encerra automaticamente o processo que segura o HID; caso contrário, feche-o manualmente. |
| UAC toda vez que abre | execute `--install-startup` uma vez; o marcador `task_configured` evita novos pedidos |
| Temperatura da CPU em 0 | driver bloqueado: use `--driver`, desative HVCI (`desativar_hvci.bat` + reinício) ou instale o PawnIO |
| Primeira leitura demora ~4 s | é o `Computer.Open()` do LibreHardwareMonitor detectando hardware; o spinner indica o carregamento |
| Visor apaga | o firmware desliga sem pacotes a cada 2–3 s; deixe o monitor rodando (não use `--once`) |
| SmartScreen ("Windows protegeu seu PC") | `.exe` não assinado → *Mais informações → Executar assim mesmo* |
| Valores zerando intermitentemente | o leitor reinicia sozinho e mantém o último valor válido por 15 s |

---

## Segurança

- O WinRing0 1.2.0.5 é um driver antigo com IOCTLs sem verificação de
  permissão — é classificado como vulnerável e bloqueado pelo Windows.
  Desativar o HVCI/bloqueio reduz a proteção do sistema.
- O PawnIO é a alternativa moderna e assinada.
- O programa não coleta dados nem envia nada à internet; toda a comunicação é
  local (HID + WMI/registry do Windows).
- A chave de inicialização é criada em **HKCU** (escopo do usuário).

---

## Licença

MIT — casmtts.
