# Design-Spec: Next-Gen Active Harness

## Context

Der bestehende `repo-agent-harness` (astrojones) hat sich zu einem breiten Plugin
entwickelt: AST-Navigation (serena-gate + `serena_*`), Repo-Fakten (`repo_*`), **plus**
durable memory (cognee, cognee_local, cognee_sync, claude_mem_reader, sync_ledger),
eine opencode-TS-Hälfte, deploy/drift/scaffold, und ~11 eigene Workflow-Skills. Das ist
mächtig, aber aufgebläht — und die Kern-Idee (Agent editiert Code, Verifikation passiert
von selbst) ist unter dem Ballast begraben.

Diese Spec entwirft einen **schlanken Greenfield-Kern**, der *ausschließlich* eine
Idee konsequent umsetzt: **Der Agent editiert nur noch Syntax-Knoten und bekommt die
Ergebnisse seiner Handlungen automatisch mitgeteilt.** Alles andere (Memory, Prozess-
Workflows) wird ausgelagert oder weggelassen.

**Kern-Inversion:** Heute *handelt* der Agent und muss dran denken zu verifizieren.
Künftig editiert er AST-Knoten; der Harness reagiert autonom und meldet zurück. Der
Agent-Loop schrumpft auf: **Knoten lesen → Knoten editieren → Events lesen.**

**Scope-Entscheidung:** Greenfield — ein **neues Standalone-Repo** (`~/dev/active-harness`),
Kern in **Python** (fastmcp + mcp + tree-sitter). *Nicht* Evolution von astrojones;
begründet unten im Delta. astrojones bleibt parallel bestehen; Konzepte (serena-Gateway,
tree-sitter-Index, health.yml-Substrat) werden übernommen, der Code wird neu und schlank
aufgesetzt.

---

## Die vier Bausteine

### 1. AST als exklusives Interface (Storage bleibt Dateien)

- **Storage unangetastet:** Dateien bleiben auf Disk → Git/CI/Editoren/Diffs funktionieren
  weiter. Der Agent sieht Dateipfade nie als primäres Interface.
- **Lesen** = Baum navigieren, Knoten-Bodies on demand expandieren
  (Muster existiert: `serena_get_symbols_overview` → `serena_find_symbol`).
- **Editieren** = Knoten-Ops: `replace_node_body`, `insert_before/after_node`,
  `rename_symbol`, `safe_delete_node` (existieren als `serena_*`, hier generalisiert).
- **Lossless Round-Trip:** Tree-sitter-CST bewahrt Kommentare/Formatting. Editiert wird
  nur der Byte-Range des betroffenen Knotens → minimale, review-freundliche Diffs;
  Formatter läuft als Post-Edit-Stage.
- **Zwei Modalitäten:**
  - *AST-Modus* für in-scope Programmiersprachen (beim Onboard gewählt).
  - *Strukturierter-Text-Modus* für Configs/Markdown/YAML/Lockfiles (Keys/Headings statt
    Zeilen, Zeilen-Fallback erlaubt). Kein sinnvoller AST → eigene Modalität.
- **Gate absolut:** Für in-scope Sprachen ist nativer Code-Read gesperrt (heutiger
  serena-gate, verschärft). Escape-Hatch nur für explizites Debugging.

### 2. Reaktive Tooling-Engine (das programmierbare Herz)

- **Deklaratives Pipeline-Spec** = "das Harness-Programm", vom Agenten beim Onboard
  geschrieben und evolviert.
- **Trigger:** Knoten-Edit committed → Dependency-Graph → welche Checks sind betroffen.
- **Gestufte Autonomie als Policy (nicht hartkodiert, evidenzbasiert iterierbar):**
  - **Tier 0** auto/instant: lint + typecheck + scoped tests auf dem Blast-Radius des Knotens.
  - **Tier 1** auto/debounced: breitere Test-Selektion, lokales Deployment, relevante e2e.
  - **Tier 2** gated: externes Deploy, Migrations, Irreversibles → braucht Freigabe.
  - Die Zuordnung Check→Tier ist konfigurierbar; wir starten mit Defaults und iterieren.
- **"Diminishing returns + Einzigartigkeit":** Test-/e2e-Selektion scort Kandidaten nach
  (a) berührt geänderten Code, (b) nicht redundant zu bereits grüner Abdeckung → nur das
  **minimale informative Set** läuft. Start statisch (Call-Graph), später dynamisch
  (Coverage-Map) verfeinern.
- **CI-Mirror:** Harness spiegelt CI-Status in eine lokale Ressource → Agent liest Zustand,
  ruft nie ein Werkzeug.
- Alle Ergebnisse werden zu **Events** (siehe Baustein 4).

### 3. Onboard programmiert den Harness (der einzige Kern-Skill)

- `/onboard` (der **einzige** mitgelieferte Workflow-Skill) lässt den Agenten
  bestätigen/ergänzen:
  1. **Aktive Sprachen** → bestimmt, welcher Code überhaupt in den AST-Index eingelesen wird.
  2. **Toolchain je Sprache** (Linter, Typechecker, Test-Runner, Build, Deploy, e2e).
  3. **Autonomie-Policy** (welcher Check auto/debounced/gated läuft).
- **Output:** ein committed **Harness-Programm** (deklaratives Spec + Escape-Hatch-Hooks).
  Das ist der "richtig konfiguriert/programmiert"-Schlüssel — ohne das trägt der Harness nichts.
- **Substrat existiert** und wird geerbt: Detection (`repo_context_overview`),
  `health.yml`, `manifest.yml`, `policies/*.yml`.

### 4. Piggyback-Event-Bus (löst die CC-subscribe/notify-Lücke)

