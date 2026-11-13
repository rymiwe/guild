<h1 align="center">The Workflow Guild</h1>

<p align="center">Five deterministic tools for agent workflows, and the heraldry they share.</p>

<table align="center">
  <tr>
    <td align="center"><a href="https://github.com/rymiwe/quartermaster"><img src="crests/quartermaster.svg" width="96" alt="quartermaster crest: an abacus on a red shield"><br><b>quartermaster</b></a></td>
    <td align="center"><a href="https://github.com/rymiwe/conclave"><img src="crests/conclave.svg" width="96" alt="conclave crest: a gavel on a purple shield"><br><b>conclave</b></a></td>
    <td align="center"><img src="crests/bridgekeeper.svg" width="96" alt="bridgekeeper crest: a lantern on a blue shield"><br><b>bridgekeeper</b></td>
    <td align="center"><img src="crests/bopeep.svg" width="96" alt="bopeep crest: a crook on a green shield"><br><b>bopeep</b></td>
    <td align="center"><a href="https://github.com/rymiwe/orderly"><img src="crests/orderly.svg" width="96" alt="orderly crest: a gold bell on a black shield"><br><b>orderly</b></a></td>
  </tr>
</table>

| Tool | What it does | Motto |
| --- | --- | --- |
| [quartermaster](https://github.com/rymiwe/quartermaster) | Reads every seat's usage pools and routes work to whichever has headroom. | *Rationes finitae sunt*: the rations are finite |
| [conclave](https://github.com/rymiwe/conclave) | Seats a council of model voters on a brief; a judge returns one decision. | *Quot capita, tot sententiae*: as many heads, as many opinions |
| bridgekeeper (private) | Pre-push guards that keep secrets and work names out of public repos. | *Thou shalt not push* |
| bopeep (private) | Shepherds every open pull request to its next step, and merges the safe ones. | *They will NOT come home on their own* |
| [orderly](https://github.com/rymiwe/orderly) | The orderly room board: the day's business on a browser new-tab page. | *Audite, audite*: hear ye, hear ye |

## The style sheet

[`index.html`](index.html) is the full style sheet: crests, small-size tests, palette, and image-model prompts for the painted character crests. It is published with GitHub Pages at https://rymiwe.github.io/guild/.

## Rules of the house

- **Three shared colors, one per tool.** Parchment `#efe4c8`, ink `#1d1a16`, tarnished gold `#b8892f`, plus the tool's field color.
- **Thick, wobbly ink outlines.** The wobble is an SVG displacement filter, so every crest gets it the same way.
- **The glyph must read at 16px.** The painted character crests are for READMEs and pages; the glyph is for everything small.

## Changing a crest

`index.html` holds the source. Its hidden SVG sprite defines each glyph as a `<symbol>`.

1. Edit the symbol in `index.html`.
2. Run `scripts/build-crests.py`. It rewrites `crests/*.svg`. Standard library only.
3. Copy the new file to the tool's repo as `assets/logo.svg`.
