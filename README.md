<p align="center"><img src="crests/guild.svg" width="160" alt="guild crest: a guildhall on a gold shield"></p>

<h1 align="center">The Workflow Guild</h1>

<p align="center">Six deterministic tools for agent workflows, and the heraldry they share.</p>

<table align="center">
  <tr>
    <td align="center"><a href="https://github.com/rymiwe/quartermaster"><img src="crests/quartermaster.svg" width="96" alt="quartermaster crest: an abacus on a red shield"><br><b>quartermaster</b></a></td>
    <td align="center"><a href="https://github.com/rymiwe/conclave"><img src="crests/conclave.svg" width="96" alt="conclave crest: a gavel on a purple shield"><br><b>conclave</b></a></td>
    <td align="center"><a href="https://github.com/rymiwe/bridgekeeper"><img src="crests/bridgekeeper.svg" width="96" alt="bridgekeeper crest: a lantern on a blue shield"><br><b>bridgekeeper</b></a></td>
    <td align="center"><a href="https://github.com/rymiwe/bopeep"><img src="crests/bopeep.svg" width="96" alt="bopeep crest: a crook on a green shield"><br><b>bopeep</b></a></td>
    <td align="center"><a href="https://github.com/rymiwe/orderly"><img src="crests/orderly.svg" width="96" alt="orderly crest: a gold bell on a black shield"><br><b>orderly</b></a></td>
    <td align="center"><a href="https://github.com/rymiwe/thresher"><img src="crests/thresher.svg" width="96" alt="thresher crest: a gear-toothed drum and a wheat stalk on a tenné shield"><br><b>thresher</b></a></td>
  </tr>
</table>

| Tool | What it does | Motto |
| --- | --- | --- |
| [quartermaster](https://github.com/rymiwe/quartermaster) | Reads every seat's usage pools and routes work to whichever has headroom. | *Rationes finitae sunt*: the rations are finite |
| [conclave](https://github.com/rymiwe/conclave) | Seats a council of model voters on a brief; a judge returns one decision. | *Quot capita, tot sententiae*: as many heads, as many opinions |
| [bridgekeeper](https://github.com/rymiwe/bridgekeeper) | Hooks and gates on every step that leaves the machine: push, PR ready, review post and merge. | *Thou shalt not push* |
| [bopeep](https://github.com/rymiwe/bopeep) | Shepherds every open pull request, yours and your team's, to its next step, and merges the safe ones. | *They will NOT come home on their own* |
| [orderly](https://github.com/rymiwe/orderly) | The orderly room board: the day's business on a browser new-tab page. | *Audite, audite*: hear ye, hear ye |
| [thresher](https://github.com/rymiwe/thresher) | Harvests small, sourced tasks from review leftovers and backlogs, winnows out the chaff, and turns each sheaf into a tiny PR. | *Triticum a palea*: the wheat from the chaff |

## The style sheet

[`index.html`](index.html) is the full style sheet: crests, small-size tests, palette, and image-model prompts for the painted character crests. It is published with GitHub Pages at https://rymiwe.github.io/guild/.

## Rules of the house

- **Three shared colors, one per tool.** Parchment `#efe4c8`, ink `#1d1a16`, tarnished gold `#b8892f`, plus the tool's field color.
- **Thick, wobbly ink outlines.** The wobble is an SVG displacement filter, so every crest gets it the same way.
- **The glyph must read at 16px.** The painted character crests are for READMEs and pages; the glyph is for everything small.

## Org config: `.guild.json`

The tools are public and carry no employer names. Every org fact (repo names, logins, ticket prefixes, persona names) lives in `.guild.json` files under the work root, which git never sees:

| File | Holds |
| --- | --- |
| `<work root>/.guild.json` | Org-wide: persona names, colleague handles, author logins and emails, secret references (never values). |
| `<work root>/<repo>/.guild.json` | Per repo: ticket prefixes, changelog route, CI workflow, lint allowances. List it in that checkout's `.git/info/exclude`. |

- **The work root** is each `work_path_prefixes` entry in `~/.config/work-account/config.json`. Its `work_github_owners` say which repos are work. That file keeps those two keys only.
- **Lookup.** Repo slug (an argument, or the `origin` remote) → owner must be a work owner → canonical checkout `<work root>/<repo>` → its `.guild.json`, then each parent's up to the work root. The nearer file wins; objects merge key by key. Clones and worktrees elsewhere read the canonical checkout's file.
- **Sections.** Top-level keys are shared. A tool keeps its own keys under its name.
- **Fail closed.** A missing or malformed file, or a missing key, exits 3 and names the file and key. No environment variable bypasses it. A tool that posts, merges or pushes loads the file itself and never treats "not work" as "config present".
- **No tool paths.** One tool calls another by command name on `PATH`, and fails closed when it is absent.
- **No `.guild.json` in a tool repo.** Tests use made-up names such as `AcmeCorp`.
- **One loader:** `guild-config` in [quartermaster](https://github.com/rymiwe/quartermaster). Shell callers run `guild-config [--repo OWNER/NAME | --org] KEY`; Python callers import it.
- **The roster.** [`roster`](roster) lists the tools, one per line. `bopeep sync` reads it, so a new tool joins sync by being added there.
- **Every command is a wrapper.** Each tool's `install.sh` writes its commands as two-line `guild-exec` wrappers and `--check` compares their text; only `guild-exec` itself is a link.
- **Wrappers run the main checkout.** A machine-local wrapper calls `guild-exec TOOL PATH "$@"` (quartermaster), which runs `~/code/TOOL/PATH` only when that checkout is clean and on its default branch, and otherwise exits 3. Branch work goes in a worktree; `bopeep sync` fast-forwards the main checkouts.

A starting org file, with made-up values:

```json
{
  "personas": ["Pat"],
  "colleagues": {"alex": "alex-gh"},
  "author_logins": ["you-gh"],
  "author_emails": ["you@example.com"],
  "secrets": {"review_token": "<a secret manager reference>"},
  "conclave": {"lint_names": ["Pat"]}
}
```

## Changing a crest

`index.html` holds the source. Its hidden SVG sprite defines each glyph as a `<symbol>`.

1. Edit the symbol in `index.html`.
2. Run `scripts/build-crests.py`. It rewrites `crests/*.svg`. Standard library only.
3. Copy the new file to the tool's repo as `assets/logo.svg`.
