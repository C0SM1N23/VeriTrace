# Instalare si prima rulare

De la zero pana la aplicatia pornita pe designul tau. Pentru referinta completa
de comenzi, [README.md](../README.md).

---

## 1. Ce trebuie sa ai instalat

| | De ce | De unde |
|---|---|---|
| **Python 3.12–3.14** | tot backend-ul | [python.org](https://python.org), sau `uv python install 3.12` |
| **uv** | creeaza mediul virtual | `winget install astral-sh.uv` |
| **Node 20+** | compileaza interfata | [nodejs.org](https://nodejs.org) |
| **Rust** | motorul de trace se compileaza la instalare | [rustup.rs](https://rustup.rs) |
| **Icarus Verilog** | simulatorul principal (§4.0) | [bleyer.org/icarus](https://bleyer.org/icarus) |

Verilator, ModelSim si Vivado xsim sunt **optionale** — VeriTrace citeste ce
scriu ele, dar nu are nevoie de ele ca sa functioneze.

---

## 2. Instalarea, o data

Copiaza tot blocul in PowerShell. Poate fi rulat de cate ori vrei.

```powershell
$ErrorActionPreference = "Stop"

Set-Location "K:\VeriTrace"
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force

# Icarus nu se pune singur in PATH pe Windows
if (Test-Path "C:\iverilog\bin\iverilog.exe") { $env:PATH = "C:\iverilog\bin;$env:PATH" }

# Mediul virtual, creat doar daca lipseste
if (-not (Test-Path ".\.venv\Scripts\python.exe")) { uv venv --seed --python 3.12 .venv }
& ".\.venv\Scripts\Activate.ps1"
python -c "import sys; print('Python:', sys.executable)"   # trebuie sa arate .venv

# Pachetul Python, cu motorul Rust in el
python -m pip install -e .

# Interfata web, compilata in pachet
Push-Location ".\web"
npm install
npm run build
Pop-Location

# Verificare
veritrace --version
iverilog -V | Select-Object -First 1
```

Daca `python -c ...` arata alt Python decat cel din `.venv`, activarea n-a
prins si build-ul va folosi interpretorul gresit. Reia de la `Activate.ps1`.

---

## 3. Prima rulare, pe un exemplu

```powershell
veritrace run ".\designs\fifo_buggy" --serve
```

Simuleaza, converteste, verifica si deschide browserul. Designul ala are un
reset legat la 0 intentionat — tab-ul **Causal** il gaseste in trei click-uri.

---

## 4. Pe designul tau

### Cazul simplu: un folder de fisiere

```powershell
veritrace run "C:\calea\catre\rtl"
```

Gaseste fisierele `.v`/`.sv`, ghiceste modulul top (cel pe care nu-l
instantiaza nimeni), simuleaza si raporteaza. Daca testbench-ul n-are
`$dumpfile`, se compileaza alaturi un modul generat care dumpuieste tot —
sursele tale nu se modifica.

### Cazul real: filelist-uri, include-uri, programe `.hex`

Un proiect serios are deja `.f`-uri. Da-i-le direct:

```powershell
Set-Location "C:\Users\bunea\OneDrive\Desktop\siemens\debug\sim"
veritrace run rtl.f tb_cpu.f --top tb_cpu_axi
```

Trei lucruri se intampla aici, si toate trei conteaza:

1. **`.f`-urile sunt date compilatorului cu `-f`**, cu tot cu `+incdir+`. Nu
   sunt reinterpretate — Icarus le citeste, ca la ModelSim si Verilator.
2. **Simularea ruleaza din directorul in care ai dat comanda**, adica de unde
   ruleaza si fluxul tau. De asta `$readmemh("program_axi.hex")` gaseste
   fisierul si caile relative din `.f` se rezolva.
3. **Rezultatele merg in `.veritrace\`** — build, waveform si log. Sursele
   raman curate.

### Optiuni

| | |
|---|---|
| `--top <modul>` | cand exista mai multe testbench-uri |
| `--incdir <dir>` | cand un `` `include `` nu se gaseste (comanda iti spune care) |
| `-D NUME=1` | define de compilare |
| `--timeout 600` | testbench lung |
| `--serve` | deschide interfata la final |
| `--fail-on ...` | iese cu cod diferit de zero — poarta de CI |

---

## 5. `.veritrace.toml` — ca sa nu mai dai argumente

`veritrace run` scrie unul daca nu exista. Pentru un proiect real merita
completat; dupa el, `veritrace check`, `veritrace serve`, `veritrace txn` merg
fara niciun argument.

Asta e cel scris pentru CPU-ul RISC-V, cu explicatia fiecarei linii:

```toml
[design]
top     = "tb_cpu_axi"
rtl     = ["../../hdl/*.v", "../hdl/*.v"]
incdirs = [".", "../../hdl", "../hdl"]

[clocks]
primary = "clk"

[reset]
signal = "rst_n"
active = "low"

[trace]
default = ".veritrace/dump.vcd"
# Semnale de testbench care nu spun nimic despre design: scoase din stuck,
# lint si corelare, ca lista de constatari sa fie despre RTL.
ignore = ["tb_cpu_axi.*_mon.*", "*.dummy", "*_cnt"]

[protocol]
# Fara asta, pack-ul generic Handshake prinde si semnalele interne de pipeline
# ale monitoarelor: 27 de "interfete" in loc de 7 magistrale.
packs = ["axi4lite"]

[coverage]
# Baza de date scrisa de run_verilator.ps1. Se ia si automat daca e in locul
# obisnuit; linia asta e pentru cand nu e.
path = "coverage.dat"

[checks]
# Un testbench mare are multe semnale care legitim nu se misca intr-o rulare
# care trece. 100 de cicluri (default) le raporteaza pe toate.
stuck_cycles = 400

[ui]
radix = { "*addr*" = "hex", "*data*" = "hex", "*_pc*" = "hex" }
```

---

## 6. Coverage de cod (optional)

VeriTrace **importa** coverage, nu il produce. Sursele valide (§8.12):

```powershell
# Verilator
verilator --binary --coverage --coverage-line --coverage-toggle rtl\*.sv tb.sv
.\obj_dir\Vtb
veritrace coverage --coverage logs\coverage.dat

# Vivado xsim
xcrg -report_format xml -dir xsim.covdb -report_dir cov_report
veritrace coverage --coverage cov_report\dashboard.xml
```

ModelSim editia free n-are coverage real — de asta nu apare aici.

Pentru fiecare punct neacoperit, tab-ul **Coverage** arata conditiile care
l-ar inchide, derivate din graf, nu sugerate.

---

## 7. Alte simulatoare

VeriTrace conduce doar Icarus. Flag-urile celorlalte sunt in `Makefile`, unde
sunt deja corecte — si nu sunt optionale: fara `-fno-inline` la Verilator sau
`+acc` la ModelSim, ierarhia din trace nu mai corespunde cu RTL-ul si corelarea
cade sub 50%.

```powershell
make sim-verilator convert DESIGN=<dir> TOP=<top>
make sim-modelsim  convert DESIGN=<dir> TOP=<top>
make sim-xsim      convert DESIGN=<dir> TOP=<top>
```

Apoi `veritrace check <dir>\dump.vcd --rtl <dir>` ca de obicei.

---

## 8. Cand ai schimbat codul VeriTrace

```powershell
maturin develop                                    # ai atins crates\ (Rust)
Push-Location ".\web"; npm run build; Pop-Location # ai atins web\

cargo test --workspace
python -m pytest -q
Push-Location ".\web"; npm test; Pop-Location
```

**Opreste serverul inainte de `maturin develop`** — tine `_native.pyd` deschis
si build-ul pica cu `os error 32`:

```powershell
Get-Process veritrace,python -ErrorAction SilentlyContinue |
  Where-Object { $_.Path -like '*VeriTrace*' } | Stop-Process -Force
```