- **Eine Event-Quelle:** die reaktive Engine emittiert typisierte Events.
- **Primärkanal — Piggyback:** jede MCP-Tool-Response trägt einen Pending-Events-Umschlag
  inline mit, z.B.: *"seit deiner letzten Aktion: 2 Tests rot in Modul X, CI-Mirror → grün,
  e2e-Suite Y sauber durch."* Clientunabhängig, deterministisch, funktioniert in CC heute.
- **Dualkanal — Resources:** dieselben Events als MCP-Resources mit `subscribe` für fähige
  Clients. Zukunftspfad, null Rework — gleiche Event-Quelle.
- **Rauschbudget:** der Umschlag ist kompakt, dedupliziert und priorisiert (rote Zustände
  zuerst); Konfiguration begrenzt Größe pro Response.

---

## Schlank halten: was IN und was explizit OUT ist

**IN (der Kern — nur das):**
- AST-Interface-Layer (Navigation + Knoten-Edits, tree-sitter-CST, zweite Text-Modalität).
- Reaktive Tooling-Engine (Trigger, gestaffelte Policy, Test-Selektion, CI-Mirror).
- Piggyback-Event-Bus (+ Resources-Dual).
- `/onboard` als einziger Skill.
- Maschinen-verwaltete **AGENTS.md**-Sektion.

**OUT (bewusst weggelassen ggü. astrojones):**
- Durable memory: `mem_*`, cognee, cognee_local, cognee_sync, claude_mem_reader,
  sync_ledger → komplett raus aus dem Kern.
- Eigene Workflow-Skills (bugfix/feature/refactor/implement/plan/commit) und die 5
  Subagents → ersetzt durch superpowers-Prozess-Skills.
- opencode-TS-Hälfte, drift, scaffold, prompts_registry → raus (opencode ggf. später als
  separates Materialisierungs-Paket, nicht im Kern).

## AGENTS.md — Harness-Priorisierung erzwingen

Der Kern liefert eine **maschinen-verwaltete AGENTS.md-Sektion** (wie astrojones heute die
AGENTS.md-Sektion managt), die jeden Agenten anweist:
- Code **ausschließlich** über AST navigieren/editieren — nie native Datei-Reads für
  in-scope Sprachen.
- **Events lesen** statt Verifikations-Werkzeuge aufzurufen (Ergebnisse kommen von selbst).
- Die **Autonomie-Policy** respektieren (Tier-2-Aktionen brauchen Freigabe).
- Prozess-Workflows über **superpowers** fahren (brainstorming, writing-plans, TDD,
  systematic-debugging, subagent-driven-development).

## Verteilung: schlanker Kern + optionales Bundle

- **Harness-Kern** = eigenständiges Plugin/MCP-Server, nur die vier Bausteine + Onboard.
- **Optionales Bundle-Plugin** = Kern **+ claude-mem** (Memory-Layer, ausgelagert)
  **+ superpowers** (Prozess-Layer). Für wer das Komplettpaket will; der Kern bleibt
  ohne diese Abhängigkeiten lauffähig.

---

## Offene Spannungen (in Detail-Plänen zu lösen)

- CST-Edit-Granularität vs. Formatter-Interplay (wer gewinnt bei Formatierungs-Konflikten).
- Gemischtsprachige Projekte: AST + strukturierter-Text sauber koexistieren lassen.
- Event-Umschlag-Rauschbudget auf *jeder* Response (Größe, Dedup, Priorisierung).
- Test-Relevanz-Scoring: statischer Call-Graph (Start) vs. dynamische Coverage-Map (später).
- Node-Identität über Edits hinweg (stabile Knoten-Refs, wenn sich Byte-Ranges verschieben).

## Warum Greenfield, nicht Evolution (das Delta)

- **Der Kern-Loop ist invertiert:** astrojones ist tool-getrieben (Agent ruft
  `repo_verify_changed`); der neue Harness ist event-getrieben (Verifikation läuft von
  selbst, Agent liest Events). Das ist eine andere Achse, kein Feature-Flag.
- **Der Ballast ist ~60% der Codebasis** (mem/cognee/claude-mem/opencode/drift/scaffold).
  Ihn im Bestand zu entfernen wäre riskanter und langwieriger als ein sauberer Neuaufbau,
  der nur die Konzepte erbt.
- **Breaking-change-frei im Bestand nicht darstellbar:** gate-absolut + reaktive Engine
  ändern das Grundverhalten für bestehende Nutzer. Ein neues Repo lässt astrojones
  unangetastet.
- Übernommene Konzepte (kein Reimplement 1:1): serena-Gateway/-Daemon-Muster,
  tree-sitter-Index (`symbols`), `context`/detect, health.yml/manifest.yml/policies.

## Verifikation (Walking Skeleton zum Konzeptbeweis)

Vor voller Ausarbeitung ein minimaler End-to-End-Durchstich, um die Kette zu beweisen:
1. Eine Sprache (Python), `/onboard` schreibt ein minimales Harness-Programm
   (ein Linter, Tier-0-Policy).
2. Agent macht einen AST-Edit an einem Knoten (`replace_node_body`).
3. Reaktive Engine triggert automatisch lint auf dem Blast-Radius.
4. Der nächste Tool-Call des Agenten liefert den Piggyback-Umschlag mit dem Ergebnis.
5. Beleg: der Agent hat **kein** Verifikations-Werkzeug aufgerufen und kennt trotzdem
   den roten/grünen Zustand.

Erst wenn dieser Durchstich trägt, folgen: zweite Sprache, strukturierter-Text-Modus,
Tier-1 (lokales Deploy + e2e-Selektion), CI-Mirror, Resources-Dual, Bundle-Plugin.

Der detaillierte Umsetzungsplan für den Walking Skeleton liegt unter
`docs/superpowers/plans/2026-07-23-active-harness-walking-skeleton.md`.
