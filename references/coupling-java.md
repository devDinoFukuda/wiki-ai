# coupling-java

Racional de design do motor Java de `coupling.md` (`scripts/codescan/coupling_java.py`
+ `coupling_java_md.py` + `coupling_java_html.py`, escolhidos pelo dispatcher
`scripts/codescan/coupling.py` quando o repo tem Java — ver `sdd-contract.md`
§1.6). Este documento explica **por que** cada sinal e cada limiar existem
do jeito que existem — o contrato de artefato (seções obrigatórias, gates
Mermaid) fica em `sdd-contract.md`; aqui fica só a justificativa e as
decisões testadas contra dado real que levaram ao desenho atual.

---

## 1. Por que duas granularidades, não uma

**Por pacote** e **Por classe** não são o mesmo gráfico em duas escalas —
são duas análises com objetivo, metodologia e critério de validação
diferentes.

| | Por pacote | Por classe |
|---|---|---|
| Metodologia | Robert C. Martin (1994/2002), sem adaptação | Sinais próprios, construídos pra essa granularidade |
| Validado contra | Código-fonte do JDepend (implementação de referência) | Dados reais + literatura de code smells (Fowler, McCabe) |
| Limiares | Estruturais (0.5/0.5 — vem da própria fórmula) | Estatísticos, relativos ao projeto (ver §3) |
| O que responde | "A arquitetura, como um todo, está balanceada?" | "Quais classes específicas merecem revisão, e por quê?" |

Misturar as duas produz o defeito que motivou separá-las: A vira binário
por classe (geometricamente degenerado — não existe "meia interface"),
e zonas por classe perdem sentido.

## 2. Por pacote — metodologia de Martin, integral

Confirmado contra o código-fonte do JDepend: Ca, Ce, I, A, D e a Main
Sequence são métricas de **pacote**, não de classe — aplicar isso em outra
granularidade não é uma variação da mesma ideia, é uma coisa diferente
usando o mesmo nome. Por isso essa visão não tem parâmetro configurável: os
limites (0.5/0.5) vêm da própria definição matemática (`I = Ce/(Ce+Ca)`,
`A` = proporção de classes abstratas), não são calibração do projeto.

| Zona | Condição | Risco |
|---|---|---|
| Dor | I<0.5 E A<0.5 | Estável e concreto — cuidado ao mudar, mas **esperado e saudável** em pacotes de domínio/modelo centrais |
| Inutilidade | I≥0.5 E A≥0.5 | Abstrato mas instável — contrato que não se comporta como contrato |
| Saudável / transição | demais casos | Perto ou longe da Main Sequence |

## 3. Por classe — sinais independentes, não uma fórmula única

Cada sinal é independente: endereça um risco específico, tem fundamentação
própria, é reportado separadamente. Não existe uma "nota de qualidade"
combinada — isso esconderia qual problema específico está em qual classe.

### 3.1 Ciclos de dependência
Ciclo no grafo de import entre classes viola o Princípio das Dependências
Acíclicas (Martin) — quebra build incremental, impede entender/testar uma
classe isoladamente. Sem limiar, sem calibração: é fato de grafo, existe ou
não existe. 🟢.

### 3.2 Classe-deus
```
is_god = Ce é outlier estatístico (ver §4)  E  WMC_max é outlier estatístico (ver §4)
```
Duas dimensões, com E, porque amplitude (depende de muita coisa) e
profundidade (tem lógica muito complexa) são riscos diferentes. Usar só
amplitude (Ce) marca orquestrador/gateway/handler legítimo como problema —
validado contra um projeto real de 96 classes: 6 tinham Ce alto, a maioria
orquestradores válidos, não classes-deus.

Complexidade é o **máximo** por método, não a soma da classe: a soma
confunde "muitos métodos simples e independentes" (ex.: um
`ApiExceptionHandler` com 11 métodos, 1 por tipo de exceção) com "um método
realmente emaranhado" — os dois cenários produzem a mesma soma (WMC=8 num
teste sintético) por motivos opostos. O máximo isola o sinal certo.

Limitação conhecida e validada contra projeto real: em código bem
decomposto, os outliers de Ce e de WMC_max podem ser classes diferentes —
interseção vazia com IQR não é falha do critério, é evidência de que os
dois problemas não coexistem na mesma classe naquele projeto. Por isso o
render também lista `is_ce_outlier`/`is_wmc_outlier` separadamente, não só
a interseção.

