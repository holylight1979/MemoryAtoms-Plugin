---
name: consciousness-stream
description: "Consciousness Stream Pipeline (識流): runs high-risk tasks, or tasks that need rigorous analysis, through a structured eight-consciousness pipeline (perceive → analyze → calibrate → execute → write back); use when the user says '用識流…' (use the consciousness stream). Triggers: 識流, 用識流, consciousness stream."
---

# Consciousness Stream Pipeline (識流處理)

> A structured pipeline modeled on the Buddhist eight-consciousness system: perceive → analyze → calibrate → execute → write back.
> Use it for high-risk cross-system tasks or any work that needs rigorous analysis.

---

## Usage

```
/memoryatoms:consciousness-stream <task description>
```

### Parameters

| Parameter | Required | Description | Example |
|------|------|------|------|
| task description | yes | The task to handle (natural language) | `Refactor the plugin loader's reflection-call mechanism` |

### Examples

```
/memoryatoms:consciousness-stream Analyze the error-handling coverage of the API request handlers
/memoryatoms:consciousness-stream Refactor the plugin loader's reflection-call mechanism
/memoryatoms:consciousness-stream 評估 Observer 洩漏的修復方案   # Chinese works too (assess the fix for the Observer leak)
```

### Error handling

- **No task description** → show this usage section and stop.
- **Task description too vague** (e.g. "change the code") → ask the user for a concrete scope.

---

## Task

$ARGUMENTS

## Step 0: Sparśa (觸) - risk grading

Grade risk by impact: shared definitions / base classes > modify, delete, migrate > add, configure > read-only queries (default: low). The keywords below are only a guide:

| Level | Keywords |
|------|--------|
| **Very high** | 框架, 基類, 共用定義, breaking, base class, shared |
| **High** | 修改, 重構, 核心, 刪除, 遷移, 跨系統, refactor, delete, migrate, core |
| **Medium** | 新增, 建立, 設定, 配置, create, add, config, setup |
| **Low** | 讀取, 搜尋, 分析, 查看, 列出, 報告, read, search, analyze, list (default) |

- Low risk → **simplified mode** (skip the five-consciousness / eight-consciousness / luminous-mind parts of Step 1).
- Medium or above → **full mode**.

Output: one-sentence task summary, risk level, domains involved, mode.

## Step 1: Perception + memory gathering

### Five-consciousness perception (full mode only)
- **Eye (眼識)**: read the files the task touches.
- **Nose (鼻識)**: `git status`, environment state.
- **Tongue/body (舌識/身識)**: code quality, system resources (as needed).

### Eighth-consciousness seed store (full mode)
- Read `_AIDocs/_INDEX.md` and look up related documents.
- Retrieve related atoms (match against the Trigger field).
- Retrieve pitfalls and the changelog.

## Step 2: Analysis (sixth + seventh consciousness merged)

**Do all of the following in one pass:**

### Three kinds of knowing (三量)
- **Direct perception** (現量, pratyakṣa): facts observed directly, no speculation.
- **Inference** (比量, anumāna): conclusions drawn from facts; state the basis.
- **False cognition** (非量): uncertain assumptions; mark them explicitly.

### Four-afflictions self-check (四煩惱)
- **Self-delusion (我癡)**: Is anything claimed as known that is not? Any conclusion reached without reading the source?
- **Self-view (我見)**: Clinging to one technology and ignoring alternatives?
- **Self-conceit (我慢)**: Overrating local capability, underrating external resources?
- **Self-love (我愛)**: Favoring your own earlier work?

### Cross-check
- Compare against existing `_AIDocs` knowledge.
- Confirm whether existing decisions (decisions.md) are relevant.

Output: calibrated judgment summary.

## Step 3: Wisdom transformation + calibration (luminous mind + four wisdoms + execution plan merged)

**Do all of the following in one pass:**

### Luminous mind (光明心) - principle calibration (full mode)
- Minimal and lightweight? Highly readable? Thin framework layer? Secure and compliant?
- **Emptiness-of-other view (他空見)**: not empty (make good use of local capability) vs. empty (let go of over-reliance).

### Four-wisdoms action plan
| Wisdom | Content |
|----|------|
| Accomplishing wisdom (成所作智) | Concrete action list: what to do, which files to change, step order |
| Discerning wisdom (妙觀察智) | Why this way; the analytical basis |
| Equality wisdom (平等性智) | Impact assessment on related modules/platforms |
| Great mirror wisdom (大圓鏡智) | Consistency with the overall system architecture |

### Execution checkpoints
- A success condition for every step.

## Step 4: Execution

Execute the action plan step by step, confirming each checkpoint.
- Result as expected → continue.
- New information found → record it for Step 5 (vāsanā).
- Adjustment needed → return to Step 3 and recalibrate.

## Step 5: Vāsanā (薰習) - experience write-back

Write back only **conclusions that were verified or confirmed by the user**; never persist speculation:
- False cognition (unconfirmed assumptions) and the self-reflection lines from the four-afflictions check → **do not** write to memory; leave them in the report.
- Pitfalls proven during Step 4 and decisions the user settled → first list them for the user ("Should these go into memory?"); only the approved ones go through MCP `atom_write` (new cards are always `[臨]`, `domain` is required; project rules use `scope=shared`).
- Do not write what can be read from the final code, tests, or docs (write a one-line file path instead).
- If project files changed → append one row to `_AIDocs/_CHANGELOG.md`.
- Do not hand-edit `MEMORY.md` / `_INDEX.md`: `atom_write` and hooks maintain the indexes.

## Output format

```markdown
## Consciousness stream report (識流報告): [task summary]

### Sparśa (觸) - task identification
- Essence / risk / domains involved / mode

### Five consciousnesses (五識) - perception summary
### Sixth consciousness (六識) - analysis
**Direct perception:** / **Inference:** / **False cognition:** / **Cross-check:**

### Seventh consciousness (七識) - self-reflection
**Four-afflictions self-check:** / **Calibrated judgment:**

### Eighth consciousness (八識) - seed retrieval
### Luminous mind (光明心) - principle calibration
### Four wisdoms (四智) - action plan
| Wisdom | Content |
### Vāsanā (薰習) - experience write-back
```
