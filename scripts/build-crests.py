#!/usr/bin/env python3
# Rebuild crests/*.svg from the sprite in index.html. Standard library only.
import re, sys, pathlib
root = pathlib.Path(__file__).resolve().parent.parent
src = (root / "index.html").read_text()
out = root / "crests"
defs = re.search(r'<defs>(.*?)</defs>', src, re.S).group(1)
for sym, name in [("g-qm","quartermaster"),("g-cv","conclave"),("g-bk","bridgekeeper"),("g-bp","bopeep"),("g-or","orderly")]:
    body = re.search(r'<symbol id="%s" viewBox="0 0 120 124">(.*?)</symbol>' % sym, src, re.S).group(1)
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 120 124" width="160" height="165">\n'
           f'<title>{name}</title>\n<defs>{defs}</defs>{body}</svg>\n')
    svg = svg.replace('<use href=', '<use xlink:href=')
    (out / f"{name}.svg").write_text(svg)