### 3.3 Implementação concreta sobre-dependida
```
is_concrete_hotspot = kind == class  E  NOT is_abstract
                       E  Ca é outlier estatístico DENTRO do subgrupo kind == class
```
Mesmo princípio da Zona de Dor do "Por pacote" — Ca alto só é seguro quando
quem depende está protegido por uma abstração. Numa classe concreta sem
interface, qualquer mudança de implementação atinge todo mundo que depende
dela diretamente (viola Dependency Inversion).

Record/enum/interface ficam fora do subgrupo comparado: são estruturas de
dado imutáveis (ou contratos) — Ca alto ali é estrutural e esperado (um
`Quote`/`Customer`/`Product` tem Ca alto por natureza; forçar uma interface
na frente só para "resolver" a métrica seria o erro oposto, abstração
especulativa). Validado contra projeto real: das 10 classes com maior Ca,
8 eram record/enum/interface (sem risco) e só uma classe concreta
("logger" de infraestrutura) se destacou de fato.

Limitação assumida: classe utilitária estática pode ser marcada mesmo tendo
risco baixo na prática (função pura raramente muda de assinatura) —
detectar "só tem método estático, sem campo de instância" não está
implementado; avalie esse caso manualmente.

### 3.4 Abstração especulativa
```
is_speculative = is_abstract  E  implementor_count <= 1  E  Ca <= implementor_count
```
Fowler chama isso de *Speculative Generality* — interface/classe abstrata
com no máximo 1 implementação e nenhum consumidor real além dela mesma não
gera ganho de desacoplamento na prática.

## 4. Limiar estatístico, não número fixo

Testado com limiar fixo (Ce≥8, WMC_max≥15) contra um projeto real de 96
classes bem escrito: zero classes-deus, porque o maior WMC_max real do
projeto (10) nunca chega perto de um limiar calibrado em exemplo sintético.
Isso não é bug de execução, é bug de design — número fixo não se adapta à
escala de cada projeto.

Método: outlier via IQR (regra do boxplot, Tukey 1977) — não "top N%" (isso
sempre aponta alguém, mesmo em distribuição uniforme, gerando ruído em
projeto bem escrito):

```
Q1, Q3 = primeiro e terceiro quartil da métrica, dentro do projeto
IQR = Q3 - Q1
limiar = Q3 + 1.5 * IQR
```

Só acende quando existe uma classe genuinamente destoante da distribuição
**daquele projeto especificamente**. Precisa de pelo menos 4 valores para
quartil fazer sentido — com menos, o limiar é `None` (reportado como
"N/A (poucas classes p/ estatística)" no render), e nenhuma classe é
marcada por esse eixo: N pequeno demais para estatística, melhor não marcar
nada do que marcar errado.

## 5. Fora de escopo (decisão testada, não só teórica)

| Item | Motivo |
|---|---|
| Coesão (LCOM/TCC) | Parser AST (`javalang`) testado e descartado — falha em `record`/`sealed`/`switch` pattern matching/text blocks, sintaxe Java moderna presente em projetos reais |
| Violação de camada | Projetos usam arquiteturas diferentes (MVC, hexagonal, etc.) — regra genérica geraria falso positivo/negativo dependendo do estilo |

## 6. Limitação do parser (regex, não AST)

Igual ao motor genérico (`coupling_generic.py`), o motor Java usa regex, não
um parser AST completo — suficiente para mapa de arquitetura, não para
refatoração automatizada. Duas consequências específicas de Java:

- `import pkg.*` (wildcard) pode superestimar Ce: toda classe do pacote
  importado entra como dependência resolvida, mesmo que só uma seja
  realmente usada.
- Resolução por identificador no mesmo pacote (Java não exige import
  dentro do próprio pacote) casa pelo `simple_name`; em caso de ambiguidade
  entre pacotes, desempata preferindo o mesmo pacote do arquivo — pode
  gerar falso positivo raro quando dois tipos homônimos legítimos existem
  em pacotes diferentes.

## 7. Resumo

**Por pacote**: Ca, Ce, I, A, D, Main Sequence, Zona de Dor, Zona de
Inutilidade — metodologia de Martin, sem adaptação, sem parâmetro
configurável.

**Por classe**: 4 sinais independentes — ciclos de dependência (fato de
grafo), classe-deus (Ce outlier E WMC_max outlier), implementação concreta
sobre-dependida (Ca outlier em classe concreta, fora do subgrupo
record/enum/interface), abstração especulativa (≤1 implementação, sem
consumidor real). Todos os limiares numéricos calculados por IQR,
específicos de cada execução — nenhum número fixo global.
